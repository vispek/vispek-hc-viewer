# SPDX-License-Identifier: Apache-2.0
"""The simulated HC1500 of ``vispek_hc.sim``, run in real time for the viewer.

The simulated device of the SDK lives on a virtual clock, so that a scan in a test takes
milliseconds. A person watching a live view needs the opposite: frames that arrive ten
times a second and an LED that stays lit while they look. Here the same simulated light
source and camera run on a clock that follows real time, and the camera may be read from
several threads, as the real one may.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import numpy as np
from numpy.typing import NDArray
from vispek_hc import HC1500, Illuminator, Session, samples, video
from vispek_hc.errors import FrameIncompleteError
from vispek_hc.interfaces import Frame
from vispek_hc.sim import SimControls, SimFrameSource, SimLightSource, VirtualClock
from vispek_hc.units import UnitProfile

POLL_S = 0.004
DEFAULT_SIZE = (640, 360)


class RealTimeClock(VirtualClock):
    """The clock of the simulated device, following real time instead of being pushed."""

    def __init__(self) -> None:
        super().__init__(start_utc=datetime.now(UTC))
        self._origin = time.monotonic()

    def now(self) -> float:
        return time.monotonic() - self._origin

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)

    def advance_to(self, when: float) -> None:
        self.sleep(when - self.now())

    def utc(self) -> datetime:
        return self.utc_at(self.now())


class SharedFrames:
    """A simulated camera that several threads may read, and that never waits while
    holding its lock: a live view goes on while a scan waits for its frames."""

    def __init__(self, inner: SimFrameSource, clock: RealTimeClock) -> None:
        self._inner = inner
        self._clock = clock
        self._lock = threading.Lock()

    @property
    def generation(self) -> int:
        return self._inner.generation

    @generation.setter
    def generation(self, value: int) -> None:
        self._inner.generation = value

    @property
    def measured_fps(self) -> float | None:
        return self._inner.measured_fps

    def start(self) -> None:
        with self._lock:
            self._inner.start()

    def stop(self) -> None:
        with self._lock:
            self._inner.stop()

    def latest(self) -> Frame | None:
        with self._lock:
            return self._inner.latest()

    def is_stale(self) -> bool:
        with self._lock:
            return self._inner.is_stale()

    def show(self, reflectance: NDArray[np.float64] | float) -> None:
        with self._lock:
            self._inner.show(reflectance)

    def wait_new(self, count: int = 1, timeout: float | None = None) -> Frame:
        first = self.latest()
        baseline = first.seq if first else 0
        if timeout is None:
            timeout = video.wait_timeout(count, self.measured_fps)
        deadline = self._clock.now() + timeout
        while True:
            frame = self.latest()
            arrived = (frame.seq if frame else 0) - baseline
            if frame is not None and arrived >= count:
                return frame
            if self._clock.now() >= deadline:
                raise FrameIncompleteError(
                    f"{arrived} of {count} new frames arrived in time",
                    details={"wanted": count, "arrived": arrived},
                )
            time.sleep(POLL_S)


def _resampled(layers: NDArray[np.float64], size: tuple[int, int]) -> NDArray[np.float64]:
    """The invented scene of the SDK at another picture size (nearest neighbour)."""
    width, height = size
    rows = np.arange(height) * layers.shape[0] // height
    columns = np.arange(width) * layers.shape[1] // width
    picked: NDArray[np.float64] = layers[rows][:, columns]
    return picked


@dataclass
class SimulatedDevice:
    """A simulated HC1500 and what stands in front of its camera."""

    session: Session
    frames: SharedFrames
    scenes: dict[str, NDArray[np.float64]]
    scene: str = "sample"

    def show(self, name: str) -> None:
        """Put the sample or the white board in front of the camera."""
        self.frames.show(self.scenes[name])
        self.scene = name


def open_simulated(
    *, allow_uv: bool = False, size: tuple[int, int] = DEFAULT_SIZE, fps: float = 10.0
) -> SimulatedDevice:
    """A simulated device in real time. Enter ``session`` to start it, as with a real one."""
    clock = RealTimeClock()
    serial_side = SimLightSource(clock, reply_delay_s=0.008)
    light = Illuminator(serial_side, allow_uv=allow_uv, clock=clock, port="sim")
    light.connect(baud=115200)
    light_falloff = _resampled(samples.illumination(), size)
    scenes = {
        "sample": _resampled(samples.scene(), size) * light_falloff[:, :, None],
        "white": light_falloff,
    }
    inner = SimFrameSource(
        clock, serial_side, size=size, fps=fps, latency_s=0.150, reflectance=scenes["sample"]
    )
    controls = SimControls()
    inner.on_restart = controls.reset_to_automatic
    frames = SharedFrames(inner, clock)
    camera: dict[str, Any] = {
        "name": "simulated",
        "backend": "sim",
        "source": "uncompressed",
        "requested_size": list(size),
        "requested_fps": fps,
    }
    session = Session(
        light,
        frames,
        controls,
        unit=UnitProfile("sim"),
        clock=clock,
        model=HC1500,
        persist=False,
        simulated=True,
        camera_info=camera,
        controls_info={"backend": "sim", "device_id": "sim"},
    )
    return SimulatedDevice(session, frames, scenes)
