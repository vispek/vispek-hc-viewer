# SPDX-License-Identifier: Apache-2.0
"""The device side of the viewer: one connected HC1500, its LEDs, its camera, its jobs.

The SDK's objects are made for one caller. The hub is the one caller: every request of
the web page goes through it, and it decides what may happen at the same time.

- One thing drives the LEDs at a time. While a job (a scan, a white reference, a check)
  runs, lighting an LED by hand or changing a camera parameter is refused.
- The live picture may always be read: the camera hands frames to any thread.
- A job runs on its own thread, reports its progress, and can be stopped; whatever
  happens to it, every LED is off afterwards.
- An LED lit by hand goes off when nobody has asked for anything for a while: the page
  was closed, or the computer went to sleep (``IDLE_OFF_S``).
"""

from __future__ import annotations

import io
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, TypeVar

import numpy as np
import vispek_hc
from numpy.typing import NDArray
from PIL import Image
from vispek_hc import HC1500, ScanSettings, Session, VispekHCError, framecheck, safety, video
from vispek_hc.cube import read_events
from vispek_hc.errors import (
    ConfigRejectedError,
    ControlsUnavailableError,
    InvalidStateError,
    OperationCancelledError,
)
from vispek_hc.illuminator import UNPLUG_HINT
from vispek_hc.sweep import check_plan, keep_pwm

from vispek_hc_viewer import simrig
from vispek_hc_viewer.library import NAME, Library

log = logging.getLogger(__name__)

T = TypeVar("T")
Opener = Callable[[dict[str, Any]], tuple[Session, simrig.SimulatedDevice | None]]

IDLE_OFF_S = 30.0  # an LED lit by hand goes off after this long without any request
JOB_KINDS = ("scan", "white", "tune", "single", "dark", "check")
EDITABLE_CONTROLS = ("exposure-time-abs", "gain")
SOURCES = ("uncompressed", "mjpeg")
HISTOGRAM_BINS = 64
JPEG_QUALITY = 85
CLIPPED_COLOUR = (255, 0, 255)
JOB_END_WAIT_S = 15.0
_SCAN_KEYS = {
    "name", "channels", "average", "pwm", "auto_pwm", "white_roi", "setup_label", "enclosure",
    "reduction", "unlocked", "calibrate", "calibration_name", "min_on_ms",
}  # fmt: skip


def _refuse(message: str, **details: Any) -> ConfigRejectedError:
    return ConfigRejectedError(message, details=details)


def _whole(value: Any) -> bool:
    return type(value) is int


@dataclass
class Job:
    """One thing the device is busy with, and how far it got."""

    job_id: int
    kind: str
    state: str = "running"  # running, done, failed, cancelled
    phase: str = ""
    band: int = 0
    bands: int = 0
    led_id: int | None = None
    group: str | None = None
    capture: str | None = None
    calibration: str | None = None
    error: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    finished: float | None = None
    stop: threading.Event = field(default_factory=threading.Event)

    def progress(self, update: dict[str, Any]) -> None:
        self.band, self.bands = update["band"], update["bands"]
        self.led_id, self.group = update["led_id"], update["group_id"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.job_id, "kind": self.kind, "state": self.state, "phase": self.phase,
            "band": self.band, "bands": self.bands, "led_id": self.led_id, "group": self.group,
            "capture": self.capture, "calibration": self.calibration, "error": self.error,
            "warnings": list(self.warnings), "started": self.started,
            "finished": self.finished, "stopping": self.stop.is_set(),
        }  # fmt: skip


@dataclass(frozen=True)
class _Picture:
    key: str
    jpeg: bytes
    rgb: NDArray[np.uint8]
    stats: dict[str, Any]


def _sharpness(rgb: NDArray[np.uint8]) -> float:
    """Mean local contrast (absolute Laplacian of the green channel) in the middle half of
    the picture: it peaks where the lens is in focus. Only its change means anything."""
    height, width = rgb.shape[:2]
    middle = rgb[height // 4 : height - height // 4, width // 4 : width - width // 4, 1]
    grey = middle.astype(np.int16)
    if grey.shape[0] < 3 or grey.shape[1] < 3:
        return 0.0
    laplacian = (
        4 * grey[1:-1, 1:-1] - grey[:-2, 1:-1] - grey[2:, 1:-1] - grey[1:-1, :-2] - grey[1:-1, 2:]
    )
    return round(float(np.abs(laplacian).mean()), 3)


def scan_warnings(directory: Path) -> list[str]:
    """What a person should know about a finished scan, from its event log."""
    try:
        events = read_events(directory)
    except (OSError, VispekHCError):
        return []
    warnings = []
    for kind, text in (
        ("saturation_warning", "clipped pixels"),
        ("no_led_response", "no light seen"),
    ):
        leds = sorted({event["led_id"] for event in events if event.get("type") == kind})
        if leds:
            warnings.append(f"{text} under LED {', '.join(map(str, leds))}")
    return warnings


def _locked(session: Session) -> bool:
    """Whether scans made now are comparable: white balance and exposure fixed, the gain
    the camera keeps settled in darkness, and the stream not reopened since."""
    report = session.lock_report
    return bool(
        report
        and report.awb_off
        and report.exposure_manual is not False
        and report.gain_state == "dark"
        and not session.lock_stale
    )


_STALE = {
    "code": "LOCK_STALE",
    "message": "the camera stream was reopened since the parameters were locked",
    "hint": "close the lid and lock again; the next scan does it by itself",
    "details": {},
}


class Hub:
    def __init__(
        self,
        library: Library,
        *,
        allow_uv: bool = False,
        simulate: bool = False,
        opener: Opener | None = None,
        run_in_main: Callable[[Callable[[], Any]], Any] | None = None,
    ) -> None:
        """``simulate``: only ever open the simulated device. ``run_in_main`` runs the
        opening of a device on the main thread, where the SDK can install the signal
        handlers that switch the LEDs off when the process is told to end."""
        self.library = library
        self.allow_uv = allow_uv
        self.simulate = simulate
        self._opener = opener or self._open_device
        self._marshal = run_in_main or (lambda work: work())
        self._ops = threading.Lock()  # one manual operation, or the start of a job, at a time
        self._session: Session | None = None
        self._sim: simrig.SimulatedDevice | None = None
        self._connecting = False
        self._lock_error: dict[str, Any] | None = None
        self._unconfirmed: set[int] = set()  # LEDs a closed device did not confirm off
        self._check: dict[str, Any] | None = None
        self._job: Job | None = None
        self._job_thread: threading.Thread | None = None
        self._job_count = 0
        self._notices: deque[dict[str, Any]] = deque(maxlen=20)
        self._notice_count = 0
        self._touched = time.monotonic()
        self._clipping = False
        self._picture: _Picture | None = None
        self._picture_lock = threading.Lock()
        self._watchdog: threading.Thread | None = None
        self._closing = threading.Event()

    # --- connection ------------------------------------------------------------------

    def _open_device(
        self, options: dict[str, Any]
    ) -> tuple[Session, simrig.SimulatedDevice | None]:
        if options["simulate"]:
            device = simrig.open_simulated(allow_uv=self.allow_uv)
            return device.session, device
        session = vispek_hc.open_session(
            port=options["port"],
            camera=options["camera"],
            unit=options["unit"],
            allow_uv=self.allow_uv,
            size=options["size"],
            fps=options["fps"],
            source=options["source"],
        )
        return session, None

    def _connect_options(self, given: dict[str, Any]) -> dict[str, Any]:
        unknown = set(given) - {"simulate", "port", "camera", "unit", "size", "fps", "source"}
        if unknown:
            raise _refuse(f"unknown connection option: {', '.join(sorted(unknown))}")
        for key in ("port", "camera"):
            value = given.get(key)
            if value is not None and not (isinstance(value, str) and 0 < len(value) <= 200):
                raise _refuse(f"{key} is a device name", option=key)
        unit = given.get("unit", "hc1500-1")
        if not isinstance(unit, str) or not NAME.fullmatch(unit):
            raise _refuse("the unit label is 1-64 letters, digits, '.', '_' or '-'", option="unit")
        size = given.get("size")
        if size is None:
            size = list(HC1500.default_size)
        if not isinstance(size, list) or tuple(size) not in HC1500.sizes:
            sizes = ", ".join(f"{w}x{h}" for w, h in HC1500.sizes)
            raise _refuse(f"the picture size is one of {sizes}", option="size")
        fps = given.get("fps", HC1500.default_fps)
        if type(fps) not in (int, float) or not 1 <= fps <= 60:
            raise _refuse("the frame rate is a number from 1 to 60", option="fps")
        source = given.get("source", "uncompressed")
        if source not in SOURCES:
            raise _refuse(f"the source is one of {', '.join(SOURCES)}", option="source")
        return {
            "simulate": bool(given.get("simulate")) or self.simulate,
            "port": given.get("port"),
            "camera": given.get("camera"),
            "unit": unit,
            "size": (size[0], size[1]),
            "fps": fps,
            "source": source,
        }

    def connect(self, options: dict[str, Any]) -> dict[str, Any]:
        """Open the device, start its camera and lock the camera parameters."""
        chosen = self._connect_options(options)
        with self._ops:
            if self._session is not None:
                raise InvalidStateError("a device is already connected", hint="disconnect first")

            def enter() -> tuple[Session, simrig.SimulatedDevice | None]:
                session, sim = self._opener(chosen)
                session.__enter__()  # starts the stream; closes everything if that fails
                return session, sim

            self._connecting = True
            try:
                session, sim = self._marshal(enter)
            finally:
                self._connecting = False
            try:
                self._try_lock(session)  # before anyone can see the device as connected
            except BaseException:
                session.__exit__(None, None, None)
                raise
            self._check, self._picture, self._job = None, None, None
            self._job_thread = None  # a job stuck on an earlier device is not this one's
            self._unconfirmed = set()
            self._touched = time.monotonic()
            self._session, self._sim = session, sim
        return self.state()

    def _try_lock(self, session: Session) -> None:
        """Lock the camera parameters if this computer can. A live view works without."""
        try:
            session.lock_controls()
            self._lock_error = None
        except VispekHCError as error:
            self._lock_error = error.to_dict()

    def disconnect(self) -> dict[str, Any]:
        """Stop what runs, switch every LED off, close the device.

        If an OFF is not confirmed, the error goes to the caller, a notice stays, and the
        LEDs concerned are shown as unknown until a device is connected again.
        """
        with self._ops:  # taken first: no job can start between stopping one and closing
            session = self._session
            if session is None:
                return self.state()
            if not self._end_job():
                self._halt(session, "the device is being closed")
            self._session, self._sim, self._picture = None, None, None
            try:
                session.__exit__(None, None, None)
            except VispekHCError as error:
                self._unconfirmed = {
                    led_id for led_id, state in session.light.states.items() if state.state != "off"
                }
                self._notice("error", f"{error.message}. {error.hint or ''}".strip())
                raise
        return self.state()

    def _halt(self, session: Session, why: str) -> list[int]:
        """A job did not stop when told: switch everything off over its head. Until a
        normal all-off succeeds, the light source refuses to light anything. Returns the
        LEDs that did not confirm."""
        unconfirmed = session.light.emergency_off()
        text = f"The running job did not stop in time ({why}); the LEDs were switched off"
        if unconfirmed:
            text += f", but LED {', '.join(map(str, unconfirmed))} did not confirm. {UNPLUG_HINT}"
        self._notice("error", text)
        return unconfirmed

    def close(self) -> None:
        """For the end of the program: never raises, leaves nothing lit that can be put out."""
        self._closing.set()
        try:
            self.disconnect()
        except VispekHCError as error:
            log.error("closing the device: %s", error.message)

    def set_scene(self, name: Any) -> dict[str, Any]:
        """Simulated device only: put the sample or the white board in front of the camera."""
        if self._sim is None:
            raise InvalidStateError("only the simulated device has scenes to choose from")
        if name not in self._sim.scenes:
            raise _refuse(f"the scene is one of {', '.join(self._sim.scenes)}")
        self._sim.show(name)
        return self.state()

    # --- what the page sees ----------------------------------------------------------

    def _notice(self, level: str, text: str) -> None:
        self._notice_count += 1
        self._notices.append(
            {"id": self._notice_count, "level": level, "text": text, "at": time.time()}
        )

    def _leds(self, session: Session | None) -> list[dict[str, Any]]:
        states = session.light.states if session else {}
        profile = session.unit.pwm_by_led if session else {}
        listed = []
        for led in HC1500.led_table.leds:
            state = states.get(led.led_id)
            shown = state.state if state else "off"
            if session is None and led.led_id in self._unconfirmed:
                shown = "unknown"
            listed.append(
                {
                    "led_id": led.led_id,
                    "nm": led.nm,
                    "band_id": led.band_id,
                    "hazard": led.hazard,
                    "allowed": self.allow_uv or not led.needs_uv_permission,
                    "dominant_channel": led.dominant_channel,
                    "state": shown,
                    "pwm": state.pwm if state else None,
                    "scan_pwm": profile.get(led.led_id, led.starting_pwm),
                    "default_pwm": led.starting_pwm,
                }
            )
        return listed

    def _device(self, session: Session) -> dict[str, Any]:
        latest = session.frames.latest()
        report = session.lock_report
        fps = session.frames.measured_fps
        described = session.describe()
        return {
            "simulated": described["simulated"],
            "unit_id": described["unit_id"],
            "serial": described["serial"],
            "camera": described["camera"],
            "controls": described["controls"],
            "size": [latest.width, latest.height] if latest else None,
            "fps": round(fps, 2) if fps else None,
            "stale": session.frames.is_stale(),
            "latency_ms": session.unit.pipeline_latency_ms,
            "locked": _locked(session),
            "gain_state": report.gain_state if report else None,
            "exposure": report.effective.get("exposure-time-abs") if report else None,
            "gain": report.effective.get("gain") if report else None,
            "lock_error": _STALE if session.lock_stale else self._lock_error,
            "check": self._check,
            "ack": session.light.ack_stats(),
            "scene": self._sim.scene if self._sim else None,
            "scenes": list(self._sim.scenes) if self._sim else [],
        }

    @property
    def connected(self) -> bool:
        return self._session is not None

    def devices(self) -> dict[str, Any]:
        """Serial ports and cameras that could be an HC1500. Takes a few seconds."""
        problems = []
        try:
            cameras = video.list_cameras()
        except VispekHCError as error:
            cameras = []
            problems.append(error.to_dict())
        return {
            "serial": vispek_hc.illuminator.find_ports(),
            "cameras": cameras,
            "matching_cameras": video.matching_cameras(cameras),  # by name, or USB id on Linux
            "sizes": [list(size) for size in HC1500.sizes],
            "problems": problems,
        }

    def state(self) -> dict[str, Any]:
        session, job = self._session, self._job
        return {
            "connected": session is not None,
            "connecting": self._connecting,
            "simulate_only": self.simulate,
            "allow_uv": self.allow_uv,
            "device": self._device(session) if session else None,
            "leds": self._leds(session),
            "job": job.to_dict() if job else None,
            "library_revision": self.library.revision,
            "notices": list(self._notices),
            "preview": {"clipping": self._clipping},
        }

    # --- manual control --------------------------------------------------------------

    def _running(self) -> bool:
        return self._job is not None and self._job.state == "running"

    def _idle(self) -> Session:
        """The connected device, if nothing else is using it. Call with ``_ops`` held."""
        if self._session is None:
            raise InvalidStateError("no device is connected", hint="connect first")
        if self._running():
            raise InvalidStateError(
                "a recording is running", hint="wait for it to finish, or stop it"
            )
        return self._session

    def _scan_pwm(self, session: Session, led_id: int) -> int:
        return session.unit.pwm_by_led.get(led_id, session.model.led(led_id).starting_pwm)

    def led(self, led_id: Any, on: bool, pwm: Any = None) -> dict[str, Any]:
        """Light one LED (at its scan PWM unless one is given), or switch it off."""
        if not _whole(led_id) or not (pwm is None or _whole(pwm)):
            raise _refuse("LED number and PWM are whole numbers")
        with self._ops:
            session = self._idle()
            if on:
                # Checked here as well as in the SDK: the permission is the viewer's.
                safety.check_channels(session.model, [led_id], allow_uv=self.allow_uv)
                session.light.on(led_id, self._scan_pwm(session, led_id) if pwm is None else pwm)
            else:
                session.light.off(led_id)
        return self.state()

    def all_off(self) -> dict[str, Any]:
        """Stop a running job and switch every LED off."""
        with self._ops:  # taken first: no job can start between stopping one and the OFFs
            session = self._session
            if session is None:
                raise InvalidStateError("no device is connected", hint="connect first")
            if not self._end_job():
                unconfirmed = self._halt(session, "all LEDs off was asked for")
                told = "the LEDs were switched off over its head"
                if unconfirmed:
                    told += f", but LED {', '.join(map(str, unconfirmed))} did not confirm"
                raise InvalidStateError(
                    f"the running job did not stop in time; {told}",
                    hint="wait a moment, then press All LEDs off again",
                    details={"unconfirmed": unconfirmed},
                )
            session.light.all_off()
        return self.state()

    def set_scan_pwm(self, led_id: Any, pwm: Any) -> dict[str, Any]:
        """Store the PWM this unit scans an LED with; ``None`` goes back to the model's."""
        if not _whole(led_id) or not (pwm is None or _whole(pwm)):
            raise _refuse("LED number and PWM are whole numbers")
        with self._ops:
            session = self._idle()
            check_plan(
                ScanSettings(channels=(led_id,), pwm=pwm), allow_uv=self.allow_uv,
                model=session.model,
            )  # fmt: skip
            if pwm is None:
                session.unit.pwm_by_led.pop(led_id, None)
            else:
                session.unit.pwm_by_led[led_id] = pwm
            session.save_unit()
        return self.state()

    def controls(self) -> dict[str, Any]:
        session = self._session
        if session is None:
            raise InvalidStateError("no device is connected", hint="connect first")
        values = session.controls.read_all() if session.controls else {}
        return {
            "available": session.controls is not None,
            "locked": _locked(session),
            "lock_error": _STALE if session.lock_stale else self._lock_error,
            "editable": list(EDITABLE_CONTROLS),
            "values": {
                name: {
                    "current": info.current,
                    "minimum": info.minimum,
                    "maximum": info.maximum,
                    "step": info.step,
                }
                for name, info in sorted(values.items())
            },
        }

    def set_control(self, name: Any, value: Any) -> dict[str, Any]:
        """Change exposure or gain: stored in the unit's profile and locked again.

        White references recorded with other values no longer match scans made after it.
        """
        if name not in EDITABLE_CONTROLS:
            raise _refuse(
                f"only {' and '.join(EDITABLE_CONTROLS)} can be changed here", control=name
            )
        if not _whole(value):
            raise _refuse(f"{name} is a whole number", control=name)
        with self._ops:
            session = self._idle()
            if session.controls is None:
                raise ControlsUnavailableError(
                    "no way to set camera parameters on this computer",
                    hint="macOS: build tools/uvc-util of vispek-hc. Linux: install v4l-utils",
                )
            previous = dict(session.unit.camera_controls)
            session.unit.camera_controls = {
                **(previous or session.model.initial_controls), name: value
            }  # fmt: skip
            try:
                session.lock_controls()
                self._lock_error = None
            except VispekHCError:
                session.unit.camera_controls = previous
                self._try_lock(session)
                raise
        return self.controls()

    def lock(self) -> dict[str, Any]:
        """Lock the camera parameters again. Every LED goes off first: the lock is taken
        in darkness, and refused (``GAIN_NOT_CONFIRMED``) if the camera sees light."""
        with self._ops:
            session = self._idle()
            try:
                session.lock_controls()
                self._lock_error = None
            except VispekHCError as error:
                self._lock_error = error.to_dict()
                raise
        return self.controls()

    # --- jobs ------------------------------------------------------------------------

    def _settings(self, given: dict[str, Any], white: bool) -> ScanSettings:
        unknown = set(given) - _SCAN_KEYS
        if unknown:
            raise _refuse(f"unknown scan option: {', '.join(sorted(unknown))}")
        channels = given.get("channels")
        if channels is not None and not isinstance(channels, list):
            raise _refuse("channels is a list of LED numbers", option="channels")
        pwm = given.get("pwm")
        if isinstance(pwm, dict):
            try:
                pwm = {int(led_id): value for led_id, value in pwm.items()}
            except (TypeError, ValueError):
                raise _refuse("PWM values are given per LED number", option="pwm") from None
        elif pwm is not None and not _whole(pwm):
            raise _refuse("PWM is a whole number, or one per LED", option="pwm")
        roi = given.get("white_roi")
        if roi is not None:
            if not (
                isinstance(roi, list)
                and len(roi) == 4
                and all(type(number) in (int, float) for number in roi)
            ):
                raise _refuse("the white region is [x, y, w, h] in fractions", option="white_roi")
            roi = (float(roi[0]), float(roi[1]), float(roi[2]), float(roi[3]))
        label = given.get("setup_label")
        if label is not None and not (isinstance(label, str) and len(label) <= 120):
            raise _refuse("the setup label is a short text", option="setup_label")
        return ScanSettings(
            channels=tuple(channels) if channels is not None else None,
            pwm=pwm,
            min_on_ms=given.get("min_on_ms", "auto"),
            average=given.get("average", 1),
            reduction=given.get("reduction", "dominant"),
            white_roi=roi,
            require_awb_off=not given.get("unlocked", False),
            role="white" if white else None,
            setup_label=label or None,
            enclosure=given.get("enclosure", "unknown"),
        )

    def _scan_work(
        self, kind: str, given: dict[str, Any], session: Session
    ) -> tuple[str, Callable[[Job], None]]:
        white = kind in ("white", "tune")
        settings = self._settings(given, white)
        check_plan(
            settings, allow_uv=self.allow_uv, model=session.model,
            profile_pwm=session.unit.pwm_by_led,
        )  # fmt: skip
        name, path = self.library.reserve(given.get("name"), kind)
        calibrate = white and bool(given.get("calibrate", True))
        calibration_name = given.get("calibration_name", name)
        if calibrate:
            taken = self.library.root / "calibrations" / str(calibration_name)
            if not isinstance(calibration_name, str) or not NAME.fullmatch(calibration_name):
                raise _refuse("the calibration name is 1-64 letters, digits, '.', '_' or '-'")
            if taken.exists() or taken.is_symlink():
                raise InvalidStateError(
                    f"a calibration named '{calibration_name}' already exists",
                    hint="choose another name",
                )
        search = kind == "white" and bool(given.get("auto_pwm"))

        def work(job: Job) -> None:
            used = settings
            if kind == "tune":
                job.phase = "Measuring timing"
                session.pair_probe(relearn=True)  # tuning learns the timing of this unit afresh
                job.phase = "Searching PWM and scanning"
                vispek_hc.tune(session, path, used, stop=job.stop, notes=job.warnings)
            else:
                if session.unit.pipeline_latency_ms is None:
                    job.phase = "Measuring timing"
                    if not session.pair_probe().paired:
                        job.warnings.append(
                            "the camera did not react to the probe LED; timing falls back to "
                            "a safe value"
                        )
                chosen = None
                if search:
                    job.phase = "Searching PWM"
                    chosen = vispek_hc.auto_pwm(session, used, job.stop, job.warnings, store=False)
                    used = replace(used, pwm=chosen)
                job.phase = "Scanning"
                vispek_hc.record(session, path, used, stop=job.stop, progress=job.progress)
                if chosen:  # kept only now: a refused reference leaves the profile alone
                    keep_pwm(session, chosen)
            job.warnings.extend(scan_warnings(path))
            if calibrate:
                job.phase = "Building the calibration"
                job.calibration = self.library.make_calibration(
                    [name], calibration_name, settings.white_roi
                )

        return name, work

    def _frame_work(
        self, kind: str, given: dict[str, Any], session: Session
    ) -> tuple[str, Callable[[Job], None]]:
        unknown = set(given) - {"name", "setup_label", "enclosure"}
        if unknown:
            raise _refuse(f"unknown option: {', '.join(sorted(unknown))}")
        described = self._settings(
            {key: given[key] for key in ("setup_label", "enclosure") if key in given}, False
        )
        name, path = self.library.reserve(given.get("name"), kind)

        def work(job: Job) -> None:
            job.phase = "Taking a frame"
            vispek_hc.record_frames(
                session, path, kind,  # type: ignore[arg-type]
                stop=job.stop, setup_label=described.setup_label,
                enclosure=described.enclosure,
            )  # fmt: skip

        return name, work

    def _check_work(self, session: Session) -> Callable[[Job], None]:
        def work(job: Job) -> None:
            job.phase = "Blinking LED 6"
            pair = session.pair_probe()
            job.phase = "Measuring the black level"
            black = session.black_level()
            self._check = {
                "paired": pair.paired,
                "rise_dn": round(pair.rise_dn, 1),
                "latency_ms": round(pair.latency_s * 1000, 1) if pair.latency_s else None,
                "black_level": list(black),
                "expected_black_level": session.model.black_level,
            }
            if not pair.paired:
                job.warnings.append("the camera did not react to LED 6")
            if any(level != session.model.black_level for level in black):
                job.warnings.append(
                    f"the black level is {list(black)}, not {session.model.black_level}: "
                    "light is getting in"
                )

        return work

    def start_job(self, kind: Any, given: dict[str, Any]) -> dict[str, Any]:
        """Check the request, then start it on its own thread. Refused while another runs."""
        if kind not in JOB_KINDS:
            raise _refuse(f"a job is one of {', '.join(JOB_KINDS)}", kind=kind)
        with self._ops:
            session = self._idle()
            name: str | None = None
            if kind in ("scan", "white", "tune"):
                name, work = self._scan_work(kind, given, session)
            elif kind in ("single", "dark"):
                name, work = self._frame_work(kind, given, session)
            else:
                work = self._check_work(session)
            self._job_count += 1
            job = Job(self._job_count, kind, capture=name)
            self._job = job
            self._job_thread = threading.Thread(
                target=self._run, args=(job, session, work), name="vispek-hc-job", daemon=True
            )
            self._job_thread.start()
            return job.to_dict()

    def _run(self, job: Job, session: Session, work: Callable[[Job], None]) -> None:
        state = "failed"
        try:
            work(job)
            state = "done"
        except OperationCancelledError:
            state = "cancelled"
        except VispekHCError as error:
            job.error = error.to_dict()
        except Exception as error:
            log.exception("a %s job failed unexpectedly", job.kind)
            job.error = {
                "code": "INTERNAL_ERROR",
                "message": f"unexpected {type(error).__name__}; see the viewer's log",
                "hint": None,
                "details": {},
            }
        finally:
            try:
                if state != "done":
                    # Whatever went wrong, nothing may stay lit, and the page must be
                    # told if that cannot be confirmed.
                    try:
                        session.light.all_off()
                    except VispekHCError as error:
                        job.warnings.append(error.message)
                        self._notice("error", f"{error.message}. {error.hint or ''}".strip())
                    except Exception as error:
                        log.exception("switching the LEDs off after a %s job failed", job.kind)
                        job.warnings.append("the LEDs could not be switched off")
                        self._notice(
                            "error",
                            f"After the {job.kind} job the LEDs could not be switched off "
                            f"({type(error).__name__}). {UNPLUG_HINT}",
                        )
                self.library.changed()
            finally:  # a job never stays "running", whatever the clean-up did
                job.phase = ""
                job.finished = time.time()
                self._touched = time.monotonic()
                job.state = state

    def stop_job(self) -> dict[str, Any]:
        job = self._job
        if job is not None and job.state == "running":
            job.stop.set()
        return self.state()

    def wait_job(self, timeout: float | None = None) -> dict[str, Any] | None:
        thread, job = self._job_thread, self._job
        if thread is not None:
            thread.join(timeout)
        return job.to_dict() if job else None

    def _end_job(self) -> bool:
        """Ask the running job to stop and wait for it. False if it is still running."""
        self.stop_job()
        thread = self._job_thread
        if thread is not None:
            thread.join(JOB_END_WAIT_S)
            return not thread.is_alive()
        return True

    # --- the live picture ------------------------------------------------------------

    def set_preview(self, options: dict[str, Any]) -> dict[str, Any]:
        clipping = options.get("clipping", self._clipping)
        if set(options) - {"clipping"} or type(clipping) is not bool:
            raise _refuse("the live picture has one option: clipping (true or false)")
        self._clipping = clipping
        return self.state()

    def _latest_picture(self, session: Session) -> _Picture | None:
        frame = session.frames.latest()
        if frame is None:
            return None
        key = f"{frame.generation}.{frame.seq}.{int(self._clipping)}"
        with self._picture_lock:
            if self._picture is not None and self._picture.key == key:
                return self._picture
            rgb = video.decode(frame)
            height, width = rgb.shape[:2]
            thin = rgb[:: max(1, height // 180), :: max(1, width // 320)].reshape(-1, 3)
            fps = session.frames.measured_fps
            stats = {
                "seq": frame.seq,
                "size": [width, height],
                "fps": round(fps, 2) if fps else None,
                "mean": [round(float(v), 2) for v in rgb.mean(axis=(0, 1))],
                "max": [int(v) for v in rgb.max(axis=(0, 1))],
                "saturated": round(framecheck.saturated_fraction(rgb), 5),
                "sharpness": _sharpness(rgb),
                "histogram": [
                    np.bincount(thin[:, channel] >> 2, minlength=HISTOGRAM_BINS).tolist()
                    for channel in range(3)
                ],
            }
            shown = rgb
            if self._clipping:
                shown = rgb.copy()
                shown[(rgb >= framecheck.CLIP_LEVEL).any(axis=2)] = CLIPPED_COLOUR
            buffer = io.BytesIO()
            Image.fromarray(shown).save(buffer, "JPEG", quality=JPEG_QUALITY)
            self._picture = _Picture(key, buffer.getvalue(), rgb, stats)
            return self._picture

    def preview(self, after: str | None, timeout: float) -> tuple[str, bytes] | None:
        """The newest live picture as JPEG once it differs from ``after``; None in time."""
        deadline = time.monotonic() + timeout
        while not self._closing.is_set():
            session = self._session
            if session is None:
                return None
            picture = self._latest_picture(session)
            if picture is not None and picture.key != after:
                return picture.key, picture.jpeg
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.01)
        return None

    def live(self, probe: dict[str, Any] | None = None) -> dict[str, Any]:
        """Numbers of the newest live picture, and of a region of it if one is asked for."""
        session = self._session
        if session is None:
            raise InvalidStateError("no device is connected", hint="connect first")
        picture = self._latest_picture(session)
        if picture is None:
            return {"size": None}
        told = dict(picture.stats)
        if probe is not None:
            height, width = picture.rgb.shape[:2]
            numbers = [probe.get("x"), probe.get("y"), probe.get("w", 1), probe.get("h", 1)]
            if not all(_whole(number) for number in numbers):
                raise _refuse("a region is x, y, w, h in whole pixels")
            x, y, w, h = (int(number) for number in numbers)
            if not (0 <= x < width and 0 <= y < height and w >= 1 and h >= 1):
                raise _refuse("the region lies outside the picture", size=[width, height])
            region = picture.rgb[y : y + h, x : x + w].reshape(-1, 3)
            told["probe"] = {
                "x": x, "y": y, "w": w, "h": h,
                "pixels": int(region.shape[0]),
                "rgb": [round(float(v), 2) for v in region.mean(axis=0)],
                "min": [int(v) for v in region.min(axis=0)],
                "max": [int(v) for v in region.max(axis=0)],
            }  # fmt: skip
        return told

    # --- nobody watching -------------------------------------------------------------

    def touch(self, now: float | None = None) -> None:
        """Somebody asked for something: the page is alive."""
        self._touched = time.monotonic() if now is None else now

    def idle_check(self, now: float | None = None) -> None:
        """Switch off LEDs lit by hand when no request has come for ``IDLE_OFF_S``."""
        session = self._session
        moment = time.monotonic() if now is None else now
        if session is None or self._running() or moment - self._touched <= IDLE_OFF_S:
            return
        if all(state.state == "off" for state in session.light.states.values()):
            return
        with self._ops:
            if self._session is not session or self._running():
                return
            try:
                session.light.all_off()
                self._notice(
                    "warning",
                    f"LEDs were switched off: no page has been in contact for {IDLE_OFF_S:g} s",
                )
            except VispekHCError as error:
                self._notice("error", f"{error.message}. {error.hint or ''}".strip())

    def start_watchdog(self, interval_s: float = 1.0) -> None:
        def watch() -> None:
            while not self._closing.wait(interval_s):
                try:
                    self.idle_check()
                except Exception:
                    log.exception("the idle check failed")

        self._watchdog = threading.Thread(target=watch, name="vispek-hc-idle", daemon=True)
        self._watchdog.start()
