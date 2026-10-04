# SPDX-License-Identifier: Apache-2.0
"""Tests never touch a real device: no serial port, no camera process."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest
import serial


@pytest.fixture(autouse=True)
def no_real_devices(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a test tried to reach a real device")

    monkeypatch.setattr(serial, "Serial", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setenv("VISPEK_HC_HOME", str(tmp_path / "home"))
