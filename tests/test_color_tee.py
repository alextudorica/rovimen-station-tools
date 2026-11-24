"""Tests for the color_tee feature in color_capture.py."""

from __future__ import annotations

import logging
import os
import time

import pytest

from color_capture import ColorCapture, _VIDEO_SEGMENT_RE


class TestColorNameFromVideo:
    """Test conversion of RMS video segment names to color capture names."""

    def test_standard_segment(self):
        result = ColorCapture._color_name_from_video(
            'RO000Z_20260622_234512_123456_video.mkv'
        )
        assert result == 'RO000Z_20260622_234512_color.mkv'

    def test_zero_microseconds(self):
        result = ColorCapture._color_name_from_video(
            'DE001B_20260621_210000_000000_video.mkv'
        )
        assert result == 'DE001B_20260621_210000_color.mkv'

    def test_non_video_segment(self):
        assert ColorCapture._color_name_from_video('something_else.mkv') is None

    def test_color_segment_rejected(self):
        assert ColorCapture._color_name_from_video(
            'RO000Z_20260622_234512_color.mkv'
        ) is None

    def test_empty_string(self):
        assert ColorCapture._color_name_from_video('') is None

    def test_short_microseconds_rejected(self):
        assert ColorCapture._color_name_from_video(
            'RO000Z_20260622_234512_1_video.mkv'
        ) is None


class TestVideoSegmentRegex:
    """Test the _VIDEO_SEGMENT_RE pattern."""

    def test_matches_standard(self):
        m = _VIDEO_SEGMENT_RE.match('RO000Z_20260622_234512_123456_video.mkv')
        assert m is not None
        assert m.group(1) == 'RO000Z'
        assert m.group(2) == '20260622'
        assert m.group(3) == '234512'

    def test_rejects_color_segment(self):
        assert _VIDEO_SEGMENT_RE.match('RO000Z_20260622_234512_color.mkv') is None

    def test_rejects_no_microseconds(self):
        assert _VIDEO_SEGMENT_RE.match('RO000Z_20260622_234512_video.mkv') is None

    def test_rejects_short_microseconds(self):
        assert _VIDEO_SEGMENT_RE.match('RO000Z_20260622_234512_1_video.mkv') is None

    def test_rejects_long_microseconds(self):
        assert _VIDEO_SEGMENT_RE.match('RO000Z_20260622_234512_1234567_video.mkv') is None


class TestColorTeeConfig:
    """Test that color_tee config is read correctly."""

    def test_default_false(self, tmp_path):
        cfg = tmp_path / 'config.json'
        cfg.write_text('{"stations": {}}')
        cc = ColorCapture.__new__(ColorCapture)
        import json
        cc.cfg = json.loads(cfg.read_text())
        cc.color_tee = cc.cfg.get('color_tee', False)
        assert cc.color_tee is False

    def test_explicit_true(self, tmp_path):
        cfg = tmp_path / 'config.json'
        cfg.write_text('{"stations": {}, "color_tee": true}')
        cc = ColorCapture.__new__(ColorCapture)
        import json
        cc.cfg = json.loads(cfg.read_text())
        cc.color_tee = cc.cfg.get('color_tee', False)
        assert cc.color_tee is True


class TestRmsVideoPath:
    """Test RMS video path derivation."""

    def test_derives_from_rms_data_path(self, tmp_path):
        """Must match RMS's own video_dir default, "VideoFiles".

        Pointing at <data_dir>/video instead produced zero linked segments for
        a whole night on gmnmd01 — the watcher waited forever on a directory
        RMS never creates.
        """
        cc = ColorCapture.__new__(ColorCapture)
        cc.cfg = {
            'stations': {
                'RO000Z': {'rms_data_path': str(tmp_path / 'RMS_data' / 'cam1')},
            }
        }
        result = cc._rms_video_path('RO000Z')
        assert result == tmp_path / 'RMS_data' / 'cam1' / 'VideoFiles'

    def test_honours_rms_video_dir_override(self, tmp_path):
        cc = ColorCapture.__new__(ColorCapture)
        cc.cfg = {
            'rms_video_dir': 'RawVideo',
            'stations': {
                'RO000Z': {'rms_data_path': str(tmp_path / 'RMS_data' / 'cam1')},
            }
        }
        result = cc._rms_video_path('RO000Z')
        assert result == tmp_path / 'RMS_data' / 'cam1' / 'RawVideo'


class TestTeeCadenceCheck:
    """A tee segment cadence that disagrees with segment_duration mis-maps
    detections onto neighbouring chunks, so it must be reported."""

    def _cc(self, segment_duration=20):
        cc = ColorCapture.__new__(ColorCapture)
        cc.segment_duration = segment_duration
        cc._tee_last_ts = {}
        cc._tee_cadence_warned = set()
        cc.log = logging.getLogger('test_tee_cadence')
        return cc

    def test_matching_cadence_is_silent(self, caplog):
        cc = self._cc(segment_duration=20)
        with caplog.at_level(logging.WARNING):
            cc._check_tee_cadence('MD0001', '20260806', '020000')
            cc._check_tee_cadence('MD0001', '20260806', '020020')
        assert not caplog.records
        assert 'MD0001' not in cc._tee_cadence_warned

    def test_mismatched_cadence_warns_once(self, caplog):
        """RMS raw_video_duration=30 against segment_duration=20 — the live
        gmnmd01 configuration."""
        cc = self._cc(segment_duration=20)
        with caplog.at_level(logging.WARNING):
            cc._check_tee_cadence('MD0001', '20260806', '020000')
            cc._check_tee_cadence('MD0001', '20260806', '020030')
            cc._check_tee_cadence('MD0001', '20260806', '020100')
        assert len(caplog.records) == 1
        assert 'raw_video_duration' in caplog.records[0].getMessage()
        assert 'MD0001' in cc._tee_cadence_warned

    def test_first_segment_never_warns(self, caplog):
        cc = self._cc()
        with caplog.at_level(logging.WARNING):
            cc._check_tee_cadence('MD0001', '20260806', '020000')
        assert not caplog.records

    def test_capture_restart_gap_is_not_a_cadence_signal(self, caplog):
        """A long gap means the night restarted, not that the cadence changed."""
        cc = self._cc(segment_duration=20)
        with caplog.at_level(logging.WARNING):
            cc._check_tee_cadence('MD0001', '20260805', '183000')
            cc._check_tee_cadence('MD0001', '20260806', '020000')
        assert not caplog.records

    def test_stations_tracked_independently(self, caplog):
        cc = self._cc(segment_duration=20)
        with caplog.at_level(logging.WARNING):
            cc._check_tee_cadence('MD0001', '20260806', '020000')
            cc._check_tee_cadence('MD0002', '20260806', '020010')
            cc._check_tee_cadence('MD0001', '20260806', '020020')
        # MD0002 has only one sample, MD0001's pair is on-cadence.
        assert not caplog.records


class TestNightDateFromFilename:
    """Test night date derivation from segment filename timestamps."""

    def test_evening_same_day(self):
        assert ColorCapture._night_date_from_filename('20260622', '213000') == '20260622'

    def test_after_midnight_previous_day(self):
        assert ColorCapture._night_date_from_filename('20260623', '024500') == '20260622'

    def test_noon_boundary_same_day(self):
        assert ColorCapture._night_date_from_filename('20260622', '120000') == '20260622'

    def test_just_before_noon(self):
        assert ColorCapture._night_date_from_filename('20260622', '115959') == '20260621'


class TestHardlinkTouchEmitsCloseWrite:
    """Verify that the touch-after-hardlink pattern emits IN_CLOSE_WRITE."""

    def test_hardlink_then_touch(self, tmp_path):
        src = tmp_path / 'source.mkv'
        src.write_bytes(b'\x00' * 100)
        dst = tmp_path / 'dest.mkv'
        os.link(str(src), str(dst))
        open(dst, 'ab').close()
        assert dst.exists()
        assert src.stat().st_ino == dst.stat().st_ino


class TestTeeReconcile:
    """The reconcile sweep is the completeness guarantee for tee mode: inotify
    dropped ~88% of a night's segment-close events on gmnmd01 (kernel queue
    overflow), so every closed segment the event stream missed must be linked
    by the sweep instead."""

    def _cc(self, tmp_path):
        cc = ColorCapture.__new__(ColorCapture)
        cc.capture_path = tmp_path / 'color_capture'
        cc.capture_path.mkdir(parents=True, exist_ok=True)
        cc.segment_duration = 20
        cc.min_disk_gb = 0
        cc._tee_last_ts = {}
        cc._tee_cadence_warned = set()
        cc.log = logging.getLogger('test_tee_reconcile')
        return cc

    def _segment(self, root, name, age_s=120):
        root.mkdir(parents=True, exist_ok=True)
        f = root / name
        f.write_bytes(b'\x00' * 64)
        old = time.time() - age_s
        os.utime(f, (old, old))
        return f

    def test_links_missed_segments_into_night_dirs(self, tmp_path):
        cc = self._cc(tmp_path)
        rms_video = tmp_path / 'RMS_data' / 'VideoFiles'
        hour_dir = rms_video / '2026' / '20260812-224' / '20260812-224_21'
        self._segment(hour_dir, 'RO000Z_20260812_213000_123456_video.mkv')
        # Post-midnight segment belongs to the previous night's folder.
        late_dir = rms_video / '2026' / '20260812-224' / '20260812-224_00'
        self._segment(late_dir, 'RO000Z_20260813_003000_123456_video.mkv')

        linked = cc._tee_reconcile('RO000Z', rms_video)

        assert linked == 2
        night = cc.capture_path / 'RO000Z' / '20260812'
        assert (night / 'RO000Z_20260812_213000_color.mkv').exists()
        assert (night / 'RO000Z_20260813_003000_color.mkv').exists()

    def test_skips_segment_still_being_written(self, tmp_path):
        cc = self._cc(tmp_path)
        rms_video = tmp_path / 'RMS_data' / 'VideoFiles'
        hour_dir = rms_video / '2026' / '20260812-224' / '20260812-224_21'
        self._segment(hour_dir, 'RO000Z_20260812_213000_123456_video.mkv', age_s=0)

        assert cc._tee_reconcile('RO000Z', rms_video) == 0
        assert not (cc.capture_path / 'RO000Z' / '20260812'
                    / 'RO000Z_20260812_213000_color.mkv').exists()

    def test_second_sweep_is_a_noop(self, tmp_path):
        cc = self._cc(tmp_path)
        rms_video = tmp_path / 'RMS_data' / 'VideoFiles'
        hour_dir = rms_video / '2026' / '20260812-224' / '20260812-224_21'
        self._segment(hour_dir, 'RO000Z_20260812_213000_123456_video.mkv')

        assert cc._tee_reconcile('RO000Z', rms_video) == 1
        assert cc._tee_reconcile('RO000Z', rms_video) == 0

    def test_registers_watches_for_every_directory(self, tmp_path):
        cc = self._cc(tmp_path)
        rms_video = tmp_path / 'RMS_data' / 'VideoFiles'
        hour_dir = rms_video / '2026' / '20260812-224' / '20260812-224_21'
        self._segment(hour_dir, 'RO000Z_20260812_213000_123456_video.mkv')

        seen: list[str] = []
        cc._tee_reconcile('RO000Z', rms_video, add_watch=seen.append)

        assert str(rms_video) in seen
        assert str(hour_dir) in seen

    def test_ignores_non_segment_files(self, tmp_path):
        cc = self._cc(tmp_path)
        rms_video = tmp_path / 'RMS_data' / 'VideoFiles'
        hour_dir = rms_video / '2026' / '20260812-224' / '20260812-224_21'
        self._segment(hour_dir, 'RO000Z_20260812_213000_color.mkv')
        self._segment(hour_dir, 'notes.txt')

        assert cc._tee_reconcile('RO000Z', rms_video) == 0

    def test_disk_full_links_nothing(self, tmp_path):
        cc = self._cc(tmp_path)
        cc.min_disk_gb = 10 ** 9
        rms_video = tmp_path / 'RMS_data' / 'VideoFiles'
        hour_dir = rms_video / '2026' / '20260812-224' / '20260812-224_21'
        self._segment(hour_dir, 'RO000Z_20260812_213000_123456_video.mkv')

        assert cc._tee_reconcile('RO000Z', rms_video) == 0


class TestTeeLedger:
    """A segment is linked at most once, ever. The reconcile sweep must not
    resurrect chunks the janitor purged (the 20260813 gmnmd01 purge/relink
    fight), and pre-NTP 1970-epoch segment names are never linked at all."""

    def _cc(self, tmp_path):
        cc = ColorCapture.__new__(ColorCapture)
        cc.capture_path = tmp_path / 'color_capture'
        cc.capture_path.mkdir(parents=True, exist_ok=True)
        cc.segment_duration = 20
        cc.min_disk_gb = 0
        cc._tee_last_ts = {}
        cc._tee_cadence_warned = set()
        cc._tee_ledger = {}
        cc.log = logging.getLogger('test_tee_ledger')
        return cc

    def _segment(self, root, name, age_s=120):
        root.mkdir(parents=True, exist_ok=True)
        f = root / name
        f.write_bytes(b'\x00' * 64)
        old = time.time() - age_s
        os.utime(f, (old, old))
        return f

    def test_purged_chunk_is_not_resurrected(self, tmp_path):
        cc = self._cc(tmp_path)
        rms_video = tmp_path / 'RMS_data' / 'VideoFiles'
        hour_dir = rms_video / '2026' / '20260812-224' / '20260812-224_21'
        self._segment(hour_dir, 'RO000Z_20260812_213000_123456_video.mkv')

        assert cc._tee_reconcile('RO000Z', rms_video) == 1
        chunk = (cc.capture_path / 'RO000Z' / '20260812'
                 / 'RO000Z_20260812_213000_color.mkv')
        assert chunk.exists()

        chunk.unlink()  # the janitor purges the chunk on purpose
        assert cc._tee_reconcile('RO000Z', rms_video) == 0
        assert not chunk.exists()

    def test_ledger_survives_restart(self, tmp_path):
        cc = self._cc(tmp_path)
        rms_video = tmp_path / 'RMS_data' / 'VideoFiles'
        hour_dir = rms_video / '2026' / '20260812-224' / '20260812-224_21'
        self._segment(hour_dir, 'RO000Z_20260812_213000_123456_video.mkv')
        assert cc._tee_reconcile('RO000Z', rms_video) == 1
        (cc.capture_path / 'RO000Z' / '20260812'
         / 'RO000Z_20260812_213000_color.mkv').unlink()

        cc2 = self._cc(tmp_path)  # fresh instance, same capture_path
        cc2._tee_ledger_load('RO000Z')
        assert cc2._tee_reconcile('RO000Z', rms_video) == 0

    def test_restart_without_ledger_relinks(self, tmp_path):
        # A fresh instance that does NOT load the ledger (pre-upgrade state)
        # still links missing chunks — the ledger only blocks what it holds.
        cc = self._cc(tmp_path)
        rms_video = tmp_path / 'RMS_data' / 'VideoFiles'
        hour_dir = rms_video / '2026' / '20260812-224' / '20260812-224_21'
        self._segment(hour_dir, 'RO000Z_20260812_213000_123456_video.mkv')
        assert cc._tee_reconcile('RO000Z', rms_video) == 1

    def test_existing_chunk_is_ledgered_not_relinked(self, tmp_path):
        # A chunk already on disk (linked by a pre-ledger version) is adopted
        # into the ledger, so purging it later is respected too.
        cc = self._cc(tmp_path)
        rms_video = tmp_path / 'RMS_data' / 'VideoFiles'
        hour_dir = rms_video / '2026' / '20260812-224' / '20260812-224_21'
        self._segment(hour_dir, 'RO000Z_20260812_213000_123456_video.mkv')
        chunk_dir = cc.capture_path / 'RO000Z' / '20260812'
        chunk_dir.mkdir(parents=True)
        chunk = chunk_dir / 'RO000Z_20260812_213000_color.mkv'
        chunk.write_bytes(b'\x00')

        assert cc._tee_reconcile('RO000Z', rms_video) == 0
        assert ('RO000Z_20260812_213000_123456_video.mkv'
                in cc._tee_ledger_set('RO000Z'))
        chunk.unlink()
        assert cc._tee_reconcile('RO000Z', rms_video) == 0

    def test_epoch_segment_never_linked(self, tmp_path):
        cc = self._cc(tmp_path)
        rms_video = tmp_path / 'RMS_data' / 'VideoFiles'
        hour_dir = rms_video / '1970' / '19700101-001' / '19700101-001_00'
        self._segment(hour_dir, 'RO000Z_19700101_000000_157590_video.mkv')

        assert cc._tee_reconcile('RO000Z', rms_video) == 0
        assert list(cc.capture_path.rglob('*_color.mkv')) == []

    def test_prune_drops_names_after_raw_retention(self, tmp_path):
        cc = self._cc(tmp_path)
        rms_video = tmp_path / 'RMS_data' / 'VideoFiles'
        hour_dir = rms_video / '2026' / '20260812-224' / '20260812-224_21'
        seg = self._segment(hour_dir, 'RO000Z_20260812_213000_123456_video.mkv')
        assert cc._tee_reconcile('RO000Z', rms_video) == 1

        seg.unlink()  # RMS video_days_to_keep retention removes the raw
        cc._tee_reconcile('RO000Z', rms_video)
        assert cc._tee_ledger_set('RO000Z') == set()
        assert cc._tee_ledger_path('RO000Z').read_text() == ''
