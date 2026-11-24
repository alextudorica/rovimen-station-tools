"""The colour-capture ffmpeg watchdog must catch a stream that never cuts.

Judging ffmpeg by the newest segment's mtime misses the failure that costs a
whole night: when the camera emits corrupted or frozen DTS timestamps the
segment muxer never reaches its cut point, so ffmpeg appends to ONE file
forever — refreshing that file's mtime on every frame. RO000Y did this on
20260831 (a 839 MB "mega-segment" whose container claims 20.0 s and holds
~13.4 min) and kept doing it for 2 h 15 min without a single restart. The
mirror-image failure, DTS jumping backwards and cutting thousands of ~1 s
segments, was invisible too, because files kept appearing.

These tests pin the replacement policy: liveness comes from the segment
FILENAME advancing and from the newest segment's size, never from mtime.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

from color_capture import (
    CAPTURE_RESTART_MIN_INTERVAL_S,
    CAPTURE_STALL_FLOOR_S,
    ColorCapture,
    evaluate_capture_health,
)

MB = 1024 * 1024
# A 20 s chunk off a fleet camera (~8.5 Mbit/s).
NOMINAL = 21 * MB
# RO000Y_20260831_192108_color.mkv as found on the station.
MEGA = 839412004


def _cadence(count, step_s=20, start='20260831_192100', size=NOMINAL, cam='RO000Y'):
    """Return ``(filename, size)`` pairs at a fixed cadence, oldest first."""
    t0 = datetime.strptime(start, '%Y%m%d_%H%M%S')
    out = []
    for i in range(count):
        stamp = (t0 + timedelta(seconds=i * step_s)).strftime('%Y%m%d_%H%M%S')
        out.append(('%s_%s_color.mkv' % (cam, stamp), size))
    return out


def _grow(segments, size):
    """Same listing with the newest segment inflated to ``size``."""
    return segments[:-1] + [(segments[-1][0], size)]


class _AliveProc:
    """Stand-in for a running ffmpeg."""

    pid = 4242

    def poll(self):
        return None


# ── The newest filename, not its mtime, is the liveness signal ────────────


class TestStall:
    def test_normal_cadence_never_restarts(self):
        """A new segment every 20 s, polled every 30 s: nothing to do."""
        state = None
        now = 0.0
        for n in range(2, 60):
            health = evaluate_capture_health(
                _cadence(n), now, state, segment_duration=20)
            assert health.restart is False, health.reason
            state = health.state
            now += 30

    def test_mega_segment_restarts_although_its_mtime_stays_fresh(self):
        """The Brasov failure: one filename, forever, mtime always current.

        The decision never sees an mtime — the growing file cannot vouch for
        itself — so the unchanged filename alone condemns the stream.
        """
        stuck = _cadence(5)
        state = None
        restart = None
        for step in range(6):
            health = evaluate_capture_health(
                stuck, step * 30.0, state, segment_duration=20)
            state = health.state
            if health.restart:
                restart = health
                break
        assert restart is not None, 'watchdog never fired on a stuck filename'
        assert restart.kind == 'stall'
        assert stuck[-1][0] in restart.reason

    def test_stall_floor_applies_to_a_short_segment_duration(self):
        """3 x segment_duration would judge a 5 s cadence in 15 s."""
        stuck = _cadence(5, step_s=5)
        state = evaluate_capture_health(
            stuck, 0.0, None, segment_duration=5).state
        health = evaluate_capture_health(
            stuck, CAPTURE_STALL_FLOOR_S - 1.0, state, segment_duration=5)
        assert health.restart is False
        health = evaluate_capture_health(
            stuck, CAPTURE_STALL_FLOOR_S + 1.0, state, segment_duration=5)
        assert health.restart is True
        assert health.kind == 'stall'


# ── Size cap ──────────────────────────────────────────────────────────────


class TestOversize:
    def test_mega_segment_size_fires_before_the_stall_timer(self):
        """839 MB in a 20 s slot needs no timer to be obviously wrong."""
        health = evaluate_capture_health(
            _grow(_cadence(5), MEGA), 0.0, None, segment_duration=20)
        assert health.restart is True
        assert health.kind == 'oversize'
        assert '801 MB' in health.reason
        assert 'cap 84 MB' in health.reason

    def test_first_segment_of_the_night_is_capped_by_the_floor(self):
        """Nothing completed yet, so the cap is 2 x segment_duration of video."""
        health = evaluate_capture_health(
            [('RO000Y_20260831_192100_color.mkv', MEGA)], 0.0, None,
            segment_duration=20)
        assert health.restart is True
        assert health.kind == 'oversize'

    def test_high_bitrate_camera_is_not_condemned(self):
        """A genuinely fat camera raises its own cap through the median."""
        fat = _cadence(12, size=60 * MB)
        health = evaluate_capture_health(
            _grow(fat, 62 * MB), 0.0, None, segment_duration=20)
        assert health.restart is False

    def test_restart_stubs_in_the_median_cannot_condemn_a_healthy_segment(self):
        """Short segments left by earlier restarts shrink the median; the
        floor keeps a normal chunk out of trouble."""
        stubs = _cadence(9, size=1 * MB)
        newest = _cadence(10)[-1]
        health = evaluate_capture_health(
            stubs + [newest], 0.0, None, segment_duration=20)
        assert health.restart is False


# ── Storm breaker ─────────────────────────────────────────────────────────


class TestStorm:
    def test_one_second_segments_restart_the_stream(self):
        """DTS jumping backwards cuts a segment per second — impossible at
        nominal cadence, and files keep appearing so no timer would notice."""
        storm = _cadence(300, step_s=1, size=1 * MB)
        health = evaluate_capture_health(storm, 0.0, None, segment_duration=20)
        assert health.restart is True
        assert health.kind == 'storm'
        assert 'corrupted' in health.reason

    def test_short_segment_duration_cadence_is_not_a_storm(self):
        """A station cutting every 2 s writes 30 segments a minute by design;
        the threshold scales with the configured cadence."""
        fast = _cadence(300, step_s=2, size=2 * MB)
        health = evaluate_capture_health(fast, 0.0, None, segment_duration=2)
        assert health.restart is False

    def test_storm_does_not_refire_once_the_cadence_recovers(self):
        """The wreckage stays on disk after the restart; only segments cut
        after it count, so a recovered stream is left alone."""
        storm = _cadence(120, step_s=1, size=1 * MB)
        first = evaluate_capture_health(storm, 0.0, None, segment_duration=20)
        assert first.kind == 'storm'
        recovered = storm + _cadence(6, start='20260831_192400')
        health = evaluate_capture_health(
            recovered, 500.0, first.state, segment_duration=20)
        assert health.restart is False

    def test_storm_still_fires_if_the_corruption_continues(self):
        storm = _cadence(120, step_s=1, size=1 * MB)
        first = evaluate_capture_health(storm, 0.0, None, segment_duration=20)
        assert first.kind == 'storm'
        worse = storm + _cadence(120, step_s=1, start='20260831_192300',
                                 size=1 * MB)
        health = evaluate_capture_health(
            worse, 500.0, first.state, segment_duration=20)
        assert health.restart is True
        assert health.kind == 'storm'


# ── Daytime idle ──────────────────────────────────────────────────────────


class TestIdle:
    def test_empty_night_directory_never_restarts(self):
        """Capture only runs on FF activity; an empty listing all day long
        must not turn into a restart loop."""
        state = None
        now = 0.0
        for _ in range(400):
            health = evaluate_capture_health([], now, state, segment_duration=20)
            assert health.restart is False
            state = health.state
            now += 30

    def test_grace_period_starts_when_the_first_segment_appears(self):
        """Hours of empty polls must not leave a stall already overdue."""
        state = None
        now = 0.0
        for _ in range(400):
            state = evaluate_capture_health(
                [], now, state, segment_duration=20).state
            now += 30
        health = evaluate_capture_health(
            _cadence(1), now, state, segment_duration=20)
        assert health.restart is False


# ── Restart rate limiting ─────────────────────────────────────────────────


class TestRateLimit:
    def test_second_fault_inside_the_floor_is_suppressed(self):
        mega = _grow(_cadence(5), MEGA)
        first = evaluate_capture_health(mega, 0.0, None, segment_duration=20)
        assert first.restart is True

        held = evaluate_capture_health(
            mega, CAPTURE_RESTART_MIN_INTERVAL_S - 1.0, first.state,
            segment_duration=20)
        assert held.restart is False
        # Nothing to log either: the caller would otherwise repeat the same
        # reason every 30 s for as long as the file sits there.
        assert held.reason is None

        again = evaluate_capture_health(
            mega, CAPTURE_RESTART_MIN_INTERVAL_S + 1.0, held.state,
            segment_duration=20)
        assert again.restart is True
        assert again.kind == 'oversize'

    def test_permanently_broken_camera_is_retried_but_never_tight_loops(self):
        """An hour of 30 s polls against a stream that never recovers."""
        stuck = _cadence(5)
        state = None
        fired = []
        now = 0.0
        for _ in range(120):
            health = evaluate_capture_health(
                stuck, now, state, segment_duration=20)
            state = health.state
            if health.restart:
                fired.append(now)
            now += 30
        assert fired, 'a stuck stream must still be retried'
        assert fired[0] >= CAPTURE_STALL_FLOOR_S
        gaps = [b - a for a, b in zip(fired, fired[1:])]
        assert min(gaps) >= CAPTURE_RESTART_MIN_INTERVAL_S
        assert len(fired) < 30


# ── Wiring: the supervisor loop reads sizes, not mtimes ───────────────────


def _watchdog_capture(tmp_path: Path) -> tuple:
    cap = ColorCapture.__new__(ColorCapture)
    cap.capture_path = tmp_path
    cap.segment_duration = 20
    cap.color_tee = False
    cap.log = logging.getLogger('test_color_capture_watchdog')
    cap._capture_health = {}
    cap._ffmpeg = {'RO000Y': _AliveProc()}
    calls: list = []
    cap._stop_ffmpeg = lambda sid: calls.append(('stop', sid))
    cap._start_ffmpeg = lambda sid: calls.append(('start', sid))
    return cap, calls


def _write_sparse(path: Path, size: int) -> None:
    with open(str(path), 'wb') as f:
        f.truncate(size)


class TestSupervisorWiring:
    def test_night_segments_reports_name_and_size(self, tmp_path: Path):
        cap, _calls = _watchdog_capture(tmp_path)
        night = tmp_path / 'RO000Y' / ColorCapture._night_date()
        night.mkdir(parents=True)
        for name, size in _cadence(3):
            _write_sparse(night / name, size)
        (night / 'state.json').write_text('{}')

        found = sorted(cap._night_segments('RO000Y'))
        assert found == _cadence(3)

    def test_mega_segment_on_disk_restarts_capture(self, tmp_path: Path):
        cap, calls = _watchdog_capture(tmp_path)
        night = tmp_path / 'RO000Y' / ColorCapture._night_date()
        night.mkdir(parents=True)
        for name, size in _grow(_cadence(5), MEGA):
            _write_sparse(night / name, size)

        cap._check_ffmpeg_health()
        assert calls == [('stop', 'RO000Y'), ('start', 'RO000Y')]

    def test_missing_night_directory_is_left_alone(self, tmp_path: Path):
        cap, calls = _watchdog_capture(tmp_path)
        cap._check_ffmpeg_health()
        cap._check_ffmpeg_health()
        assert calls == []

    def test_tee_mode_is_not_policed(self, tmp_path: Path):
        cap, calls = _watchdog_capture(tmp_path)
        cap.color_tee = True
        night = tmp_path / 'RO000Y' / ColorCapture._night_date()
        night.mkdir(parents=True)
        for name, size in _grow(_cadence(5), MEGA):
            _write_sparse(night / name, size)

        cap._check_ffmpeg_health()
        assert calls == []
