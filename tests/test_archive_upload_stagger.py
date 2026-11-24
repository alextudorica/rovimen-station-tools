"""The dawn upload stagger spreads the fleet's post-night burst over an hour.

Background: every station's night ends within minutes of every other's, and
dawn_process (cron ``*/15 4-12``) then uploads the finished night for every
camera in parallel. Measured on the VPS 2026-09-09, the whole fleet arriving at
once drove blob POSTs to 2,518 per 5-minute bucket, ~375 MiB/min of arrivals,
a 20 GB ingest spool on the 38 GB boot disk, and 3.3 GB of free space left.

These tests pin the properties that make the stagger safe to run fleet-wide:
an even spread, stability across restarts, no effect outside the dawn window,
and a hard guarantee that every station's slot does open.
"""

from __future__ import annotations

import time
from statistics import mean

import pytest

import archive_upload


# The real fleet, so the spread assertions are about the actual host keys.
FLEET = [
    "gmnro01", "gmnro02", "gmnro03", "gmnro04", "gmnro05", "gmnro06",
    "gmnro07", "gmnro08", "gmnro09", "gmnro16", "gmnro17", "gmnro18",
    "gmnmd01", "gmnde00", "gmnde01",
]

WINDOW_S = 3600.0


def _cfg(**archive) -> dict:
    return {"archive": {"enabled": True, **archive}}


def _at(hour: int, minute: int = 0, day: int = 9) -> float:
    """Epoch for a station-local wall-clock time on 2026-09-<day>."""
    return time.mktime((2026, 9, day, hour, minute, 0, 0, 0, -1))


# ── The offset itself ─────────────────────────────────────────────────────

class TestOffset:
    def test_offset_is_inside_the_window(self):
        for host in FLEET:
            offset = archive_upload._stagger_offset_s(host, WINDOW_S)
            assert 0.0 <= offset < WINDOW_S

    def test_offset_is_stable_across_calls(self):
        """A restart must not reshuffle a station -- that is what makes the
        fleet-wide spread hold night after night."""
        first = [archive_upload._stagger_offset_s(h, WINDOW_S) for h in FLEET]
        second = [archive_upload._stagger_offset_s(h, WINDOW_S) for h in FLEET]
        assert first == second

    def test_offsets_differ_between_stations(self):
        offsets = {archive_upload._stagger_offset_s(h, WINDOW_S) for h in FLEET}
        assert len(offsets) == len(FLEET)

    def test_offsets_spread_across_the_window_rather_than_clumping(self):
        """The point of the change: the fleet must occupy the whole window, not
        pile into one corner of it."""
        offsets = sorted(
            archive_upload._stagger_offset_s(h, WINDOW_S) for h in FLEET
        )
        # Every 15-minute quarter of the hour gets at least one station.
        quarters = {int(o // 900) for o in offsets}
        assert quarters == {0, 1, 2, 3}, f"uneven spread: {quarters}"
        # And no two stations land within 30 s of each other. A plain hash of
        # the host key fails this (measured 18 s on the real fleet keys) --
        # hence the low-discrepancy sequence in _stagger_fraction.
        gaps = [b - a for a, b in zip(offsets, offsets[1:])]
        assert min(gaps) > 30, f"stations too close together: {min(gaps):.0f}s"
        # Mean offset near the middle of the window (uniform, not skewed).
        assert WINDOW_S * 0.3 < mean(offsets) < WINDOW_S * 0.7

    def test_zero_window_disables_the_stagger(self):
        assert archive_upload._stagger_offset_s("gmnro03", 0.0) == 0.0

    def test_jitter_stays_within_its_bound_and_changes_daily(self):
        base = archive_upload._stagger_offset_s("gmnro03", WINDOW_S, 0.0)
        days = [f"202609{d:02d}" for d in range(1, 29)]
        seen = {archive_upload._stagger_offset_s("gmnro03", WINDOW_S, 300.0, day=d)
                for d in days}
        assert all(base <= o <= base + 300.0 for o in seen)
        assert len(seen) > 1, "jitter should vary between days"

    def test_jitter_is_stable_within_a_day(self):
        offsets = {archive_upload._stagger_offset_s("gmnro03", WINDOW_S, 300.0, day="20260926")
                   for _ in range(10)}
        assert len(offsets) == 1


# ── The hold decision ─────────────────────────────────────────────────────

class TestHold:
    def test_no_hold_before_the_window_opens(self):
        """An evening or manual run must never be delayed."""
        hold = archive_upload.stagger_hold_s(
            _cfg(), "gmnro03", now=_at(22, 30), offset_s=1800.0,
        )
        assert hold == 0.0

    def test_holds_inside_the_window_before_the_slot(self):
        hold = archive_upload.stagger_hold_s(
            _cfg(), "gmnro03", now=_at(4, 5), offset_s=1800.0,
        )
        # Slot at 04:30, now 04:05 -> 25 minutes to wait.
        assert hold == pytest.approx(25 * 60, abs=1)

    def test_no_hold_once_the_slot_has_passed(self):
        hold = archive_upload.stagger_hold_s(
            _cfg(), "gmnro03", now=_at(4, 45), offset_s=1800.0,
        )
        assert hold == 0.0

    def test_no_hold_later_in_the_morning(self):
        """Every slot has opened by the end of the window, so the back half of
        the dawn_process cron range is never gated."""
        for hour in (5, 6, 7, 8, 9, 10):
            hold = archive_upload.stagger_hold_s(
                _cfg(), "gmnro03", now=_at(hour), offset_s=WINDOW_S,
            )
            assert hold == 0.0, f"still holding at {hour}:00"

    def test_hold_never_exceeds_window_plus_jitter(self):
        """A station can be delayed, but the delay is bounded -- no station can
        be starved of its upload."""
        for host in FLEET:
            hold = archive_upload.stagger_hold_s(
                _cfg(stagger_jitter_minutes=5), host, now=_at(4, 0),
            )
            assert 0.0 <= hold <= WINDOW_S + 5 * 60

    def test_zero_window_config_opts_a_station_out(self):
        hold = archive_upload.stagger_hold_s(
            _cfg(stagger_window_minutes=0), "gmnro03", now=_at(4, 1),
        )
        assert hold == 0.0

    def test_window_start_hour_is_configurable(self):
        # A station whose dawn cron starts at 03:00 gates from 03:00.
        assert archive_upload.stagger_hold_s(
            _cfg(stagger_window_start_hour=3), "gmnro03",
            now=_at(3, 5), offset_s=1800.0,
        ) == pytest.approx(25 * 60, abs=1)
        # ...and is not gated at 02:55, before its window.
        assert archive_upload.stagger_hold_s(
            _cfg(stagger_window_start_hour=3), "gmnro03",
            now=_at(2, 55), offset_s=1800.0,
        ) == 0.0

    def test_shorter_window_shortens_the_worst_case_hold(self):
        hold = archive_upload.stagger_hold_s(
            _cfg(stagger_window_minutes=15, stagger_jitter_minutes=0),
            "gmnro03", now=_at(4, 0),
        )
        assert 0.0 <= hold <= 15 * 60


# ── Fleet-level effect: concurrent starts per quarter hour ────────────────

def test_fleet_starts_are_spread_not_simultaneous():
    """Before the stagger, all 15 stations started in the same cron tick.

    dawn_process runs every 15 min, so a station actually starts at the first
    tick at or after its slot -- four possible ticks across the hour. With 15
    stations the best any scheme can do is 4/4/4/3, so anything above 5 per
    tick means the fleet is still bunching. (A plain host-key hash gives
    2/7/5/1.)
    """
    ticks: dict[int, int] = {}
    for host in FLEET:
        offset = archive_upload._stagger_offset_s(host, WINDOW_S)
        tick = int(offset // 900) + 1        # 0,1,2,3 -> 15/30/45/60 min tick
        ticks[tick] = ticks.get(tick, 0) + 1
    assert max(ticks.values()) <= 5, (
        f"too many stations still starting together: {ticks}"
    )
    assert len(ticks) == 4, f"starts not spread over the window: {ticks}"


# ── Identity ──────────────────────────────────────────────────────────────

class TestStaggerId:
    def test_falls_back_to_hostname_without_an_ingest_key(self, monkeypatch):
        monkeypatch.setattr(archive_upload, "_resolve_host_key", None)
        monkeypatch.setattr(archive_upload.socket, "gethostname", lambda: "somebox")
        assert archive_upload.stagger_id({}) == "somebox"

    def test_prefers_the_ingest_host_key(self, monkeypatch):
        monkeypatch.setattr(archive_upload, "_resolve_host_key", lambda cfg: "gmnro03")
        assert archive_upload.stagger_id({}) == "gmnro03"

    def test_falls_back_when_key_resolution_raises(self, monkeypatch):
        def _boom(cfg):
            raise RuntimeError("no config")

        monkeypatch.setattr(archive_upload, "_resolve_host_key", _boom)
        monkeypatch.setattr(archive_upload.socket, "gethostname", lambda: "somebox")
        assert archive_upload.stagger_id({}) == "somebox"

    def test_cameras_share_one_slot_with_the_jitter_on(self, monkeypatch):
        """Dawn asks once per camera and passes no offset: with a random
        jitter per call, a station's cameras landed on both sides of its slot."""
        monkeypatch.setattr(archive_upload, "_resolve_host_key", lambda cfg: "gmnro03")
        cfg = _cfg(stagger_jitter_minutes=5)
        holds = {
            archive_upload.stagger_hold_s(cfg, archive_upload.stagger_id(cfg), now=_at(4, 1))
            for _camera in range(6)
        }
        assert len(holds) == 1

    def test_all_cameras_of_a_station_share_one_slot(self, monkeypatch):
        """The stagger is per HOST, so a station's cameras move together
        instead of interleaving with other stations'."""
        monkeypatch.setattr(archive_upload, "_resolve_host_key", lambda cfg: "gmnro03")
        cfg = _cfg()
        holds = {
            archive_upload.stagger_hold_s(
                cfg, archive_upload.stagger_id(cfg), now=_at(4, 1), offset_s=1800.0,
            )
            for _camera in ("RO000H", "RO000I", "RO000J")
        }
        assert len(holds) == 1


# ── The gate is actually wired into the upload entry points ──────────────

class TestWiring:
    def test_run_night_skips_before_its_slot(self, monkeypatch):
        monkeypatch.setattr(archive_upload, "stagger_hold_s",
                            lambda *a, **k: 900.0)
        called: list[tuple] = []
        monkeypatch.setattr(
            archive_upload._Uploader, "cycle_night",
            lambda self, s, d, **k: called.append((s, d)) or 1,
        )
        monkeypatch.setattr(archive_upload._Uploader, "enabled", True,
                            raising=False)

        assert archive_upload.run_night("RO000H", "20260908", _cfg()) == 0
        assert called == [], "upload ran despite the stagger hold"

    def test_run_night_uploads_once_the_slot_is_open(self, monkeypatch):
        monkeypatch.setattr(archive_upload, "stagger_hold_s", lambda *a, **k: 0.0)
        called: list[tuple] = []

        def _cycle_night(self, s, d, **k):
            called.append((s, d))
            return 3

        monkeypatch.setattr(archive_upload._Uploader, "cycle_night", _cycle_night)
        monkeypatch.setattr(archive_upload._Uploader, "enabled", True,
                            raising=False)

        assert archive_upload.run_night("RO000H", "20260908", _cfg()) == 3
        assert called == [("RO000H", "20260908")]

    def test_run_once_skips_before_its_slot(self, monkeypatch):
        monkeypatch.setattr(archive_upload, "stagger_hold_s",
                            lambda *a, **k: 600.0)
        monkeypatch.setattr(
            archive_upload._Uploader, "cycle",
            lambda self, *a, **k: pytest.fail("full scan ran while held"),
        )
        monkeypatch.setattr(archive_upload._Uploader, "enabled", True,
                            raising=False)

        assert archive_upload.run_once("RO000H", "", _cfg()) == 0
