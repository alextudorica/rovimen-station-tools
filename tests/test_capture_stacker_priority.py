"""Colour capture's realtime stacker child runs at low CPU and disk priority
without a ``preexec_fn``.

``color_capture.py`` is a multi-threaded daemon; a ``preexec_fn`` that called
``os.nice`` and spawned ``ionice`` from the forked child is unsafe there (the
Python docs warn the child can deadlock), and a deadlocked child cost the
stacker lane its 120 s timeout. The priority now comes from the argv.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from color_capture import ColorCapture

STATION = "RO0019"


@pytest.fixture()
def capture(tmp_path: Path) -> ColorCapture:
    cfg = {
        "color_capture_path": str(tmp_path / "color"),
        "log_path": str(tmp_path / "logs"),
        "stations": {STATION: {"camera_rtsp": "rtsp://cam/stream_0"}},
    }
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg))
    return ColorCapture(str(cfg_path))


def _launch(capture: ColorCapture, monkeypatch, tmp_path: Path, cmd: list[str]) -> dict:
    seen: dict = {}

    class _Proc:
        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    def fake_popen(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        capture._running = False
        return _Proc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    capture._running = True
    capture._stacker_queue.put((STATION, tmp_path / "chunk.mkv", cmd))
    capture._stacker_worker()
    return seen


def test_the_stacker_runs_under_ionice_idle_and_nice(capture, monkeypatch, tmp_path):
    cmd = ["/venv/bin/python", "/s/stacker.py", "/v/chunk.mkv"]
    seen = _launch(capture, monkeypatch, tmp_path, cmd)
    assert seen["argv"] == ["ionice", "-c", "3", "nice", "-n", "19", *cmd]
    assert "preexec_fn" not in seen["kwargs"]


def test_the_configured_nice_value_is_used(capture, monkeypatch, tmp_path):
    capture.cfg["services"] = {"stacker": {"nice": 10}}
    seen = _launch(capture, monkeypatch, tmp_path, ["stacker"])
    assert seen["argv"] == ["ionice", "-c", "3", "nice", "-n", "10", "stacker"]
