"""The janitor must free the bytes it deletes on a color_tee station.

On a tee station every colour chunk is a hardlink of an RMS raw segment under
``<rms_data_path>/VideoFiles``. Unlinking only the chunk freed nothing, so on
gmnro05 (Bistrița) on 2026-10-04 the pressure eviction deleted 232 chunks in one
pass with free space unchanged, then went on to the only copies of 295
detections, freeing nothing either, while 69 GB of raw segments whose chunks
were long gone sat in VideoFiles. A chunk deletion now also removes the raw
segment that is the same file, a sweep removes raw segments whose chunk is
already gone, and ``retention.color_after_morning`` releases a night's
non-detection chunks once its morning sequence is done.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import janitor_storage_watchdog as jsw


def _stamp(hours_ago: float) -> tuple[str, str]:
    t = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return t.strftime('%Y%m%d'), t.strftime('%H%M%S')


def _station(tmp_path: Path, *, tee: bool = True, retention: dict | None = None) -> tuple[jsw.Janitor, Path, Path]:
    capture = tmp_path / 'capture'
    rms = tmp_path / 'RMS_data' / 'CAM1'
    (capture / 'CAM1').mkdir(parents=True)
    (rms / 'VideoFiles').mkdir(parents=True)
    cfg = {
        'videocapture_path': str(capture),
        'log_path': str(tmp_path / 'logs'),
        'color_tee': tee,
        'stations': {'CAM1': {'rms_data_path': str(rms)}},
    }
    if retention is not None:
        cfg['retention'] = retention
    cfg_path = tmp_path / 'config.json'
    cfg_path.write_text(json.dumps(cfg))
    return jsw.Janitor(str(cfg_path)), capture, rms / 'VideoFiles'


def _segment(video: Path, hours_ago: float, *, size: int = 1000) -> Path:
    date, time = _stamp(hours_ago)
    day = video / date[:4] / f'{date}-001'
    day.mkdir(parents=True, exist_ok=True)
    seg = day / f'CAM1_{date}_{time}_123456_video.mkv'
    seg.write_bytes(b'v' * size)
    return seg


def _tee_link(capture: Path, seg: Path, night: str, *, ledger: bool = True) -> Path:
    m = jsw.rovimen_night.VIDEO_SEGMENT_RE.match(seg.name)
    chunk = capture / 'CAM1' / night / f'{m.group(1)}_{m.group(2)}_{m.group(3)}_color.mkv'
    chunk.parent.mkdir(parents=True, exist_ok=True)
    os.link(seg, chunk)
    if ledger:
        with open(capture / 'CAM1' / '.tee_linked.log', 'a') as fh:
            fh.write(seg.name + '\n')
    return chunk


def _state(capture: Path, night: str, **fields) -> None:
    (capture / 'CAM1' / night / 'state.json').write_text(json.dumps({'chunks': {}, **fields}))


class TestChunkDeletionRemovesRawTwin:
    def test_eviction_frees_the_raw_segment_too(self, tmp_path, monkeypatch):
        jan, capture, video = _station(tmp_path)
        seg = _segment(video, 30)
        chunk = _tee_link(capture, seg, _stamp(30)[0])
        calls = iter([True, True, False])
        monkeypatch.setattr(jan, '_over_limit', lambda: next(calls, False))
        dt = datetime.now(timezone.utc) - timedelta(hours=30)

        assert jan._evict_chunks([(dt, False, 'CAM1', _stamp(30)[0], chunk)], phase='test') == 1
        assert not chunk.exists()
        assert not seg.exists()

    def test_retention_purge_frees_the_raw_segment_too(self, tmp_path):
        jan, capture, video = _station(tmp_path, retention={'color_days': 1})
        seg = _segment(video, 80)
        night = _stamp(80)[0]
        chunk = _tee_link(capture, seg, night)

        jan._purge_station('CAM1')

        assert not chunk.exists()
        assert not seg.exists()

    def test_a_segment_that_is_another_file_is_kept(self, tmp_path):
        jan, capture, video = _station(tmp_path, retention={'color_days': 1})
        seg = _segment(video, 80)
        night = _stamp(80)[0]
        chunk = _tee_link(capture, seg, night)
        # Same name mapping, different inode (a copy), and the chunk is still
        # linked by a second name so st_nlink alone would not rule it out.
        chunk.unlink()
        chunk.write_bytes(b'c' * 10)
        os.link(chunk, chunk.with_name('spare.bin'))

        jan._unlink_chunk('CAM1', chunk)

        assert not chunk.exists()
        assert seg.exists()

    def test_non_tee_station_never_touches_rms_video(self, tmp_path):
        jan, capture, video = _station(tmp_path, tee=False, retention={'color_days': 1})
        seg = _segment(video, 80)
        chunk = _tee_link(capture, seg, _stamp(80)[0])

        jan._purge_station('CAM1')
        jan._purge_tee_orphans()

        assert not chunk.exists()
        assert seg.exists()


class TestOrphanSweep:
    def test_removes_linked_segments_whose_chunk_is_gone(self, tmp_path):
        jan, capture, video = _station(tmp_path)
        gone = _segment(video, 30)
        _tee_link(capture, gone, _stamp(30)[0]).unlink()
        live = _segment(video, 29)
        live_chunk = _tee_link(capture, live, _stamp(29)[0])

        jan._purge_tee_orphans()

        assert not gone.exists()
        assert live.exists() and live_chunk.exists()

    def test_keeps_segments_the_watcher_never_linked(self, tmp_path):
        jan, capture, video = _station(tmp_path)
        unlinked = _segment(video, 30)
        (capture / 'CAM1' / '.tee_linked.log').write_text('')

        jan._purge_tee_orphans()

        assert unlinked.exists()

    def test_keeps_recent_segments(self, tmp_path):
        jan, capture, video = _station(tmp_path)
        recent = _segment(video, 0.5)
        _tee_link(capture, recent, _stamp(0.5)[0]).unlink()

        jan._purge_tee_orphans()

        assert recent.exists()

    def test_runs_in_the_normal_sweep(self, tmp_path):
        jan, capture, video = _station(tmp_path)
        gone = _segment(video, 30)
        _tee_link(capture, gone, _stamp(30)[0]).unlink()

        jan._run_normal_sweep()

        assert not gone.exists()


class TestRollingRelease:
    """Rolling release (default on): once a night's morning is done, its
    non-detection chunks go oldest first, only while free space is under the
    keep-free target."""

    def _night(self, tmp_path, *, hours=(30, 29, 28), morning_done=True, locked=(),
               retention=None, rms_complete=True):
        jan, capture, video = _station(tmp_path, retention=retention or {
            'color_days': 1, 'color_after_morning': True, 'color_keep_free_gb': 80})
        night = _stamp(max(hours))[0]
        chunks, segs = [], []
        for h in hours:
            seg = _segment(video, h)
            segs.append(seg)
            chunks.append(_tee_link(capture, seg, night))
        _state(capture, night, morning_done=morning_done, rms_complete=rms_complete,
               chunks={chunks[i].name: {'lock': 'detection'} for i in locked})
        return jan, capture, chunks, segs

    @staticmethod
    def _disk(monkeypatch, capture, free_at):
        """Fake free space as a function of how many chunks are left."""
        monkeypatch.setattr(jsw, '_fs_stats', lambda p: (
            90, free_at(len(list(capture.glob('CAM1/*/*_color.mkv'))))))

    def test_deletes_oldest_first_until_the_target(self, tmp_path, monkeypatch):
        jan, capture, chunks, segs = self._night(tmp_path)
        self._disk(monkeypatch, capture, lambda n: 80.0 - 0.5 * (n - 1))
        with jsw.flags_manager.scan_cache():
            jan._rolling_release()
        assert [c.exists() for c in chunks] == [False, False, True]
        assert [s.exists() for s in segs] == [False, False, True]

    def test_nothing_goes_while_free_space_is_above_the_target(self, tmp_path, monkeypatch):
        jan, capture, chunks, _ = self._night(tmp_path)
        self._disk(monkeypatch, capture, lambda n: 81.0)
        jan._rolling_release()
        assert all(c.exists() for c in chunks)

    def test_a_night_whose_morning_is_not_done_is_kept(self, tmp_path, monkeypatch):
        jan, capture, chunks, _ = self._night(tmp_path, morning_done=False)
        self._disk(monkeypatch, capture, lambda n: 10.0)
        jan._rolling_release()
        assert all(c.exists() for c in chunks)

    def test_a_night_rms_has_not_finished_is_kept(self, tmp_path, monkeypatch):
        """Dawn marks morning_done even when it gave up waiting for RMS and
        skipped the EON lock pass (rms_complete unset). The detections RMS
        finishes later are locked by the next dawn's backfill, so the night's
        chunks wait for it; the pressure eviction still covers a night that
        never gets there."""
        jan, capture, chunks, _ = self._night(tmp_path, rms_complete=False)
        self._disk(monkeypatch, capture, lambda n: 10.0)
        jan._rolling_release()
        assert all(c.exists() for c in chunks)

    def test_a_night_rms_has_finished_is_rolled(self, tmp_path, monkeypatch):
        jan, capture, chunks, _ = self._night(tmp_path, rms_complete=True)
        self._disk(monkeypatch, capture, lambda n: 80.0 - 0.5 * (n - 1))
        jan._rolling_release()
        assert [c.exists() for c in chunks] == [False, False, True]

    @pytest.mark.parametrize('camera', [{'rms': False}, {}],
                             ids=['rms-false', 'no-rms-data-path'])
    def test_a_camera_without_rms_is_rolled_on_morning_done_alone(
            self, tmp_path, monkeypatch, camera):
        """A colour-only or spectroscope camera has no RMS, so its nights
        never get an EON pass; morning_done alone releases them, as before."""
        jan, capture, chunks, _ = self._night(tmp_path, rms_complete=False)
        jan.cfg['stations']['CAM1'] = camera
        self._disk(monkeypatch, capture, lambda n: 80.0 - 0.5 * (n - 1))
        jan._rolling_release()
        assert [c.exists() for c in chunks] == [False, False, True]

    def test_pressure_eviction_still_takes_a_night_rms_never_finished(
            self, tmp_path, monkeypatch):
        jan, capture, chunks, _ = self._night(tmp_path, rms_complete=False)

        def stats(_p):
            free = 10.0 + 20.0 * (3 - len(list(capture.glob('CAM1/*/*_color.mkv'))))
            return (90 if free < 30 else 50), free

        monkeypatch.setattr(jsw, '_fs_stats', stats)
        jan._ordered_eviction()
        assert [c.exists() for c in chunks] == [False, True, True]

    def test_detections_are_kept(self, tmp_path, monkeypatch):
        jan, capture, chunks, _ = self._night(tmp_path, locked=(0,))
        self._disk(monkeypatch, capture, lambda n: 80.0 - 0.5 * (n - 2))
        jan._rolling_release()
        assert [c.exists() for c in chunks] == [True, False, True]

    def test_on_by_default(self, tmp_path, monkeypatch):
        jan, capture, chunks, _ = self._night(
            tmp_path, retention={'color_days': 2, 'color_keep_free_gb': 80})
        self._disk(monkeypatch, capture, lambda n: 80.0 - 0.5 * (n - 1))
        jan._rolling_release()
        assert [c.exists() for c in chunks] == [False, False, True]

    @pytest.mark.parametrize('key', ['rolling', 'color_after_morning'])
    def test_turned_off_per_station(self, tmp_path, monkeypatch, key):
        jan, capture, chunks, _ = self._night(tmp_path, retention={'color_days': 1, key: False})
        self._disk(monkeypatch, capture, lambda n: 10.0)
        jan._rolling_release()
        assert all(c.exists() for c in chunks)

    def test_stops_when_deleting_frees_nothing(self, tmp_path, monkeypatch):
        jan, capture, chunks, _ = self._night(tmp_path, hours=(30, 29, 28, 27))
        self._disk(monkeypatch, capture, lambda n: 10.0)
        monkeypatch.setattr(jsw, 'ROLLING_STALL_BYTES', 1500)
        jan._rolling_release()
        assert [c.exists() for c in chunks] == [False, False, True, True]

    def test_runs_in_every_janitor_pass(self, tmp_path, monkeypatch):
        jan, capture, chunks, _ = self._night(tmp_path)
        self._disk(monkeypatch, capture, lambda n: 80.0 - 0.5 * (n - 1))
        monkeypatch.setattr(jan, '_run_disk_check', lambda: None)
        monkeypatch.setattr(jan, '_write_state', lambda *a: None)
        jan.run()
        assert [c.exists() for c in chunks] == [False, False, True]

    @pytest.mark.parametrize('color_days, cap', [(1, 2), (2, 2), (4, 4)])
    def test_date_cap_is_color_days_but_at_least_two(self, tmp_path, color_days, cap):
        jan, _, _ = _station(tmp_path, retention={'color_days': color_days})
        assert jan.color_max_days == cap

    def test_keep_free_target_covers_rms_and_warn_pct(self, tmp_path, monkeypatch):
        jan, capture, _ = _station(tmp_path, retention={'color_days': 2})
        logs = tmp_path / 'RMS_data' / 'CAM1' / 'logs'
        logs.mkdir()
        (logs / 'log_CAM1_20261101_134927_001.log').write_text(
            '2026/11/01 15:37:34-INFO-DeleteOldObservations-line:867 - Need 40.00 GB for next night\n')
        (logs / 'log_CAM1_20261102_054754_001.log').write_text('no estimate here\n')
        usable = 400 * 10**9

        class _Vfs:
            f_frsize = 1
            f_blocks = usable
            f_bfree = usable // 2
            f_bavail = usable // 2

        monkeypatch.setattr(jsw.os, 'statvfs', lambda p: _Vfs())
        jan.warn_pct = 85
        jan.min_free_gb = 3
        # warn_pct: 15% of 400 GB = 60 GB beats RMS's 40 GiB (42.9 GB).
        assert jan._keep_free_gb() == pytest.approx(60 + jsw.COLOR_KEEP_MARGIN_GB)
        jan._keep_free_cache = None
        jan.warn_pct = 95
        assert jan._keep_free_gb() == pytest.approx(40 * 1024 ** 3 / 1e9 + jsw.COLOR_KEEP_MARGIN_GB)

    def test_color_max_days_replaces_color_days_as_the_date_cap(self, tmp_path):
        # A night two days old: color_days 1 would delete it at the date
        # cutoff, the rolling station keeps it until color_max_days (2).
        night = (datetime.now(timezone.utc) - timedelta(days=2)).strftime('%Y%m%d')
        for rolling, kept in ((True, True), (False, False)):
            base = tmp_path / str(rolling)
            base.mkdir()
            ret = {'color_days': 1, 'color_after_morning': rolling}
            jan, capture, video = _station(base, retention=ret)
            chunk = _tee_link(capture, _segment(video, 40), night)
            _state(capture, night, morning_done=True)
            jan._purge_station('CAM1')
            assert chunk.exists() is kept
