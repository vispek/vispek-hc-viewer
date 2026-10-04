# SPDX-License-Identifier: Apache-2.0
"""The HTTP side of the viewer: one page and a JSON API, for this computer only.

- Bound to 127.0.0.1; there is no option to bind anything else.
- The page itself (``/``, ``/app.js``, ``/style.css``) is public and holds no secret.
  Everything under ``/api/`` needs ``Authorization: Bearer <token>``. The token is made
  at start, handed over once in the fragment of the address the program prints, and is
  never accepted in a query string and never logged.
- The live stream is an ``<img>``, which cannot send a header: it is opened with a
  ticket that works once and for half a minute.
- Requests whose ``Host`` is not this server's own address are refused (DNS rebinding),
  as are requests carrying another site's ``Origin``. No CORS header is ever sent.
- Captures and calibrations are addressed by name inside the data folder. No request
  carries a path.
"""

from __future__ import annotations

import hmac
import json
import logging
import math
import re
import secrets
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from vispek_hc import VispekHCError
from vispek_hc.errors import ConfigRejectedError

from vispek_hc_viewer import __version__
from vispek_hc_viewer.hub import Hub
from vispek_hc_viewer.library import Library, palette_png

log = logging.getLogger(__name__)

MAX_BODY = 1024 * 1024
DRAIN_LIMIT = 8 * 1024 * 1024  # an oversized body up to this is read and dropped
TICKET_LIFE_S = 30.0
MAX_STREAMS = 4
STREAM_WAIT_S = 1.0
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; img-src 'self' blob: data:; object-src 'none'; base-uri 'none'; "
    "frame-ancestors 'none'; form-action 'none'"
)
_BAD_REQUEST = {"CONFIG_REJECTED", "USAGE_ERROR", "SCHEMA_UNSUPPORTED"}


def status_of(error: VispekHCError) -> int:
    """The HTTP status of an SDK error; the page reads ``error.code``, not this."""
    if error.code in _BAD_REQUEST:
        return 400
    if error.code == "UV_NOT_ALLOWED":
        return 403
    if error.code == "FRAME_NOT_FOUND":
        return 404
    return {3: 503, 4: 503, 5: 409, 6: 502, 7: 422, 130: 409}.get(error.exit_code, 500)


def _clean(value: Any) -> Any:
    """JSON has no NaN: a number that is not finite becomes null."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_clean(item) for item in value]
    return value


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, str]
    body: dict[str, Any]
    names: tuple[str, ...] = ()  # the captured parts of the path, percent-decoded

    def text(self, key: str) -> str | None:
        return self.query.get(key) or None

    def number(self, key: str, kind: type = float) -> Any:
        value = self.query.get(key)
        if value is None or value == "":
            return None
        try:
            number = kind(value)
        except ValueError:
            raise ConfigRejectedError(f"'{key}' is a number", details={"option": key}) from None
        if isinstance(number, float) and not math.isfinite(number):
            raise ConfigRejectedError(f"'{key}' is a number", details={"option": key})
        return number

    def flag(self, key: str) -> bool:
        value = self.query.get(key, "0")
        if value not in ("0", "1", "true", "false", ""):
            raise ConfigRejectedError(f"'{key}' is 0 or 1", details={"option": key})
        return value in ("1", "true")

    def field(self, key: str, default: Any = None) -> Any:
        return self.body.get(key, default)


@dataclass
class Response:
    body: bytes
    content_type: str = "application/json"
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)


def reply(data: Any, status: int = 200) -> Response:
    return Response(json.dumps(_clean(data), allow_nan=False).encode(), status=status)


def problem(status: int, code: str, message: str, hint: str | None = None) -> Response:
    return reply({"error": {"code": code, "message": message, "hint": hint, "details": {}}}, status)


class App:
    """What the handlers share: the hub, the library, the token and the stream tickets."""

    def __init__(self, hub: Hub, library: Library, token: str | None = None) -> None:
        self.hub = hub
        self.library = library
        self.token = token or secrets.token_urlsafe(32)
        self._tickets: dict[str, float] = {}
        self._guard = threading.Lock()
        self.streams = 0

    def authorised(self, header: str | None) -> bool:
        scheme, _, given = (header or "").partition(" ")
        return scheme == "Bearer" and hmac.compare_digest(given.encode(), self.token.encode())

    def ticket(self) -> str:
        made = secrets.token_urlsafe(24)
        now = time.monotonic()
        with self._guard:
            self._tickets = {t: end for t, end in self._tickets.items() if end > now}
            self._tickets[made] = now + TICKET_LIFE_S
        return made

    def redeem(self, ticket: str | None) -> bool:
        """True once for a ticket that exists and has not run out."""
        with self._guard:
            expires = self._tickets.pop(ticket or "", None)
        return expires is not None and expires > time.monotonic()


# --- routes ----------------------------------------------------------------------------


def _calibrated(request: Request) -> dict[str, Any]:
    return {
        "calibration": request.text("calibration"),
        "output": request.number("output", int) or 0,
        "force": request.flag("force"),
    }


def _region(source: dict[str, Any]) -> dict[str, Any]:
    if "rect" in source:
        return {"rect": source["rect"]}
    if "point" in source:
        return {"point": source["point"]}
    raise ConfigRejectedError("a region is a point [x, y] or a rect [x, y, w, h]")


def _view(app: App, request: Request) -> Response:
    gamma = request.number("gamma")
    data = app.library.view_png(
        request.names[0],
        request.text("mode") or "rgb",
        low=request.number("low"),
        high=request.number("high"),
        gamma=1.0 if gamma is None else gamma,
        palette=request.text("palette") or "auto",
        overlay=request.flag("overlay"),
        **_calibrated(request),
    )
    return Response(data, "image/png")


def _scale(app: App, request: Request) -> Response:
    return reply(
        app.library.scale(
            request.names[0],
            request.text("mode") or "rgb",
            low=request.number("low"),
            high=request.number("high"),
            palette=request.text("palette") or "auto",
            **_calibrated(request),
        )
    )


def _spectrum(app: App, request: Request) -> Response:
    x, y = request.number("x", int), request.number("y", int)
    w, h = request.number("w", int), request.number("h", int)
    if x is None or y is None:
        raise ConfigRejectedError("a spectrum needs x and y")
    geometry = {"point": [x, y]} if w is None and h is None else {"rect": [x, y, w or 1, h or 1]}
    return reply(
        app.library.spectrum(
            request.names[0], geometry, label=request.text("name") or "", **_calibrated(request)
        )
    )


def _spectra_csv(app: App, request: Request) -> Response:
    regions = request.field("regions")
    if not isinstance(regions, list) or not all(isinstance(entry, dict) for entry in regions):
        raise ConfigRejectedError("regions is a list of named points and rectangles")
    data = app.library.spectra_csv(
        request.names[0],
        [(str(entry.get("name") or f"region {n + 1}"), _region(entry))
         for n, entry in enumerate(regions)],
        calibration=request.field("calibration"),
        output=request.field("output", 0),
        force=bool(request.field("force", False)),
    )  # fmt: skip
    name = re.sub(r"[^A-Za-z0-9._-]", "_", request.names[0])
    return Response(
        data,
        "text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}-spectra.csv"'},
    )


def _export(app: App, request: Request) -> Response:
    return reply(
        app.library.export(
            request.names[0],
            request.field("format"),
            calibration=request.field("calibration"),
            output=request.field("output", 0),
            force=bool(request.field("force", False)),
        )
    )


def _make_calibration(app: App, request: Request) -> Response:
    whites, roi = request.field("whites"), request.field("white_roi")
    if not isinstance(whites, list):
        raise ConfigRejectedError("whites is a list of white-reference captures")
    if roi is not None and not (
        isinstance(roi, list) and len(roi) == 4 and all(type(n) in (int, float) for n in roi)
    ):
        raise ConfigRejectedError("the white region is [x, y, w, h] in fractions")
    made = app.library.make_calibration(whites, request.field("name"), tuple(roi) if roi else None)
    return reply({"id": made})


def _live(app: App, request: Request) -> Response:
    probe = None
    if "x" in request.query or "y" in request.query:
        probe = {
            key: request.number(key, int)
            for key in ("x", "y", "w", "h")
            if request.query.get(key) not in (None, "")
        }
    return reply(app.hub.live(probe))


def _led(app: App, request: Request) -> Response:
    on = request.field("on")
    if type(on) is not bool:
        raise ConfigRejectedError("'on' is true or false")
    return reply(app.hub.led(request.field("led_id"), on, request.field("pwm")))


def _start_job(app: App, request: Request) -> Response:
    given = dict(request.body)
    return reply(app.hub.start_job(given.pop("kind", None), given), 202)


Route = tuple[str, re.Pattern[str], Callable[[App, Request], Response]]
_NAME = r"([^/]+)"


def _route(method: str, pattern: str, handler: Callable[[App, Request], Response]) -> Route:
    return method, re.compile(pattern), handler


ROUTES: list[Route] = [
    _route("GET", r"/api/state", lambda app, r: reply(app.hub.state())),
    _route("GET", r"/api/devices", lambda app, r: reply(app.hub.devices())),
    _route("POST", r"/api/connect", lambda app, r: reply(app.hub.connect(r.body))),
    _route("POST", r"/api/disconnect", lambda app, r: reply(app.hub.disconnect())),
    _route("POST", r"/api/scene", lambda app, r: reply(app.hub.set_scene(r.field("name")))),
    _route("POST", r"/api/led", _led),
    _route("POST", r"/api/leds/off", lambda app, r: reply(app.hub.all_off())),
    _route(
        "POST",
        r"/api/leds/scan-pwm",
        lambda app, r: reply(app.hub.set_scan_pwm(r.field("led_id"), r.field("pwm"))),
    ),
    _route("GET", r"/api/camera", lambda app, r: reply(app.hub.controls())),
    _route(
        "POST",
        r"/api/camera",
        lambda app, r: reply(app.hub.set_control(r.field("name"), r.field("value"))),
    ),
    _route("POST", r"/api/camera/lock", lambda app, r: reply(app.hub.lock())),
    _route("POST", r"/api/preview/ticket", lambda app, r: reply({"ticket": app.ticket()})),
    _route("POST", r"/api/preview/options", lambda app, r: reply(app.hub.set_preview(r.body))),
    _route("GET", r"/api/live", _live),
    _route("POST", r"/api/jobs", _start_job),
    _route("POST", r"/api/jobs/stop", lambda app, r: reply(app.hub.stop_job())),
    _route(
        "GET",
        r"/api/captures",
        lambda app, r: reply(
            {"captures": app.library.captures(), "revision": app.library.revision}
        ),
    ),
    _route("GET", rf"/api/captures/{_NAME}", lambda app, r: reply(app.library.summary(r.names[0]))),
    _route(
        "GET",
        rf"/api/captures/{_NAME}/thumbnail\.png",
        lambda app, r: Response(app.library.thumbnail(r.names[0]), "image/png"),
    ),
    _route(
        "GET",
        rf"/api/captures/{_NAME}/layer",
        lambda app, r: reply(app.library.layer(r.names[0], **_calibrated(r))),
    ),
    _route("GET", rf"/api/captures/{_NAME}/view\.png", _view),
    _route("GET", rf"/api/captures/{_NAME}/scale", _scale),
    _route("GET", rf"/api/captures/{_NAME}/spectrum", _spectrum),
    _route(
        "GET",
        rf"/api/palettes/{_NAME}\.png",
        lambda app, r: Response(palette_png(r.names[0]), "image/png"),
    ),
    _route("POST", rf"/api/captures/{_NAME}/spectra\.csv", _spectra_csv),
    _route("POST", rf"/api/captures/{_NAME}/export", _export),
    _route(
        "GET",
        r"/api/calibrations",
        lambda app, r: reply({"calibrations": app.library.calibrations()}),
    ),
    _route("POST", r"/api/calibrations", _make_calibration),
]


# --- the handler -----------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"vispek-hc-viewer/{__version__}"
    sys_version = ""
    timeout = 60  # an idle connection is given up after this long
    app: App
    _unread = False

    def log_message(self, format: str, *args: Any) -> None:
        # The address may carry a stream ticket: only method, path and status are logged.
        # A request line that could not be parsed has neither a command nor a path.
        log.debug("%s %s", getattr(self, "command", "?"), urlsplit(getattr(self, "path", "")).path)

    def _send(self, response: Response) -> None:
        if self._unread:
            self.close_connection = True  # a body nobody read must not be taken for a request
        self.send_response(response.status)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        for name, value in response.headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(response.body)

    def _own_names(self) -> set[str]:
        port = self.connection.getsockname()[1]
        return {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _length(self) -> int:
        try:
            return int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return -1

    def _body(self) -> dict[str, Any] | Response:
        length = self._length()
        if length < 0:
            return problem(400, "CONFIG_REJECTED", "the request has no usable length")
        if length > MAX_BODY:
            if length <= DRAIN_LIMIT:
                left = length
                while left > 0 and (chunk := self.rfile.read(min(left, 65536))):
                    left -= len(chunk)
                self._unread = left > 0
            return problem(413, "CONFIG_REJECTED", "the request is too large")
        data = self.rfile.read(length) if length else b""
        self._unread = False
        kind = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if kind != "application/json":
            return problem(415, "CONFIG_REJECTED", "requests carry application/json")
        try:
            body = json.loads(data) if data else {}
        except ValueError:
            return problem(400, "CONFIG_REJECTED", "the request is not valid JSON")
        if not isinstance(body, dict):
            return problem(400, "CONFIG_REJECTED", "the request body is a JSON object")
        return body

    def _handle(self, method: str) -> None:
        app = self.app
        self._unread = self._length() != 0
        split = urlsplit(self.path)
        path, query = split.path, dict(parse_qsl(split.query, keep_blank_values=True))
        if self.headers.get("Host", "") not in self._own_names():
            return self._send(problem(403, "FORBIDDEN", "this server answers only as 127.0.0.1"))
        origin = self.headers.get("Origin")
        if origin is not None and origin.removeprefix("http://") not in self._own_names():
            return self._send(problem(403, "FORBIDDEN", "requests from other sites are refused"))
        if method not in ("GET", "POST"):
            return self._send(problem(405, "CONFIG_REJECTED", f"{method} is not used here"))
        if method == "GET" and path in STATIC:
            return self._static(*STATIC[path])
        if not path.startswith("/api/"):
            return self._send(problem(404, "FRAME_NOT_FOUND", "there is nothing at this address"))
        if path == "/api/preview.mjpg" and method == "GET":
            if not app.redeem(query.get("ticket")):
                return self._send(problem(401, "UNAUTHORISED", "the stream needs a fresh ticket"))
            return self._stream()
        if not app.authorised(self.headers.get("Authorization")):
            return self._send(
                problem(401, "UNAUTHORISED", "the token is missing or wrong",
                        "open the address the viewer printed when it started")
            )  # fmt: skip
        app.hub.touch()
        body: dict[str, Any] = {}
        if method == "POST":
            parsed = self._body()
            if isinstance(parsed, Response):
                return self._send(parsed)
            body = parsed
        known = False
        for route_method, pattern, handler in ROUTES:
            match = pattern.fullmatch(path)
            if match is None:
                continue
            known = True
            if route_method != method:
                continue
            names = tuple(unquote(part) for part in match.groups())
            return self._send(self._answer(handler, Request(method, path, query, body, names)))
        if known:
            return self._send(problem(405, "CONFIG_REJECTED", f"{method} is not used here"))
        self._send(problem(404, "FRAME_NOT_FOUND", "there is nothing at this address"))

    def _answer(self, handler: Callable[[App, Request], Response], request: Request) -> Response:
        try:
            return handler(self.app, request)
        except VispekHCError as error:
            return reply({"error": error.to_dict()}, status_of(error))
        except Exception:
            log.exception("%s %s failed", request.method, request.path)
            return problem(500, "INTERNAL_ERROR", "the viewer failed unexpectedly; see its log")

    def _static(self, name: str, content_type: str) -> None:
        data = (resources.files("vispek_hc_viewer") / "static" / name).read_bytes()
        headers = {}
        if name.endswith(".html"):
            headers = {
                "Content-Security-Policy": CONTENT_SECURITY_POLICY,
                "X-Frame-Options": "DENY",
            }
        self._send(Response(data, content_type, headers=headers))

    def _stream(self) -> None:
        """The live picture as multipart JPEG, until the page closes it or the device goes."""
        app = self.app
        with app._guard:
            admitted = app.streams < MAX_STREAMS
            if admitted:
                app.streams += 1
        if not admitted:  # answered outside the lock: a slow reader must not hold it
            return self._send(problem(503, "INVALID_STATE", "too many live streams are open"))
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            key: str | None = None
            jpeg = b""
            while True:
                fresh = app.hub.preview(key, STREAM_WAIT_S)
                if fresh is not None:
                    key, jpeg = fresh
                elif not jpeg or not app.hub.connected:
                    break
                # Without a new frame the last one is sent again: a write is the only way
                # to learn that the page has gone.
                self.wfile.write(
                    b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                    + str(len(jpeg)).encode()
                    + b"\r\n\r\n"
                    + jpeg
                    + b"\r\n"
                )
                self.wfile.flush()
                app.hub.touch()
        except OSError:
            pass  # the page closed the stream
        finally:
            with app._guard:
                app.streams -= 1

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def do_PATCH(self) -> None:
        self._handle("PATCH")

    def do_OPTIONS(self) -> None:
        self._handle("OPTIONS")


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows this option would let another program listen on the same port as well.
    allow_reuse_address = sys.platform != "win32"

    def handle_error(self, request: Any, client_address: Any) -> None:
        """A page that drops its connection is ordinary; anything else goes to the log."""
        error = sys.exc_info()[1]
        if isinstance(error, ConnectionError | TimeoutError):
            log.debug("a connection ended: %s", type(error).__name__)
        else:
            log.exception("a request failed outside its handler")


def make_server(app: App, port: int) -> ThreadingHTTPServer:
    """A server for ``app`` on 127.0.0.1 and nowhere else. Port 0 picks a free one."""
    handler = type("BoundHandler", (Handler,), {"app": app})
    return _Server(("127.0.0.1", port), handler)
