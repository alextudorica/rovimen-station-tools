"""Selecting a camera's RMS capture log by the night it belongs to.

RMS writes the only record of what the capture pipeline actually did: the
`Dropped frames: N (last 10 min), M this session` counter and the
`Frame grabbing failed, video device is probably disconnected!` lines that say
whether a night was lost to the camera or to the host.

Until now `/api/logs/rms-<CAM>` returned whichever log had the newest mtime.
During the day that is an idle EventMonitor process still holding a handle, so
the counter for the night you are diagnosing was unreachable through the API
and the only way to read it was SSH. That is backwards: the API exists so that
fleet diagnosis does not need station access.

Selection by name, not by mtime, is the whole point. A log from any night can
be touched later by a process that still has it open, so mtime cannot identify
which night a file belongs to -- only the date RMS stamped into the filename
can. Within one date RMS rotates (`_001`, `_002`, ...), and the last rotation
holds the end of the night, so the tie-break is the name too.
"""

from __future__ import annotations

import os

import pytest

import station_api


def _touch(path, when):
    """Set an explicit mtime.

    The tests turn on which file is 'most recently modified', so the mtimes
    have to be set outright -- files written microseconds apart can tie at the
    filesystem's timestamp resolution and make the assertion flap.
    """
    os.utime(path, (when, when))


def _make_logs(tmp_path, names, per_camera=True, station_id="RO000E"):
    """Lay out a station's RMS tree and return a config pointing at it."""
    rms_data = tmp_path / "RMS_data" / ("cam2" if per_camera else "")
    logs = (rms_data if per_camera else tmp_path / "RMS_data") / "logs"
    if not per_camera:
        logs = tmp_path / "RMS_data" / "logs"
        rms_data = tmp_path / "RMS_data" / "cam2"
    logs.mkdir(parents=True, exist_ok=True)
    rms_data.mkdir(parents=True, exist_ok=True)
    for i, name in enumerate(sorted(names)):
        f = logs / name
        f.write_text(name + "\n")
        _touch(f, 1_700_000_000 + i)
    return {"stations": {station_id: {"rms_data_path": str(rms_data)}}}


# Two nights, the later one rotated twice, plus today's fresh session.
NIGHT_LOGS = [
    "log_RO000E_20260920_081451_001.log",
    "log_RO000E_20260921_081451_001.log",
    "log_RO000E_20260921_081451_002.log",
    "log_RO000E_20260922_120027_001.log",
]


class TestFindByDate:
    def test_without_a_date_the_newest_mtime_still_wins(self, tmp_path):
        """Unchanged behaviour: no date means "what is happening now"."""
        cfg = _make_logs(tmp_path, NIGHT_LOGS)
        logs_dir = tmp_path / "RMS_data" / "cam2" / "logs"
        target = logs_dir / "log_RO000E_20260920_081451_001.log"
        _touch(target, 1_800_000_000)  # an OLD night, most recently modified
        found = station_api._find_latest_rms_log("RO000E", cfg)
        assert found is not None
        assert found.name == target.name

    def test_a_date_selects_that_night_despite_mtime(self, tmp_path):
        """The trap this endpoint existed with: an idle process touching an
        unrelated log must not decide which night you get."""
        cfg = _make_logs(tmp_path, NIGHT_LOGS)
        logs_dir = tmp_path / "RMS_data" / "cam2" / "logs"
        _touch(logs_dir / "log_RO000E_20260922_120027_001.log", 1_800_000_000)
        found = station_api._find_latest_rms_log("RO000E", cfg, date="20260921")
        assert found is not None
        assert found.name == "log_RO000E_20260921_081451_002.log"

    def test_last_rotation_of_the_night_wins(self, tmp_path):
        """`_002` holds the end of the night; `_001` stops at the rotation."""
        cfg = _make_logs(tmp_path, NIGHT_LOGS)
        logs_dir = tmp_path / "RMS_data" / "cam2" / "logs"
        _touch(logs_dir / "log_RO000E_20260921_081451_001.log", 1_800_000_000)
        found = station_api._find_latest_rms_log("RO000E", cfg, date="20260921")
        assert found.name.endswith("_002.log")

    def test_unknown_date_returns_nothing_rather_than_a_wrong_night(self, tmp_path):
        cfg = _make_logs(tmp_path, NIGHT_LOGS)
        assert station_api._find_latest_rms_log("RO000E", cfg, date="20260101") is None

    def test_shared_logs_dir_is_filtered_by_station(self, tmp_path):
        """On a shared logs dir every camera's logs sit together, so the
        station id has to be part of the match or one camera answers for
        another."""
        logs = tmp_path / "RMS_data" / "logs"
        logs.mkdir(parents=True)
        (tmp_path / "RMS_data" / "cam2").mkdir(parents=True)
        for name in ("log_RO000E_20260921_081451_001.log",
                     "log_RO000G_20260921_081451_001.log"):
            (logs / name).write_text(name)
        cfg = {"stations": {"RO000E": {
            "rms_data_path": str(tmp_path / "RMS_data" / "cam2")}}}
        found = station_api._find_latest_rms_log("RO000E", cfg, date="20260921")
        assert found is not None
        assert "RO000E" in found.name

    def test_no_rms_data_path_is_not_an_error(self, tmp_path):
        assert station_api._find_latest_rms_log("RO000E", {"stations": {}}) is None


class TestAvailableDates:
    def test_dates_are_listed_newest_first_and_deduped(self, tmp_path):
        """A rotated night must appear once, not once per rotation."""
        cfg = _make_logs(tmp_path, NIGHT_LOGS)
        assert station_api._rms_log_dates("RO000E", cfg) == [
            "20260922", "20260921", "20260920",
        ]

    def test_missing_tree_gives_an_empty_list(self, tmp_path):
        assert station_api._rms_log_dates("RO000E", {"stations": {}}) == []


class TestRoute:
    """The endpoint contract, including the rejection of a malformed date."""

    @pytest.fixture()
    def client(self):
        station_api.app.config["TESTING"] = True
        return station_api.app.test_client()

    @pytest.mark.parametrize("bad", ["2026-09-21", "2026092", "notadate", "202609211"])
    def test_malformed_date_is_rejected(self, client, bad, monkeypatch, tmp_path):
        monkeypatch.setattr(station_api, "load_config",
                            lambda: _make_logs(tmp_path, NIGHT_LOGS))
        r = client.get("/api/logs/rms-RO000E?date=%s" % bad)
        assert r.status_code == 400
        assert "YYYYMMDD" in r.get_json()["error"]

    def test_unknown_date_reports_what_is_available(self, client, monkeypatch, tmp_path):
        """A miss has to say which nights DO exist, or the caller is guessing."""
        monkeypatch.setattr(station_api, "load_config",
                            lambda: _make_logs(tmp_path, NIGHT_LOGS))
        body = client.get("/api/logs/rms-RO000E?date=20260101").get_json()
        assert body["lines"] == []
        assert "20260101" in body["note"]
        assert "20260921" in body["available_dates"]

    def test_hit_returns_the_named_log(self, client, monkeypatch, tmp_path):
        monkeypatch.setattr(station_api, "load_config",
                            lambda: _make_logs(tmp_path, NIGHT_LOGS))
        body = client.get("/api/logs/rms-RO000E?date=20260921").get_json()
        assert body["log_file"] == "log_RO000E_20260921_081451_002.log"
        assert body["date"] == "20260921"
