# SPDX-License-Identifier: Apache-2.0
"""The device side of the viewer, on the simulated device with its virtual clock."""

from __future__ import annotations

import io
import threading
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import vispek_hc
from PIL import Image
from vispek_hc import Session, SimulatedRig
from vispek_hc.errors import (
    ConfigRejectedError,
    GainNotConfirmedError,
    InvalidStateError,
    OperationCancelledError,
    TimingUnstableError,
    UVNotAllowedError,
)

from vispek_hc_viewer import hub as hub_module
from vispek_hc_viewer.hub import Hub
from vispek_hc_viewer.library import Library


def make(tmp_path: Path, *, allow_uv: bool = False, **rig: Any) -> tuple[Hub, list[SimulatedRig]]:
    """A hub whose devices are simulated on the virtual clock: a scan takes milliseconds."""
    rigs: list[SimulatedRig] = []

    def opener(options: dict[str, Any]) -> tuple[Session, None]:
        made = SimulatedRig(allow_uv=allow_uv, **rig)
        rigs.append(made)
        return Session.from_rig(made), None

    return Hub(Library(tmp_path / "data"), allow_uv=allow_uv, opener=opener), rigs


def finished(hub: Hub) -> dict[str, Any]:
    job = hub.wait_job(10)
    assert job is not None
    assert job["state"] != "running"
    return job


def test_without_a_device_the_state_still_describes_the_leds(tmp_path: Path) -> None:
    hub, _ = make(tmp_path)
    state = hub.state()
    assert state["connected"] is False
    assert state["device"] is None
    assert state["job"] is None
    assert state["allow_uv"] is False
    assert [led["led_id"] for led in state["leds"]] == list(range(1, 18))
    first, sixth = state["leds"][0], state["leds"][5]
    assert (first["nm"], first["hazard"], first["allowed"], first["state"]) == (
        255, "UV-C", False, "off",
    )  # fmt: skip
    assert (sixth["nm"], sixth["hazard"], sixth["allowed"], sixth["scan_pwm"]) == (
        520, None, True, 224,
    )  # fmt: skip
    with pytest.raises(InvalidStateError, match="no device"):
        hub.led(6, True)
    with pytest.raises(InvalidStateError, match="no device"):
        hub.start_job("scan", {})


def test_connecting_locks_the_camera_and_describes_the_device(tmp_path: Path) -> None:
    hub, rigs = make(tmp_path)
    state = hub.connect({"simulate": True})
    assert state["connected"] is True
    device = state["device"]
    assert device["simulated"] is True
    assert device["size"] == [64, 36]
    assert device["locked"] is True
    assert device["lock_error"] is None
    assert device["scene"] is None
    assert rigs[0].controls.values["auto-white-balance-temp"] == 0
    with pytest.raises(InvalidStateError, match="already"):
        hub.connect({"simulate": True})
    hub.led(9, True)
    assert hub.disconnect()["connected"] is False
    assert rigs[0].device.lit == {}
    assert rigs[0].device.closed
    assert hub.disconnect()["connected"] is False, "disconnecting twice is harmless"


def test_a_lock_that_sees_light_is_shown_as_not_locked_with_the_reason(tmp_path: Path) -> None:
    # The camera keeps a gain from the moment of the lock (4.4 times between darkness and
    # light, read back by no control): a lock with the lid open is no lock.
    hub, rigs = make(tmp_path)
    hub.connect({"simulate": True})
    assert hub.state()["device"]["gain_state"] == "dark"
    rigs[0].frames.drop_out(0.5)  # the camera comes back in automatic mode
    rigs[0].frames.wait_new(timeout=5.0)
    stale = hub.state()["device"]
    assert stale["locked"] is False
    assert stale["lock_error"]["code"] == "LOCK_STALE"
    rigs[0].frames.ambient = 40.0  # the lid is open
    with pytest.raises(GainNotConfirmedError):
        hub.lock()
    device = hub.state()["device"]
    assert device["locked"] is False
    assert device["gain_state"] is None
    assert device["lock_error"]["code"] == "GAIN_NOT_CONFIRMED"
    hub.start_job("scan", {})
    job = finished(hub)
    assert (job["state"], job["error"]["code"]) == ("failed", "GAIN_NOT_CONFIRMED")
    assert rigs[0].device.lit == {}
    rigs[0].frames.ambient = 0.0
    assert hub.lock()["locked"] is True
    device = hub.state()["device"]
    assert (device["locked"], device["gain_state"], device["lock_error"]) == (True, "dark", None)


def test_connecting_with_the_lid_open_connects_without_a_lock(tmp_path: Path) -> None:
    hub, _ = make(tmp_path)
    original = Hub._try_lock

    def open_lid(self: Hub, session: Session) -> None:
        session.frames.ambient = 40.0  # type: ignore[attr-defined]
        original(self, session)

    Hub._try_lock = open_lid  # type: ignore[method-assign]
    try:
        device = hub.connect({"simulate": True})["device"]
    finally:
        Hub._try_lock = original  # type: ignore[method-assign]
    assert device["locked"] is False
    assert device["lock_error"]["code"] == "GAIN_NOT_CONFIRMED"
    assert "lid" in device["lock_error"]["hint"]


def test_the_device_counts_as_connected_only_once_its_camera_is_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A page that asks in between must not read "not locked" from a half-opened device.
    hub, _ = make(tmp_path)
    seen: list[bool] = []
    original = Session.lock_controls

    def watching(session: Session) -> Any:
        seen.append(hub.state()["connected"])
        return original(session)

    monkeypatch.setattr(Session, "lock_controls", watching)
    state = hub.connect({})
    assert seen == [False]
    assert state["connected"] is True
    assert state["device"]["locked"] is True


def test_connection_options_are_checked_before_anything_is_opened(tmp_path: Path) -> None:
    hub, rigs = make(tmp_path)
    for bad in ({"size": [3, 3]}, {"fps": 0}, {"source": "film"}, {"port": 7}, {"unit": "../x"}):
        with pytest.raises(ConfigRejectedError):
            hub.connect(bad)
    assert rigs == []


def test_one_led_at_a_time_at_its_scan_power(tmp_path: Path) -> None:
    hub, rigs = make(tmp_path)
    hub.connect({})
    state = hub.led(6, True)
    assert (state["leds"][5]["state"], state["leds"][5]["pwm"]) == ("on", 224)
    assert rigs[0].device.lit == {6: 224}
    state = hub.led(9, True, 100)
    assert rigs[0].device.lit == {9: 100}, "lighting another LED switches the first off"
    assert state["leds"][5]["state"] == "off"
    hub.led(9, False)
    assert rigs[0].device.lit == {}
    with pytest.raises(ConfigRejectedError):
        hub.led(9, True, 5000)
    with pytest.raises(ConfigRejectedError):
        hub.led(99, True)


def test_ultraviolet_needs_the_permission_the_viewer_was_started_with(tmp_path: Path) -> None:
    hub, rigs = make(tmp_path)
    hub.connect({})
    with pytest.raises(UVNotAllowedError):
        hub.led(1, True, 10)
    with pytest.raises(UVNotAllowedError):
        hub.start_job("scan", {"channels": [1, 5]})
    assert rigs[0].device.lit == {}
    assert hub.state()["job"] is None, "refused before a job existed"
    # The hub checks for itself, whatever the device underneath would allow.
    lenient = Hub(
        Library(tmp_path / "lenient"),
        opener=lambda options: (Session.from_rig(SimulatedRig(allow_uv=True)), None),
    )
    lenient.connect({})
    with pytest.raises(UVNotAllowedError):
        lenient.led(1, True, 10)
    with pytest.raises(UVNotAllowedError):
        lenient.start_job("scan", {"channels": [1, 5]})
    lenient.close()
    allowed, allowed_rigs = make(tmp_path / "uv", allow_uv=True)
    allowed.connect({})
    assert allowed.state()["leds"][0]["allowed"] is True
    allowed.led(3, True, 20)
    assert allowed_rigs[0].device.lit == {3: 20}


def test_a_scan_runs_as_a_job_and_lands_in_the_library(tmp_path: Path) -> None:
    hub, rigs = make(tmp_path)
    hub.connect({})
    before = hub.library.revision
    started = hub.start_job("scan", {"name": "first", "setup_label": "bench"})
    assert (started["kind"], started["state"], started["capture"]) == ("scan", "running", "first")
    job = finished(hub)
    assert job["state"] == "done", job
    assert (job["band"], job["bands"]) == (13, 13)
    assert job["error"] is None
    assert hub.library.revision > before
    listed = hub.library.captures()
    assert [entry["id"] for entry in listed] == ["first"]
    assert listed[0]["status"] == "complete"
    assert listed[0]["setup_label"] == "bench"
    assert rigs[0].device.lit == {}
    assert hub.state()["device"]["latency_ms"] is not None, "timing was measured first"


def test_scan_settings_come_from_the_request(tmp_path: Path) -> None:
    hub, _ = make(tmp_path)
    hub.connect({})
    hub.start_job("scan", {"name": "two", "channels": [5, 9], "average": 2, "pwm": {"9": 50}})
    assert finished(hub)["state"] == "done"
    summary = hub.library.summary("two")
    assert [band["led_id"] for band in summary["bands"]] == [5, 9]
    assert summary["bands"][1]["pwm"] == 50
    assert summary["average"] == 2
    for bad in (
        {"channels": "5-9"}, {"channels": [5, 5]}, {"average": 0}, {"pwm": {"x": 1}},
        {"pwm": {"9": 4000}}, {"name": "../up"}, {"white_roi": [0, 0, 2, 2]},
        {"enclosure": "cave"}, {"min_on_ms": "soon"},
    ):  # fmt: skip
        with pytest.raises(ConfigRejectedError):
            hub.start_job("scan", bad)
    with pytest.raises(InvalidStateError, match="already"):
        hub.start_job("scan", {"name": "two"})
    with pytest.raises(ConfigRejectedError):
        hub.start_job("film", {})


def test_a_white_reference_becomes_a_calibration(tmp_path: Path) -> None:
    hub, _ = make(tmp_path)
    hub.connect({})
    hub.start_job("white", {"name": "board", "auto_pwm": True})
    job = finished(hub)
    assert job["state"] == "done", job
    assert job["calibration"] == "board"
    assert hub.library.summary("board")["role"] == "white"
    assert [entry["id"] for entry in hub.library.calibrations()] == ["board"]
    assert hub.state()["leds"][4]["scan_pwm"] > 24, "the search stored a brighter LED 5"
    hub.start_job("scan", {"name": "sample"})
    assert finished(hub)["state"] == "done"
    assert hub.library.layer("sample", calibration="board")["layer"] == "L2"
    hub.start_job("white", {"name": "plain", "calibrate": False})
    assert finished(hub)["calibration"] is None
    with pytest.raises(InvalidStateError, match="already"):
        hub.start_job("white", {"name": "fresh-capture", "calibration_name": "board"})


def test_single_frames_and_the_device_check(tmp_path: Path) -> None:
    hub, _ = make(tmp_path)
    hub.connect({})
    hub.start_job("single", {"name": "one"})
    assert finished(hub)["state"] == "done"
    assert hub.library.summary("one")["mode"] == "single"
    hub.start_job("dark", {})
    job = finished(hub)
    assert hub.library.summary(job["capture"])["role"] == "dark"
    hub.start_job("check", {})
    job = finished(hub)
    assert job["state"] == "done"
    check = hub.state()["device"]["check"]
    assert check["paired"] is True
    assert check["black_level"] == [5, 5, 5]
    assert check["latency_ms"] > 0


def test_a_pwm_search_is_kept_only_when_its_white_reference_succeeds(tmp_path: Path) -> None:
    # A circuit board where the white board should be: its glints clip at any useful
    # power, the search ends at a PWM that gives almost no light, the reference is
    # refused. The unit's scan PWM must stay what it was.
    scene = np.full((36, 64), 0.03)
    scene[10:12, 20:22] = 40.0
    hub, _ = make(tmp_path, reflectance=scene)
    hub.connect({})
    hub.start_job("white", {"name": "not-a-board", "channels": [9], "auto_pwm": True})
    job = finished(hub)
    assert job["state"] == "failed"
    assert job["error"]["code"] == "TIMING_UNSTABLE"
    assert job["calibration"] is None
    assert hub.state()["leds"][8]["scan_pwm"] == 36, "the model's value, not the search's"
    assert hub.library.calibrations() == []


def test_tuning_learns_the_unit_and_leaves_a_calibration(tmp_path: Path) -> None:
    hub, _ = make(tmp_path)
    hub.connect({})
    hub.start_job("tune", {"name": "tuned"})
    job = finished(hub)
    assert job["state"] == "done", job
    assert job["calibration"] == "tuned"
    assert hub.library.summary("tuned")["role"] == "white"


def test_while_a_job_runs_the_device_is_taken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub, rigs = make(tmp_path)
    hub.connect({})
    entered, release = threading.Event(), threading.Event()

    def slow(session: Session, directory: Path, settings: Any, **options: Any) -> None:
        session.light.on(9, 36)
        entered.set()
        try:
            release.wait(10)
            if options["stop"].is_set():
                raise OperationCancelledError("stopped")
        finally:
            session.light.off(9)

    monkeypatch.setattr(vispek_hc, "record", slow)
    hub.start_job("scan", {})
    assert entered.wait(10)
    assert hub.state()["job"]["state"] == "running"
    with pytest.raises(InvalidStateError, match="running"):
        hub.led(6, True)
    with pytest.raises(InvalidStateError, match="running"):
        hub.start_job("scan", {})
    with pytest.raises(InvalidStateError, match="running"):
        hub.set_control("gain", 1)
    hub.stop_job()
    release.set()
    job = finished(hub)
    assert job["state"] == "cancelled"
    assert rigs[0].device.lit == {}
    hub.led(6, True)  # the device is free again


def test_all_off_stops_a_job_and_disconnecting_does_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub, rigs = make(tmp_path)
    hub.connect({})

    def until_stopped(session: Session, directory: Path, settings: Any, **options: Any) -> None:
        session.light.on(9, 36)
        try:
            options["stop"].wait(10)
            raise OperationCancelledError("stopped")
        finally:
            session.light.off(9)

    monkeypatch.setattr(vispek_hc, "record", until_stopped)
    hub.start_job("scan", {})
    state = hub.all_off()
    assert state["job"]["state"] == "cancelled"
    assert rigs[0].device.lit == {}
    hub.start_job("scan", {})
    assert hub.disconnect()["connected"] is False
    assert rigs[0].device.closed


def test_no_job_can_start_between_stopping_one_and_closing_the_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A second request arriving while the device is being disconnected must find it
    # taken, not a gap in which it lights an LED that the closing port then strands.
    hub, rigs = make(tmp_path)
    hub.connect({})
    started: list[str] = []
    original = hub._end_job

    def end_then_intrude() -> bool:
        ended = original()
        got = hub._ops.acquire(blocking=False)
        if got:
            hub._ops.release()
        started.append("free" if got else "held")
        return ended

    monkeypatch.setattr(hub, "_end_job", end_then_intrude)
    hub.disconnect()
    hub.connect({})
    hub.all_off()
    assert started == ["held", "held"]
    assert rigs[0].device.lit == {}


def test_a_job_that_does_not_stop_in_time_cannot_light_anything_more(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub, rigs = make(tmp_path)
    hub.connect({})
    monkeypatch.setattr(hub_module, "JOB_END_WAIT_S", 0.05)
    lit, release = threading.Event(), threading.Event()
    seen: list[BaseException] = []

    def deaf(session: Session, directory: Path, settings: Any, **options: Any) -> None:
        session.light.on(9, 36)
        lit.set()
        release.wait(10)  # ignores the stop it was sent
        try:
            session.light.on(10, 18)
        except BaseException as error:
            seen.append(error)
            raise

    monkeypatch.setattr(vispek_hc, "record", deaf)
    hub.start_job("scan", {})
    assert lit.wait(10)
    with pytest.raises(InvalidStateError, match="did not stop") as overruled:
        hub.all_off()
    assert overruled.value.details == {"unconfirmed": []}
    assert rigs[0].device.lit == {}, "everything was switched off over the job's head"
    notices = hub.state()["notices"]
    assert "did not stop" in notices[-1]["text"]
    release.set()
    finished(hub)
    assert seen, "the job could not light another LED"
    assert rigs[0].device.lit == {}
    hub.led(6, True)  # a normal all-off afterwards made the device usable again
    assert rigs[0].device.lit == {6: 224}


def test_disconnecting_overrules_a_job_that_does_not_stop_and_the_next_device_is_usable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub, rigs = make(tmp_path)
    hub.connect({})
    monkeypatch.setattr(hub_module, "JOB_END_WAIT_S", 0.05)
    lit, release = threading.Event(), threading.Event()

    def deaf(session: Session, directory: Path, settings: Any, **options: Any) -> None:
        session.light.on(9, 36)
        lit.set()
        release.wait(10)

    monkeypatch.setattr(vispek_hc, "record", deaf)
    hub.start_job("scan", {})
    assert lit.wait(10)
    assert hub.disconnect()["connected"] is False
    assert rigs[0].device.lit == {}, "switched off over the job's head"
    assert rigs[0].device.closed
    assert "did not stop" in hub.state()["notices"][-1]["text"]
    # The old job is still stuck. A new connection must not inherit it.
    state = hub.connect({})
    assert state["job"] is None
    hub.led(6, True)
    assert rigs[1].device.lit == {6: 224}
    assert all(led["state"] == "off" for led in hub.all_off()["leds"])
    release.set()


def test_the_idle_switch_off_leaves_a_running_job_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub, rigs = make(tmp_path)
    hub.connect({})
    lit, release = threading.Event(), threading.Event()

    def long(session: Session, directory: Path, settings: Any, **options: Any) -> None:
        session.light.on(9, 36)
        lit.set()
        try:
            release.wait(10)
        finally:
            session.light.off(9)

    monkeypatch.setattr(vispek_hc, "record", long)
    hub.start_job("scan", {})
    assert lit.wait(10)
    hub.touch(now=0.0)
    hub.idle_check(now=10_000.0)  # nobody has asked for hours: the scan goes on
    assert rigs[0].device.lit == {9: 36}
    assert hub.state()["notices"] == []
    release.set()
    assert finished(hub)["state"] == "done"


def test_an_off_that_is_not_confirmed_is_never_shown_as_off(tmp_path: Path) -> None:
    hub, rigs = make(tmp_path)
    hub.connect({})
    hub.led(9, True)
    rigs[0].device.unplugged = True
    with pytest.raises(vispek_hc.VispekHCError) as caught:
        hub.disconnect()
    assert caught.value.code == "LED_SHUTDOWN_UNCONFIRMED"
    state = hub.state()
    assert state["connected"] is False
    assert state["leds"][8]["state"] == "unknown", "LED 9 may still be lit"
    assert state["notices"][-1]["level"] == "error"
    assert "unplug" in state["notices"][-1]["text"]
    assert hub.connect({})["leds"][8]["state"] == "off", "a new connection starts afresh"


def test_an_unconfirmed_off_after_a_failed_job_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub, rigs = make(tmp_path)
    hub.connect({})

    def unplugged(session: Session, directory: Path, settings: Any, **options: Any) -> None:
        session.light.on(9, 36)
        rigs[0].device.unplugged = True
        raise TimingUnstableError("frames did not settle")

    monkeypatch.setattr(vispek_hc, "record", unplugged)
    hub.start_job("scan", {})
    job = finished(hub)
    assert job["state"] == "failed"
    assert any("unconfirmed" in warning for warning in job["warnings"])
    state = hub.state()
    assert state["notices"][-1]["level"] == "error"
    assert state["leds"][8]["state"] == "unknown"


def test_a_job_always_ends_even_when_cleaning_up_fails_unexpectedly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub, _ = make(tmp_path)
    hub.connect({})

    def failing(session: Session, directory: Path, settings: Any, **options: Any) -> None:
        def broken() -> None:
            raise RuntimeError("no such thing")

        monkeypatch.setattr(session.light, "all_off", broken)
        raise TimingUnstableError("frames did not settle")

    monkeypatch.setattr(vispek_hc, "record", failing)
    hub.start_job("scan", {})
    job = finished(hub)
    assert job["state"] == "failed", "never left running"
    assert hub.state()["notices"][-1]["level"] == "error"


def test_a_failed_job_says_why_and_leaves_nothing_lit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub, rigs = make(tmp_path)
    hub.connect({})

    def failing(session: Session, directory: Path, settings: Any, **options: Any) -> None:
        session.light.on(9, 36)  # and no OFF: the job must clean up
        raise TimingUnstableError("frames did not settle", details={"led_id": 9})

    monkeypatch.setattr(vispek_hc, "record", failing)
    hub.start_job("scan", {})
    job = finished(hub)
    assert job["state"] == "failed"
    assert job["error"]["code"] == "TIMING_UNSTABLE"
    assert job["error"]["details"] == {"led_id": 9}
    assert rigs[0].device.lit == {}

    def broken(session: Session, directory: Path, settings: Any, **options: Any) -> None:
        raise RuntimeError("secret path /Users/someone")

    monkeypatch.setattr(vispek_hc, "record", broken)
    hub.start_job("scan", {})
    job = finished(hub)
    assert job["error"]["code"] == "INTERNAL_ERROR"
    assert "someone" not in job["error"]["message"]


def test_exposure_and_gain_can_be_set_and_stay_in_the_unit_profile(tmp_path: Path) -> None:
    hub, rigs = make(tmp_path)
    hub.connect({})
    controls = hub.controls()
    assert controls["editable"] == ["exposure-time-abs", "gain"]
    exposure = controls["values"]["exposure-time-abs"]
    assert (exposure["current"], exposure["minimum"], exposure["maximum"]) == (157, 1, 5000)
    after = hub.set_control("exposure-time-abs", 100)
    assert after["values"]["exposure-time-abs"]["current"] == 100
    assert after["locked"] is True
    assert rigs[0].controls.values["exposure-time-abs"] == 100
    hub.start_job("scan", {"name": "short"})
    assert finished(hub)["state"] == "done"
    assert hub.library.summary("short")["exposure"] == 100, "the scan kept the chosen exposure"
    with pytest.raises(ConfigRejectedError):
        hub.set_control("white-balance-temp", 3000)
    with pytest.raises(ConfigRejectedError):
        hub.set_control("exposure-time-abs", 99999)
    with pytest.raises(ConfigRejectedError):
        hub.set_control("gain", "much")
    assert hub.controls()["values"]["exposure-time-abs"]["current"] == 100


def test_the_scan_power_of_an_led_can_be_changed_and_reset(tmp_path: Path) -> None:
    hub, _ = make(tmp_path)
    hub.connect({})
    assert hub.set_scan_pwm(6, 300)["leds"][5]["scan_pwm"] == 300
    hub.led(6, True)
    assert hub.state()["leds"][5]["pwm"] == 300
    assert hub.set_scan_pwm(6, None)["leds"][5]["scan_pwm"] == 224
    with pytest.raises(ConfigRejectedError):
        hub.set_scan_pwm(6, 0)
    with pytest.raises(UVNotAllowedError):
        hub.set_scan_pwm(1, 10)


def test_the_live_picture_and_its_numbers(tmp_path: Path) -> None:
    hub, rigs = make(tmp_path)
    assert hub.preview(None, 0.01) is None, "no device, no picture"
    hub.connect({})
    key, jpeg = hub.preview(None, 1.0)  # type: ignore[misc]
    image = np.asarray(Image.open(io.BytesIO(jpeg)))
    assert image.shape == (36, 64, 3)
    assert hub.preview(key, 0.01) is None, "no new frame yet"
    dark = hub.live()
    assert dark["size"] == [64, 36]
    assert dark["mean"] == [5.0, 5.0, 5.0]
    assert dark["saturated"] == 0.0
    assert dark["sharpness"] == 0.0, "a flat picture has no detail"
    assert len(dark["histogram"]) == 3
    assert len(dark["histogram"][0]) == 64
    assert sum(dark["histogram"][0]) > 0
    hub.led(9, True, 1023)
    rigs[0].frames.wait_new(5)
    lit = hub.live({"x": 10, "y": 10, "w": 4, "h": 4})
    assert lit["mean"][0] == 255.0
    assert lit["saturated"] == 1.0
    assert lit["probe"]["rgb"][0] == 255.0
    assert lit["probe"]["pixels"] == 16
    hub.set_preview({"clipping": True})
    _, marked = hub.preview(key, 1.0)  # type: ignore[misc]
    pixel = np.asarray(Image.open(io.BytesIO(marked)))[18, 32]
    assert pixel[0] > 200
    assert pixel[1] < 60
    assert pixel[2] > 200, "clipped pixels are drawn magenta"
    with pytest.raises(ConfigRejectedError):
        hub.live({"x": -5, "y": 0})
    with pytest.raises(ConfigRejectedError):
        hub.set_preview({"clipping": "yes"})


def test_sharpness_is_highest_for_the_picture_with_the_finest_detail() -> None:
    rows, columns = np.indices((72, 128))
    checker = ((rows + columns) % 2 * 200).astype(np.uint8)
    sharp = np.stack([checker] * 3, axis=2)
    blurred = np.stack([np.full_like(checker, 100)] * 3, axis=2)
    assert hub_module._sharpness(sharp) > 100
    assert hub_module._sharpness(blurred) == 0.0
    assert hub_module._sharpness(np.zeros((2, 2, 3), np.uint8)) == 0.0


def test_leds_go_off_when_nobody_is_watching(tmp_path: Path) -> None:
    hub, rigs = make(tmp_path)
    hub.connect({})
    hub.led(9, True)
    hub.touch(now=1000.0)
    hub.idle_check(now=1000.0 + hub_module.IDLE_OFF_S - 1)
    assert rigs[0].device.lit == {9: 36}
    hub.idle_check(now=1000.0 + hub_module.IDLE_OFF_S + 1)
    assert rigs[0].device.lit == {}
    notices = hub.state()["notices"]
    assert notices
    assert "switched off" in notices[-1]["text"]
    hub.idle_check(now=5000.0)  # nothing lit: nothing to do, no second notice
    assert len(hub.state()["notices"]) == len(notices)
    hub.led(9, True)
    hub.touch(now=6000.0)
    rigs[0].device.unplugged = True
    hub.idle_check(now=7000.0)
    told = hub.state()
    assert told["notices"][-1]["level"] == "error", "an OFF nobody confirmed is an error"
    assert told["leds"][8]["state"] == "unknown"


def test_closing_the_hub_leaves_everything_off(tmp_path: Path) -> None:
    hub, rigs = make(tmp_path)
    hub.connect({})
    hub.led(9, True)
    hub.close()
    assert rigs[0].device.lit == {}
    assert rigs[0].device.closed
    hub.close()


def test_the_scene_can_only_be_changed_on_the_viewers_own_simulated_device(
    tmp_path: Path,
) -> None:
    hub, _ = make(tmp_path)
    hub.connect({})
    with pytest.raises(InvalidStateError):
        hub.set_scene("white")


def test_the_device_the_hub_opens_itself_carries_its_uv_permission(tmp_path: Path) -> None:
    for allowed in (False, True):
        hub = Hub(Library(tmp_path / str(allowed)), simulate=True, allow_uv=allowed)
        try:
            hub.connect({})
            assert hub._session is not None
            assert hub._session.light.allow_uv is allowed
        finally:
            hub.close()


def test_the_real_time_simulated_device_is_what_simulate_opens(tmp_path: Path) -> None:
    hub = Hub(Library(tmp_path / "data"), simulate=True)
    try:
        state = hub.connect({})
        assert state["device"]["simulated"] is True
        assert state["device"]["scene"] == "sample"
        assert state["device"]["size"] == [640, 360]
        assert hub.set_scene("white")["device"]["scene"] == "white"
        with pytest.raises(ConfigRejectedError):
            hub.set_scene("moon")
    finally:
        hub.close()
