# SPDX-License-Identifier: Apache-2.0
"""The HTTP side: who may ask, what each route answers, how errors look."""

from __future__ import annotations

import http.client
import json
import logging
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from vispek_hc import Session, SimulatedRig

from vispek_hc_viewer.hub import Hub
from vispek_hc_viewer.library import Library
from vispek_hc_viewer.server import App, make_server

PNG = b"\x89PNG\r\n\x1a\n"


class Client:
    def __init__(self, port: int, token: str) -> None:
        self.port, self.token = port, token

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        token: str | None = "",
        headers: dict[str, str] | None = None,
        raw: bytes | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        sent = dict(headers or {})
        if token == "":
            token = self.token
        if token is not None:
            sent.setdefault("Authorization", f"Bearer {token}")
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            sent.setdefault("Content-Type", "application/json")
        try:
            connection.request(method, path, body=data, headers=sent)
            response = connection.getresponse()
            return (
                response.status,
                {k.lower(): v for k, v in response.getheaders()},
                response.read(),
            )
        finally:
            connection.close()

    def get(self, path: str, **options: Any) -> Any:
        status, _, body = self.request("GET", path, **options)
        assert status == 200, (status, body)
        return json.loads(body)

    def post(self, path: str, body: Any = None, expect: int = 200) -> Any:
        status, _, answer = self.request("POST", path, {} if body is None else body)
        assert status == expect, (status, answer)
        return json.loads(answer)

    def wait_for_job(self) -> dict[str, Any]:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            job = self.get("/api/state")["job"]
            if job and job["state"] != "running":
                return job  # type: ignore[no-any-return]
            time.sleep(0.02)
        raise AssertionError("the job did not finish")


@pytest.fixture
def served(tmp_path: Path) -> Iterator[tuple[Client, App]]:
    def opener(options: dict[str, Any]) -> tuple[Session, None]:
        return Session.from_rig(SimulatedRig()), None

    library = Library(tmp_path / "data")
    hub = Hub(library, opener=opener)
    app = App(hub, library)
    server = make_server(app, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield Client(server.server_address[1], app.token), app
    finally:
        server.shutdown()
        server.server_close()
        hub.close()
        thread.join(5)


# --- who may ask -----------------------------------------------------------------------


def test_the_page_is_served_without_a_token_and_holds_none(served: tuple[Client, App]) -> None:
    client, app = served
    for path, kind in (
        ("/", "text/html"),
        ("/index.html", "text/html"),
        ("/app.js", "text/javascript"),
        ("/style.css", "text/css"),
        ("/favicon.svg", "image/svg+xml"),
    ):
        status, headers, body = client.request("GET", path, token=None)
        assert status == 200, path
        assert headers["content-type"].startswith(kind)
        assert headers["x-content-type-options"] == "nosniff"
        assert "access-control-allow-origin" not in headers
        assert app.token.encode() not in body
    _, headers, _ = client.request("GET", "/", token=None)
    policy = headers["content-security-policy"]
    assert "default-src 'self'" in policy
    assert "unsafe-inline" not in policy
    assert "frame-ancestors 'none'" in policy


@pytest.mark.parametrize(
    "path", ["/hub.py", "/static/app.js", "/../hub.py", "/app.js/..", "/%2e%2e/hub.py", "/api"]
)
def test_nothing_else_is_served(served: tuple[Client, App], path: str) -> None:
    client, _ = served
    status, _, _ = client.request("GET", path)
    assert status == 404


def test_the_api_needs_the_token(served: tuple[Client, App]) -> None:
    client, app = served
    for token in (None, "wrong", app.token + "x", ""):
        headers = {} if token is None else {"Authorization": f"Bearer {token}"}
        status, _, body = client.request("GET", "/api/state", token=None, headers=headers)
        assert status == 401
        assert json.loads(body)["error"]["code"] == "UNAUTHORISED"
    status, _, _ = client.request(
        "GET", "/api/state", token=None, headers={"Authorization": f"Basic {app.token}"}
    )
    assert status == 401
    status, _, _ = client.request("GET", f"/api/state?token={app.token}", token=None)
    assert status == 401, "the token is not accepted in the address"
    assert client.get("/api/state")["connected"] is False


def test_only_this_computer_by_its_own_name_is_answered(served: tuple[Client, App]) -> None:
    client, _ = served
    for host in ("evil.example", f"evil.example:{client.port}", "127.0.0.1:1", ""):
        status, _, body = client.request("GET", "/api/state", headers={"Host": host})
        assert status == 403, host
        assert json.loads(body)["error"]["code"] == "FORBIDDEN"
    status, _, _ = client.request("GET", "/", token=None, headers={"Host": "evil.example"})
    assert status == 403
    status, _, _ = client.request("GET", "/api/state", headers={"Host": f"localhost:{client.port}"})
    assert status == 200


def test_requests_from_other_sites_are_refused(served: tuple[Client, App]) -> None:
    client, _ = served
    status, _, _ = client.request(
        "POST", "/api/leds/off", {}, headers={"Origin": "http://evil.example"}
    )
    assert status == 403
    status, _, _ = client.request(
        "POST", "/api/preview/options", {"clipping": False},
        headers={"Origin": f"http://127.0.0.1:{client.port}"},
    )  # fmt: skip
    assert status == 200


def test_bodies_are_json_and_small(served: tuple[Client, App]) -> None:
    client, _ = served
    status, _, _ = client.request(
        "POST", "/api/connect", raw=b"simulate=1",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )  # fmt: skip
    assert status == 415
    status, _, _ = client.request(
        "POST", "/api/connect", raw=b"{ not json", headers={"Content-Type": "application/json"}
    )
    assert status == 400
    status, _, _ = client.request(
        "POST", "/api/connect", raw=b"[1, 2]", headers={"Content-Type": "application/json"}
    )
    assert status == 400, "a body is an object"
    status, _, _ = client.request(
        "POST", "/api/connect", raw=b"{" + b" " * 2_000_000 + b"}",
        headers={"Content-Type": "application/json"},
    )  # fmt: skip
    assert status == 413
    assert client.get("/api/state")["connected"] is False


def test_a_refused_request_with_a_body_ends_its_connection(served: tuple[Client, App]) -> None:
    # Its body was not read: on a kept connection it would be taken for the next request.
    client, _ = served
    status, headers, _ = client.request("POST", "/api/connect", {"simulate": True}, token="wrong")
    assert status == 401
    assert headers["connection"] == "close"
    status, headers, _ = client.request(
        "POST", "/api/connect", {"simulate": True}, headers={"Host": "evil.example"}
    )
    assert (status, headers["connection"]) == (403, "close")
    status, headers, _ = client.request("GET", "/api/state")
    assert status == 200
    assert headers.get("connection") != "close"


def test_a_malformed_request_gets_an_answer_not_a_traceback(
    served: tuple[Client, App], capfd: pytest.CaptureFixture[str]
) -> None:
    import socket

    client, _ = served
    with socket.create_connection(("127.0.0.1", client.port), timeout=5) as raw:
        raw.sendall(b"NONSENSE\r\n\r\n")
        answer = raw.recv(4096)
    assert b"400" in answer, (
        "a one-word request is answered in the old style, without a status line"
    )
    assert "Traceback" not in capfd.readouterr().err
    with socket.create_connection(("127.0.0.1", client.port), timeout=5) as raw:
        raw.sendall(b"GET / HTTP/1.1 extra words\r\n\r\n")
        assert b" 400 " in raw.recv(4096).split(b"\r\n")[0]
    assert "Traceback" not in capfd.readouterr().err


def test_a_page_that_goes_away_mid_request_leaves_no_traceback(
    served: tuple[Client, App], capfd: pytest.CaptureFixture[str]
) -> None:
    import socket
    import struct

    client, _ = served
    raw = socket.create_connection(("127.0.0.1", client.port), timeout=5)
    raw.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    raw.sendall(b"GET /api/state HTTP/1.1\r\nHost: 127.0.0.1\r\n")  # and no more
    raw.close()  # with a reset, as a browser that drops a connection does
    time.sleep(0.3)
    assert client.get("/api/state")["connected"] is False, "the server goes on"
    assert "Traceback" not in capfd.readouterr().err


def test_bad_values_in_a_body_are_refused_not_crashed_on(served: tuple[Client, App]) -> None:
    client, _ = served
    client.post("/api/connect", {})
    client.post("/api/jobs", {"kind": "scan", "name": "leaf"}, expect=202)
    client.wait_for_job()
    for output in ("0", 1.5, []):
        answer = client.post(
            "/api/captures/leaf/export", {"format": "envi", "output": output}, expect=400
        )
        assert answer["error"]["code"] == "CONFIG_REJECTED"
        answer = client.post(
            "/api/captures/leaf/spectra.csv",
            {"regions": [{"point": [1, 1]}], "output": output},
            expect=400,
        )
        assert answer["error"]["code"] == "CONFIG_REJECTED"


def test_other_methods_and_unknown_routes(served: tuple[Client, App]) -> None:
    client, _ = served
    for method in ("PUT", "DELETE", "PATCH", "OPTIONS"):
        status, headers, _ = client.request(method, "/api/state")
        assert status == 405, method
        assert "access-control-allow-origin" not in headers
    status, _, _ = client.request("GET", "/api/nothing")
    assert status == 404
    status, _, _ = client.request("POST", "/api/state", {})
    assert status == 405


# --- the device ------------------------------------------------------------------------


def test_connect_light_and_disconnect(served: tuple[Client, App]) -> None:
    client, _ = served
    state = client.post("/api/connect", {"simulate": True})
    assert state["connected"] is True
    assert state["device"]["simulated"] is True
    state = client.post("/api/led", {"led_id": 6, "on": True})
    assert state["leds"][5]["state"] == "on"
    state = client.post("/api/led", {"led_id": 9, "on": True, "pwm": 80})
    assert (state["leds"][8]["pwm"], state["leds"][5]["state"]) == (80, "off")
    assert client.post("/api/leds/scan-pwm", {"led_id": 9, "pwm": 60})["leds"][8]["scan_pwm"] == 60
    state = client.post("/api/leds/off")
    assert all(led["state"] == "off" for led in state["leds"])
    camera = client.get("/api/camera")
    assert camera["values"]["exposure-time-abs"]["current"] == 157
    camera = client.post("/api/camera", {"name": "exposure-time-abs", "value": 200})
    assert camera["values"]["exposure-time-abs"]["current"] == 200
    assert client.post("/api/camera/lock")["locked"] is True
    assert client.post("/api/disconnect")["connected"] is False


def test_errors_carry_code_message_and_hint(served: tuple[Client, App]) -> None:
    client, _ = served
    answer = client.post("/api/led", {"led_id": 6, "on": True}, expect=409)
    assert answer["error"]["code"] == "INVALID_STATE"
    assert answer["error"]["hint"] == "connect first"
    client.post("/api/connect", {})
    answer = client.post("/api/led", {"led_id": 1, "on": True, "pwm": 10}, expect=403)
    assert answer["error"]["code"] == "UV_NOT_ALLOWED"
    assert answer["error"]["details"] == {"led_ids": [1]}
    answer = client.post("/api/led", {"led_id": "six", "on": True}, expect=400)
    assert answer["error"]["code"] == "CONFIG_REJECTED"
    answer = client.post("/api/led", {"on": True}, expect=400)
    assert answer["error"]["code"] == "CONFIG_REJECTED"
    answer = client.post("/api/jobs", {"kind": "scan", "channels": [1]}, expect=403)
    assert answer["error"]["code"] == "UV_NOT_ALLOWED"
    status, _, body = client.request("GET", "/api/captures/none/view.png")
    assert status == 404
    assert json.loads(body)["error"]["code"] == "FRAME_NOT_FOUND"
    status, _, _ = client.request("GET", "/api/captures/..%2Fx/view.png")
    assert status in (400, 404)


def test_an_unexpected_failure_says_nothing_about_the_computer(
    served: tuple[Client, App], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, app = served

    def broken() -> dict[str, Any]:
        raise RuntimeError("/Users/someone/secret")

    with monkeypatch.context() as patch:
        patch.setattr(app.hub, "state", broken)
        status, _, body = client.request("GET", "/api/state")
    assert status == 500
    assert json.loads(body)["error"]["code"] == "INTERNAL_ERROR"
    assert b"someone" not in body


# --- recording and looking -------------------------------------------------------------


def test_scan_then_look_at_it(served: tuple[Client, App]) -> None:
    client, _ = served
    client.post("/api/connect", {})
    job = client.post("/api/jobs", {"kind": "white", "name": "board"}, expect=202)
    assert (job["kind"], job["state"]) == ("white", "running")
    assert client.wait_for_job()["calibration"] == "board"
    client.post("/api/jobs", {"kind": "scan", "name": "leaf"}, expect=202)
    assert client.wait_for_job()["state"] == "done"

    listed = client.get("/api/captures")
    assert [entry["id"] for entry in listed["captures"]] == ["leaf", "board"]
    assert listed["revision"] == client.get("/api/state")["library_revision"]
    assert client.get("/api/captures/leaf")["status"] == "complete"
    assert [entry["id"] for entry in client.get("/api/calibrations")["calibrations"]] == ["board"]

    for path in (
        "/api/captures/leaf/thumbnail.png",
        "/api/captures/leaf/view.png?mode=rgb",
        "/api/captures/leaf/view.png?mode=band:led_09&palette=viridis&gamma=0.8&low=0&high=200",
        "/api/captures/leaf/view.png?mode=ndvi&calibration=board&overlay=1",
    ):
        status, headers, body = client.request("GET", path)
        assert status == 200, path
        assert headers["content-type"] == "image/png"
        assert body.startswith(PNG)
    status, _, body = client.request("GET", "/api/captures/leaf/view.png?mode=ndvi")
    assert status == 409, "an index needs a calibration"
    status, _, _ = client.request("GET", "/api/captures/leaf/view.png?mode=rgb&gamma=wide")
    assert status == 400
    status, _, _ = client.request("GET", "/api/captures/leaf/view.png?mode=nonsense")
    assert status == 400

    scale = client.get("/api/captures/leaf/scale?mode=ndvi&calibration=board")
    assert scale == {"low": -1.0, "high": 1.0, "palette": "spectral", "unit": "index"}
    assert client.get("/api/captures/leaf/scale?mode=rgb") is None
    status, headers, body = client.request("GET", "/api/palettes/spectral.png")
    assert (status, headers["content-type"]) == (200, "image/png")
    assert body.startswith(PNG)
    status, _, _ = client.request("GET", "/api/palettes/..%2Fx.png")
    assert status == 400

    assert client.get("/api/captures/leaf/layer")["layer"] == "L1"
    assert client.get("/api/captures/leaf/layer?calibration=board")["layer"] == "L2"
    point = client.get("/api/captures/leaf/spectrum?x=3&y=4&calibration=board")
    assert point["geometry"] == {"point": [3, 4]}
    assert len(point["bands"]) == 13
    assert point["bands"][0]["mean"] == pytest.approx(1.0, abs=0.05)
    rect = client.get("/api/captures/leaf/spectrum?x=3&y=4&w=5&h=6&name=A")
    assert rect["geometry"] == {"rect": [3, 4, 5, 6]}
    assert rect["name"] == "A"
    status, _, _ = client.request("GET", "/api/captures/leaf/spectrum?x=three&y=4")
    assert status == 400

    status, headers, body = client.request(
        "POST", "/api/captures/leaf/spectra.csv",
        {"calibration": "board", "regions": [{"name": "A", "rect": [1, 1, 3, 3]},
                                             {"name": "B", "point": [9, 9]}]},
    )  # fmt: skip
    assert status == 200
    assert headers["content-type"].startswith("text/csv")
    assert "attachment" in headers["content-disposition"]
    assert body.decode().count("\n") == 1 + 2 * 13

    exported = client.post("/api/captures/leaf/export", {"format": "envi"})
    assert exported["directory"] == "exports/leaf-L1-envi"
    made = client.post("/api/calibrations", {"whites": ["board"], "name": "again"})
    assert made["id"] == "again"
    answer = client.post("/api/calibrations", {"whites": ["board"], "name": "again"}, expect=409)
    assert answer["error"]["code"] == "INVALID_STATE"


def test_a_job_can_be_stopped_and_only_one_runs(
    served: tuple[Client, App], monkeypatch: pytest.MonkeyPatch
) -> None:
    import vispek_hc
    from vispek_hc.errors import OperationCancelledError

    client, _ = served
    client.post("/api/connect", {})

    def until_stopped(session: Any, directory: Any, settings: Any, **options: Any) -> None:
        options["stop"].wait(10)
        raise OperationCancelledError("stopped")

    monkeypatch.setattr(vispek_hc, "record", until_stopped)
    client.post("/api/jobs", {"kind": "scan"}, expect=202)
    answer = client.post("/api/jobs", {"kind": "scan"}, expect=409)
    assert answer["error"]["code"] == "INVALID_STATE"
    assert client.post("/api/jobs/stop")["job"]["stopping"] is True
    assert client.wait_for_job()["state"] == "cancelled"


# --- the live picture ------------------------------------------------------------------


def read_part(response: http.client.HTTPResponse) -> bytes:
    """One JPEG of a multipart stream."""
    assert response.readline().strip() == b"--frame"
    length = 0
    while (line := response.readline().strip()) != b"":
        name, _, value = line.partition(b":")
        if name.lower() == b"content-length":
            length = int(value)
    data = response.read(length)
    assert response.readline() == b"\r\n"
    return data


def test_the_live_stream_needs_a_ticket_that_works_once(served: tuple[Client, App]) -> None:
    client, _ = served
    client.post("/api/connect", {})
    status, _, _ = client.request("GET", "/api/preview.mjpg")
    assert status == 401, "the token itself does not open the stream"
    status, _, _ = client.request("GET", "/api/preview.mjpg?ticket=guess", token=None)
    assert status == 401
    ticket = client.post("/api/preview/ticket")["ticket"]
    connection = http.client.HTTPConnection("127.0.0.1", client.port, timeout=20)
    try:
        connection.request("GET", f"/api/preview.mjpg?ticket={ticket}")
        response = connection.getresponse()
        assert response.status == 200
        assert response.getheader("Content-Type") == "multipart/x-mixed-replace; boundary=frame"
        assert read_part(response).startswith(b"\xff\xd8")
    finally:
        connection.close()
    status, _, _ = client.request("GET", f"/api/preview.mjpg?ticket={ticket}", token=None)
    assert status == 401, "a ticket opens one stream"
    live = client.get("/api/live?x=1&y=1&w=2&h=2")
    assert live["size"] == [64, 36]
    assert live["probe"]["pixels"] == 4
    assert client.post("/api/preview/options", {"clipping": True})["preview"]["clipping"] is True


def test_an_open_stream_counts_as_somebody_watching(
    served: tuple[Client, App], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, app = served
    client.post("/api/connect", {})
    ticket = client.post("/api/preview/ticket")["ticket"]
    touched: list[int] = []
    with monkeypatch.context() as patch:
        patch.setattr(app.hub, "touch", lambda: touched.append(1))
        connection = http.client.HTTPConnection("127.0.0.1", client.port, timeout=20)
        try:
            connection.request("GET", f"/api/preview.mjpg?ticket={ticket}")
            response = connection.getresponse()
            read_part(response)
            read_part(response)  # no request in between: the frames alone keep contact
            assert len(touched) >= 2
        finally:
            response.close()
            connection.close()


def test_only_a_few_streams_at_a_time(served: tuple[Client, App]) -> None:
    from vispek_hc_viewer.server import MAX_STREAMS

    client, app = served
    client.post("/api/connect", {})
    connections = []
    try:
        for _ in range(MAX_STREAMS):
            ticket = client.post("/api/preview/ticket")["ticket"]
            connection = http.client.HTTPConnection("127.0.0.1", client.port, timeout=20)
            connection.request("GET", f"/api/preview.mjpg?ticket={ticket}")
            response = connection.getresponse()
            assert response.status == 200
            read_part(response)
            connections.append((connection, response))
        ticket = client.post("/api/preview/ticket")["ticket"]
        status, _, body = client.request("GET", f"/api/preview.mjpg?ticket={ticket}", token=None)
        assert status == 503
        assert json.loads(body)["error"]["code"] == "INVALID_STATE"
        assert client.post("/api/preview/ticket")["ticket"], "tickets are still handed out"
    finally:
        for connection, response in connections:
            response.close()  # the response holds the socket too
            connection.close()
    deadline = time.monotonic() + 10
    while app.streams and time.monotonic() < deadline:
        time.sleep(0.05)
    assert app.streams == 0, "a closed stream gives its place back"


def test_a_ticket_expires(served: tuple[Client, App], monkeypatch: pytest.MonkeyPatch) -> None:
    client, app = served
    client.post("/api/connect", {})
    ticket = client.post("/api/preview/ticket")["ticket"]
    real = time.monotonic
    monkeypatch.setattr("vispek_hc_viewer.server.time.monotonic", lambda: real() + 3600)
    assert app.redeem(ticket) is False


def test_the_log_holds_no_token_and_no_ticket(
    served: tuple[Client, App], caplog: pytest.LogCaptureFixture
) -> None:
    client, app = served
    caplog.set_level(logging.DEBUG)
    client.post("/api/connect", {})
    ticket = client.post("/api/preview/ticket")["ticket"]
    client.request("GET", f"/api/preview.mjpg?ticket={ticket}x", token=None)
    client.request("GET", f"/api/state?token={app.token}", token=None)
    client.get("/api/state")
    assert caplog.text, "requests are logged"
    assert app.token not in caplog.text
    assert ticket not in caplog.text


def test_asking_keeps_hand_lit_leds_on_and_devices_are_listed(
    served: tuple[Client, App], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, app = served
    touched = []
    with monkeypatch.context() as patch:
        patch.setattr(app.hub, "touch", lambda: touched.append(1))
        client.get("/api/state")
        assert touched, "an answered request counts as somebody watching"
        count = len(touched)
        client.request("GET", "/api/state", token="wrong")
        client.request("GET", "/", token=None)
        assert len(touched) == count, "only requests with the token count"
        patch.setattr(
            app.hub, "devices", lambda: {"serial": ["/dev/cu.usbserial-1"], "cameras": []}
        )
        assert client.get("/api/devices")["serial"] == ["/dev/cu.usbserial-1"]
