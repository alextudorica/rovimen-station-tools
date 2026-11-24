"""Regression guard for the sky-dome render storm that froze the prod dashboard.

The scheduler used to re-render every station's 1600x1600 dome (both variants)
on every worker start. Because the worker recycles (max_requests) and was being
SIGKILLed on WORKER TIMEOUT, that whole-fleet render storm re-ran every ~10 min,
pegging the VPS CPU (each render is ~1-2 CPU-s x cameras) and starving request
serving -> the freezing.

The fix seeds freshness from the on-disk cache mtime (which survives a respawn)
and skips rendering anything still fresh, so a recycled worker inherits the
previous worker's renders instead of re-storming. These tests pin:

  1. ``_dome_cache_is_fresh`` — the freshness gate (fresh / stale / missing).
  2. The render/colour cadences are env-overridable so the VPS can be throttled
     without a redeploy.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path


def test_dome_cache_is_fresh(tmp_path):
    from routes.sky_dome import _dome_cache_is_fresh

    png = tmp_path / "sky_dome_ff_max.png"
    png.write_bytes(b"x")
    os.utime(png, (1000.0, 1000.0))  # mtime = 1000

    # now=1030, threshold 60s -> 30s old -> fresh.
    assert _dome_cache_is_fresh(png, 60.0, now=1030.0) is True
    # now=1100, threshold 60s -> 100s old -> stale.
    assert _dome_cache_is_fresh(png, 60.0, now=1100.0) is False
    # exactly at the boundary is treated as stale (strict <).
    assert _dome_cache_is_fresh(png, 60.0, now=1060.0) is False
    # missing file -> not fresh (render).
    assert _dome_cache_is_fresh(tmp_path / "nope.png", 60.0, now=1030.0) is False


def test_dome_cadences_are_env_overridable(monkeypatch):
    """The night / day / colour cadences must read from env at import so the VPS
    can be dialled down without a code deploy while the station-side offload is
    built."""
    monkeypatch.setenv("ROVIMEN_DOME_INTERVAL_NIGHT_S", "180")
    monkeypatch.setenv("ROVIMEN_DOME_INTERVAL_DAY_S", "3600")
    monkeypatch.setenv("ROVIMEN_DOME_COLOUR_INTERVAL_NIGHT_S", "900")

    import routes.sky_dome as sky_dome
    sky_dome = importlib.reload(sky_dome)
    try:
        assert sky_dome._DOME_PRERENDER_INTERVAL_NIGHT_S == 180
        assert sky_dome._DOME_PRERENDER_INTERVAL_DAY_S == 3600
        assert sky_dome._DOME_COLOUR_NIGHT_INTERVAL_S == 900
    finally:
        # Restore module defaults for any later test that imports it.
        for key in (
            "ROVIMEN_DOME_INTERVAL_NIGHT_S",
            "ROVIMEN_DOME_INTERVAL_DAY_S",
            "ROVIMEN_DOME_COLOUR_INTERVAL_NIGHT_S",
        ):
            monkeypatch.delenv(key, raising=False)
        importlib.reload(sky_dome)


def test_dome_defaults_unchanged(monkeypatch):
    """Without env overrides the historical defaults hold."""
    for key in (
        "ROVIMEN_DOME_INTERVAL_NIGHT_S",
        "ROVIMEN_DOME_INTERVAL_DAY_S",
        "ROVIMEN_DOME_COLOUR_INTERVAL_NIGHT_S",
    ):
        monkeypatch.delenv(key, raising=False)

    import routes.sky_dome as sky_dome
    sky_dome = importlib.reload(sky_dome)
    assert sky_dome._DOME_PRERENDER_INTERVAL_NIGHT_S == 60
    assert sky_dome._DOME_PRERENDER_INTERVAL_DAY_S == 1800
    assert sky_dome._DOME_COLOUR_NIGHT_INTERVAL_S == 600


def test_timelapse_attempt_marker_is_distinct_from_the_output(tmp_path, monkeypatch):
    """The guard must key off "attempted", not "output exists".

    Most stations produce no dome timelapse on a given night (no per-camera
    MP4s, no platepar). Keying the skip off the output MP4 would leave exactly
    those stations re-running the full build on every scheduler start -- the
    storm this guard exists to stop.
    """
    import routes.sky_dome as sky_dome

    monkeypatch.setattr(sky_dome, "THUMB_CACHE_DIR", tmp_path)

    marker = sky_dome._sky_dome_timelapse_attempt_marker("gmnro07", "20260903")
    output = sky_dome._sky_dome_timelapse_path("gmnro07", "20260903")

    assert marker != output
    assert marker.parent == output.parent
    assert marker.name == "20260903.attempted"
    assert output.name == "20260903.mp4"

    # A recorded attempt that produced nothing is still a recorded attempt.
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    assert marker.exists()
    assert not output.exists()

    # And the marker must not surface as an available night to the UI, which
    # lists the directory by *.mp4.
    assert {p.stem for p in marker.parent.glob("*.mp4")} == set()


class TestTimelapseRetryStateMachine:
    """The night-timelapse trigger must survive its inputs arriving late.

    Regression tests for 2026-09-17: the single dawn-crossing shot fired at
    ~03:20 UTC, hours before stations publish their per-camera timelapses
    (~07:20), so every build found nothing, recorded its attempt marker and
    never retried — zero fleet dome timelapses since 2026-09-08.
    """

    def _state(self, **kw):
        from routes.sky_dome import _timelapse_retry_state
        defaults = dict(mp4_exists=False, marker_exists=False,
                        sun_alt=10.0, utc_hour=6)
        defaults.update(kw)
        return _timelapse_retry_state(**defaults)

    def test_missing_inputs_night_stays_due_through_the_morning(self):
        """THE BUG: 03:20 (dawn) and 07:30 (inputs ready) must both be due."""
        assert self._state(sun_alt=-5.0, utc_hour=3) == "due"
        assert self._state(sun_alt=25.0, utc_hour=7) == "due"

    def test_a_night_with_output_or_marker_is_closed(self):
        assert self._state(mp4_exists=True) == "skip"
        assert self._state(marker_exists=True) == "skip"

    def test_night_time_never_builds(self):
        """Sun below civil twilight = the night is still being captured."""
        assert self._state(sun_alt=-30.0, utc_hour=22) == "skip"
        assert self._state(sun_alt=-8.0, utc_hour=2) == "skip"
        assert self._state(sun_alt=None, utc_hour=6) == "skip"

    def test_give_up_closes_the_night_once(self):
        """Past the give-up hour the caller records the marker; with the
        marker recorded the night reads as closed, not give-up again."""
        assert self._state(utc_hour=12) == "give_up"
        assert self._state(utc_hour=12, marker_exists=True) == "skip"

    def test_after_1400_utc_the_night_key_flips_so_nothing_runs(self):
        assert self._state(utc_hour=14) == "skip"
        assert self._state(utc_hour=20) == "skip"
