# SPDX-License-Identifier: Apache-2.0
"""The command: its options, and running a device's opening on the main thread."""

from __future__ import annotations

import socket
import threading
from pathlib import Path

import pytest

from vispek_hc_viewer import __main__ as program
from vispek_hc_viewer import __version__


def test_defaults_touch_real_hardware_only_when_asked_and_never_uv() -> None:
    args = program.parser().parse_args([])
    assert args.simulate is False
    assert args.allow_uv is False
    assert args.port == program.DEFAULT_PORT
    assert args.data_dir == Path("vispek-hc-data")
    assert not any(
        "bind" in action.dest or "host" in action.dest for action in program.parser()._actions
    ), "there is no way to listen on another address"


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        program.main(["--version"])
    assert stopped.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_work_from_another_thread_runs_on_the_serving_thread() -> None:
    runner = program.MainThread()
    stop = threading.Event()
    ran_on: list[str] = []
    results: list[object] = []

    def ask() -> None:
        results.append(runner.call(lambda: ran_on.append(threading.current_thread().name) or 7))
        try:
            runner.call(lambda: 1 / 0)
        except ZeroDivisionError as error:
            results.append(type(error).__name__)
        stop.set()

    worker = threading.Thread(target=ask, name="asker")
    worker.start()
    runner.serve(stop)  # this test's thread plays the main thread
    worker.join(5)
    assert results == [7, "ZeroDivisionError"]
    assert ran_on == [threading.current_thread().name]
    assert runner.call(lambda: "direct") == "direct", "the main thread itself just runs it"


def test_work_handed_over_after_the_end_is_refused_not_left_waiting() -> None:
    from vispek_hc.errors import InvalidStateError

    runner = program.MainThread()
    stop = threading.Event()
    stop.set()
    runner.serve(stop)  # served and ended
    outcome: list[object] = []

    def ask() -> None:
        try:
            outcome.append(runner.call(lambda: "ran"))
        except InvalidStateError as error:
            outcome.append(error.code)

    worker = threading.Thread(target=ask, daemon=True)
    worker.start()
    worker.join(5)
    assert not worker.is_alive(), "the caller is not left waiting for ever"
    assert outcome == ["INVALID_STATE"]


def test_work_still_queued_when_serving_ends_is_refused() -> None:
    from vispek_hc.errors import InvalidStateError

    runner = program.MainThread()
    outcome: list[object] = []

    def ask() -> None:
        try:
            outcome.append(runner.call(lambda: "ran"))
        except InvalidStateError as error:
            outcome.append(error.code)

    worker = threading.Thread(target=ask, daemon=True)
    worker.start()
    while runner._work.empty():
        pass  # the request is queued; nobody is serving
    runner.close()
    worker.join(5)
    assert outcome == ["INVALID_STATE"]


def test_a_caller_whose_work_got_lost_is_released_when_serving_ends() -> None:
    from vispek_hc.errors import InvalidStateError

    runner = program.MainThread()
    outcome: list[object] = []

    def ask() -> None:
        try:
            outcome.append(runner.call(lambda: "ran"))
        except InvalidStateError as error:
            outcome.append(error.code)

    worker = threading.Thread(target=ask, daemon=True)
    worker.start()
    while runner._work.empty():
        pass
    runner._work.get_nowait()  # taken off the queue and never run, as an interrupt can do
    runner._closed = True
    worker.join(5)
    assert outcome == ["INVALID_STATE"]


def test_the_program_passes_its_options_on_and_shuts_everything_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[str] = []
    made: dict[str, object] = {}

    class SpyHub(program.Hub):
        def __init__(self, library: object, **options: object) -> None:
            made.update(options)
            super().__init__(library, **options)  # type: ignore[arg-type]

        def start_watchdog(self, interval_s: float = 1.0) -> None:
            calls.append("watchdog")

        def close(self) -> None:
            calls.append("hub closed")
            super().close()

    def interrupted(self: object, stop: threading.Event) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(program, "Hub", SpyHub)
    monkeypatch.setattr(program.MainThread, "serve", interrupted)
    monkeypatch.setattr(program.safety, "all_off_now", lambda: calls.append("all off now"))
    monkeypatch.setattr(program.webbrowser, "open", lambda address: calls.append("browser"))
    code = program.main(["--port", "0", "--data-dir", str(tmp_path), "--allow-uv", "--simulate"])
    assert code == 0
    assert (made["allow_uv"], made["simulate"]) == (True, True)
    assert calls == ["watchdog", "browser", "hub closed", "all off now"]
    printed = capsys.readouterr().out
    assert "#token=" in printed
    assert "ULTRAVIOLET" in printed
    code = program.main(["--port", "0", "--data-dir", str(tmp_path), "--no-browser"])
    assert (made["allow_uv"], made["simulate"]) == (False, False)
    assert "browser" not in calls[4:]


def test_a_port_in_use_ends_the_program_with_a_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        code = program.main(
            ["--port", str(taken.getsockname()[1]), "--data-dir", str(tmp_path), "--no-browser"]
        )
    assert code == 4
    assert "cannot be used" in capsys.readouterr().err


def test_the_version_is_the_one_the_package_was_installed_with() -> None:
    # It was written into the code once and said 0.1.0.dev0 after the release was built.
    import tomllib
    from importlib.metadata import version
    from pathlib import Path

    project = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text("utf-8"))
    assert __version__ == version("vispek-hc-viewer") == project["project"]["version"]
