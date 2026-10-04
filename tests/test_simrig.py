# SPDX-License-Identifier: Apache-2.0
"""The simulated device as the viewer uses it: in real time, from several threads."""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest
from vispek_hc import video
from vispek_hc.errors import FrameIncompleteError

from vispek_hc_viewer import simrig


def test_the_clock_moves_with_real_time() -> None:
    clock = simrig.RealTimeClock()
    started = clock.now()
    time.sleep(0.05)
    assert 0.04 < clock.now() - started < 5
    before = time.monotonic()
    clock.advance_to(clock.now() + 0.05)
    assert time.monotonic() - before >= 0.045
    clock.advance_to(0.0)  # a moment already past: no waiting, no going back
    assert clock.now() >= started


def test_frames_arrive_at_the_frame_rate_without_anyone_asking() -> None:
    device = simrig.open_simulated(fps=50.0, size=(64, 36))
    with device.session as session:
        first = session.frames.latest()
        assert first is not None, "entering the session waited for the first frame"
        started = time.monotonic()
        time.sleep(0.2)
        later = session.frames.latest()
        elapsed = time.monotonic() - started  # a busy machine sleeps longer than asked
        assert later is not None
        assert 50.0 * elapsed - 3 <= later.seq - first.seq <= 50.0 * elapsed + 3


def test_wait_new_waits_for_frames_and_gives_up_in_time() -> None:
    device = simrig.open_simulated(fps=50.0, size=(64, 36))
    with device.session as session:
        seen = session.frames.wait_new()
        assert session.frames.wait_new(3).seq >= seen.seq + 3
        with pytest.raises(FrameIncompleteError):
            session.frames.wait_new(50, timeout=0.05)


def test_an_led_shows_in_the_picture_and_the_scene_can_be_swapped() -> None:
    device = simrig.open_simulated(fps=50.0, size=(64, 36))
    with device.session as session:
        assert device.scene == "sample"
        session.light.on(9, 36)
        try:
            sample = video.decode(session.frames.wait_new(15))
            device.show("white")
            white = video.decode(session.frames.wait_new(15))
        finally:
            session.light.off(9)
        assert device.scene == "white"
        assert white[..., 0].mean() > sample[..., 0].mean() + 5, "the board is brighter"
        assert np.ptp(sample[..., 0]) > 30, "the sample has patches"
        with pytest.raises(KeyError):
            device.show("moon")


def test_two_threads_can_read_frames_while_leds_switch() -> None:
    device = simrig.open_simulated(fps=100.0, size=(64, 36))
    failures: list[BaseException] = []
    with device.session as session:
        done = threading.Event()

        def watch() -> None:
            try:
                while not done.is_set():
                    frame = session.frames.latest()
                    assert frame is not None
                    video.decode(frame)
            except BaseException as error:
                failures.append(error)

        watcher = threading.Thread(target=watch)
        watcher.start()
        try:
            for led_id in (5, 6, 7, 8, 9):
                session.light.on(led_id, 50)
                session.frames.wait_new(2)
                session.light.off(led_id)
        finally:
            done.set()
            watcher.join()
    assert not failures


def test_ultraviolet_stays_gated() -> None:
    from vispek_hc.errors import UVNotAllowedError

    device = simrig.open_simulated(fps=50.0, size=(64, 36))
    with device.session as session, pytest.raises(UVNotAllowedError):
        session.light.on(1, 10)
