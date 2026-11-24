"""archive_upload.py waits while the station is on the rescue Wi-Fi.

The rescue Wi-Fi (#1432) is a phone hotspot an operator brings to a station
whose cable uplink failed. gmnde00 (Johannismühle) uploads about 19.5 GB a
night, which would drain a phone plan, so rovimen_watchdog.sh keeps
~/rovimen_scripts/rescue_wifi_active fresh while the rescue is up and the
upload passes skip while it exists. A flag older than 15 minutes means the
watchdog stopped writing it and is ignored.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

import archive_upload


@pytest.fixture()
def flag(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "rescue_wifi_active"
    monkeypatch.setattr(archive_upload, "_rescue_wifi_path", lambda: path)
    return path


@pytest.fixture()
def uploader(monkeypatch):
    up = archive_upload._Uploader.__new__(archive_upload._Uploader)
    passes: list[str] = []
    monkeypatch.setattr(up, "_ready_to_upload", lambda: True, raising=False)
    monkeypatch.setattr(up, "_run_pass", lambda body, abort=None: passes.append("pass") or 7, raising=False)
    up.passes = passes
    return up


def test_no_flag_uploads(flag, uploader):
    assert uploader.cycle() == 7
    assert uploader.cycle_night("RO0001", "20261120") == 7
    assert len(uploader.passes) == 2


def test_fresh_flag_skips_both_passes_with_one_line_each(flag, uploader, caplog):
    flag.write_text("2026-11-21T10:00:00Z rescue Wi-Fi up on wlp0s20f3\n")
    caplog.set_level("INFO", logger=archive_upload.logger.name)
    assert uploader.cycle() == 0
    assert uploader.cycle_night("RO0001", "20261120") == 0
    assert uploader.passes == []
    lines = [r.getMessage() for r in caplog.records if "Rescue Wi-Fi is up" in r.getMessage()]
    assert len(lines) == 2


def test_stale_flag_is_ignored(flag, uploader):
    flag.write_text("left over\n")
    old = time.time() - archive_upload._RESCUE_FLAG_MAX_AGE_S - 60
    os.utime(flag, (old, old))
    assert uploader.cycle() == 7
