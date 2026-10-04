# SPDX-License-Identifier: Apache-2.0
"""``vispek-hc-viewer``: start the local server and open the page."""

from __future__ import annotations

import argparse
import contextlib
import logging
import queue
import sys
import threading
import webbrowser
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import vispek_hc
from vispek_hc import safety
from vispek_hc.errors import InvalidStateError

from vispek_hc_viewer import __version__
from vispek_hc_viewer.hub import Hub
from vispek_hc_viewer.library import Library
from vispek_hc_viewer.server import App, make_server

DEFAULT_PORT = 8642


class MainThread:
    """Runs work handed over by other threads on the thread that calls :meth:`serve`.

    A device is opened here, on the main thread, because only there can the SDK install
    the signal handlers that switch the LEDs off when the process is told to end.
    """

    def __init__(self) -> None:
        self._work: queue.Queue[tuple[Callable[[], Any], dict[str, Any], threading.Event]] = (
            queue.Queue()
        )
        self._guard = threading.Lock()
        self._closed = False

    def call(self, work: Callable[[], Any]) -> Any:
        if threading.current_thread() is threading.main_thread():
            return work()
        box: dict[str, Any] = {}
        done = threading.Event()
        with self._guard:
            if self._closed:
                raise InvalidStateError("the viewer is shutting down")
            self._work.put((work, box, done))
        while not done.wait(0.25):
            if self._closed and "value" not in box and "error" not in box:
                # Taken off the queue and never run: an interrupt can do that.
                raise InvalidStateError("the viewer is shutting down")
        if "error" in box:
            raise box["error"]
        return box["value"]

    def close(self) -> None:
        """Nobody serves any more: whoever waits, or asks from now on, is refused."""
        with self._guard:
            self._closed = True
            while True:
                try:
                    _, box, done = self._work.get_nowait()
                except queue.Empty:
                    break
                box["error"] = InvalidStateError("the viewer is shutting down")
                done.set()

    def serve(self, stop: threading.Event) -> None:
        try:
            while not stop.is_set():
                try:
                    work, box, done = self._work.get(timeout=0.25)
                except queue.Empty:
                    continue
                try:
                    box["value"] = work()
                except Exception as error:
                    box["error"] = error
                except BaseException:
                    box["error"] = InvalidStateError("the viewer is shutting down")
                    raise
                finally:
                    done.set()
        finally:
            self.close()


def parser() -> argparse.ArgumentParser:
    made = argparse.ArgumentParser(
        prog="vispek-hc-viewer",
        description="Local web viewer for the Vispek HC1500: live view, LED control, scans, "
        "white references, pictures and spectra. It serves one page on 127.0.0.1.",
    )
    made.add_argument(
        "--data-dir",
        type=Path,
        default=Path("vispek-hc-data"),
        help="folder for captures, calibrations and exports (default: ./vispek-hc-data)",
    )
    made.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"port on 127.0.0.1 (default {DEFAULT_PORT}; 0 picks a free one)",
    )
    made.add_argument(
        "--simulate", action="store_true", help="open the simulated device only; touch no hardware"
    )
    made.add_argument(
        "--allow-uv",
        action="store_true",
        help="permit LEDs 1-4 (UV-C and UV-A). Read SAFETY.md of vispek-hc first",
    )
    made.add_argument("--no-browser", action="store_true", help="do not open the page")
    made.add_argument("-v", "--verbose", action="store_true", help="log every request")
    made.add_argument(
        "--version",
        action="version",
        version=f"vispek-hc-viewer {__version__} (vispek-hc {vispek_hc.__version__})",
    )
    return made


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    library = Library(args.data_dir)
    main_thread = MainThread()
    hub = Hub(library, allow_uv=args.allow_uv, simulate=args.simulate, run_in_main=main_thread.call)
    app = App(hub, library)
    try:
        server = make_server(app, args.port)
    except OSError as error:
        print(
            f"vispek-hc-viewer: port {args.port} cannot be used ({error.strerror}). "
            "Is the viewer already running? Try --port 0.",
            file=sys.stderr,
        )
        return 4
    address = f"http://127.0.0.1:{server.server_address[1]}/#token={app.token}"
    serving = threading.Thread(target=server.serve_forever, name="vispek-hc-http", daemon=True)
    serving.start()
    hub.start_watchdog()
    print(f"Vispek HC Viewer {__version__}")
    print(f"  data folder: {args.data_dir}")
    if args.simulate:
        print("  simulated device only: no hardware is touched")
    if args.allow_uv:
        print("  ULTRAVIOLET LEDs ARE PERMITTED: keep the light source shielded")
    print(f"  open: {address}")
    print("  Ctrl-C ends the viewer and switches every LED off.", flush=True)
    if not args.no_browser:
        webbrowser.open(address)
    stop = threading.Event()
    try:
        main_thread.serve(stop)
    except KeyboardInterrupt:
        # The terminal may already be gone (a pipe that got the Ctrl-C too): saying so
        # must not stand in the way of switching the LEDs off.
        with contextlib.suppress(OSError):
            print("\nending: switching every LED off", flush=True)
    finally:
        stop.set()
        main_thread.close()  # a connect still waiting for this thread is refused
        try:
            hub.close()
        finally:
            safety.all_off_now()  # whatever could not be closed in order
            server.shutdown()
            server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
