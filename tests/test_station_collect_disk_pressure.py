"""station_collect._janitor_pressure: the janitor's unrelieved disk-pressure
episode on /api/status (and so in the pusher's heartbeat), for the dashboard's
Storage banner."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import station_collect as sc

PRESSURE = {"since": "2026-10-16T18:00:02+00:00", "runs": 25,
            "filesystem": "/home/gmn/color_capture", "pct": 98, "free_gb": 3.6}


def _state(tmp_path, monkeypatch, *, age_min: float = 5, pressure=PRESSURE, raw: str | None = None):
    path = tmp_path / "janitor_state.json"
    last = (datetime.now(timezone.utc) - timedelta(minutes=age_min)).isoformat()
    path.write_text(raw if raw is not None else json.dumps({"last_run": last, "pressure": pressure}))
    monkeypatch.setattr(sc, "_JANITOR_STATE", path)


def test_ongoing_episode_is_reported(tmp_path, monkeypatch):
    _state(tmp_path, monkeypatch)
    assert sc._janitor_pressure() == PRESSURE


def test_relieved_disk_reports_none(tmp_path, monkeypatch):
    _state(tmp_path, monkeypatch, pressure=None)
    assert sc._janitor_pressure() is None


def test_state_from_a_janitor_that_stopped_is_ignored(tmp_path, monkeypatch):
    _state(tmp_path, monkeypatch, age_min=120)
    assert sc._janitor_pressure() is None


def test_missing_or_broken_state_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "_JANITOR_STATE", tmp_path / "absent.json")
    assert sc._janitor_pressure() is None
    _state(tmp_path, monkeypatch, raw="{oops")
    assert sc._janitor_pressure() is None


def test_bundle_without_the_field_is_none(tmp_path, monkeypatch):
    _state(tmp_path, monkeypatch, raw=json.dumps({"last_run": datetime.now(timezone.utc).isoformat()}))
    assert sc._janitor_pressure() is None
