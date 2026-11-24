"""Tests for archive_upload.py — station-side archive uploader."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import archive_upload
from archive_upload import _Uploader, _ScanResults, _is_locked

# Passes skip without ingest settings; these drive them through stubbed POSTs.
pytestmark = pytest.mark.usefixtures('ingest_ready')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mkv(parent: Path, name: str, size: int = 1024) -> Path:
    """Create a dummy MKV file."""
    f = parent / name
    f.write_bytes(b'\x00' * size)
    return f


def _old(path: Path) -> None:
    """Age a night product past the uploader's minimum age."""
    t = time.time() - 3600
    os.utime(path, (t, t))


def _make_state(night_dir: Path, state: dict) -> Path:
    """Write state.json inside a night dir."""
    sj = night_dir / 'state.json'
    sj.write_text(json.dumps(state, indent=2))
    return sj


def _archive_cfg(tmp_path: Path, *, enabled: bool = True,
                 stations: dict | None = None, **overrides) -> dict:
    """Build a minimal config dict with an archive block."""
    capture = tmp_path / 'color_capture'
    capture.mkdir(exist_ok=True)
    cfg = {
        'videocapture_path': str(capture),
        'stations': stations or {'RO000H': {}},
        'archive': {
            'enabled': enabled,
            'host': '10.0.0.1',
            'port': 2222,
            'user': 'rovimen',
            'base_path': '/mnt/archive',
            'upload_meteors': True,
            'upload_timelapses': True,
            'upload_stacks': False,
            **overrides,
        },
    }
    return cfg


# ---------------------------------------------------------------------------
# _is_locked
# ---------------------------------------------------------------------------

class TestIsLocked:

    def test_locked_via_state_json(self, tmp_path):
        """When state.json records a lock, _is_locked returns True."""
        cfg = {'videocapture_path': str(tmp_path / 'color_capture')}
        night = tmp_path / 'color_capture' / 'RO000H' / '20260315'
        night.mkdir(parents=True)
        mkv = _make_mkv(night, 'RO000H_20260315_210000_color.mkv')
        _make_state(night, {
            'station': 'RO000H', 'date': '20260315',
            'chunks': {mkv.name: {'lock': {'lock_type': 'detection'}}},
        })
        assert _is_locked(mkv, 'RO000H', '20260315', cfg) is True

    def test_not_locked_via_state_json(self, tmp_path):
        """When state.json records lock=None, _is_locked returns False (no sidecar)."""
        cfg = {'videocapture_path': str(tmp_path / 'color_capture')}
        night = tmp_path / 'color_capture' / 'RO000H' / '20260315'
        night.mkdir(parents=True)
        mkv = _make_mkv(night, 'RO000H_20260315_210000_color.mkv')
        _make_state(night, {
            'station': 'RO000H', 'date': '20260315',
            'chunks': {mkv.name: {'lock': None}},
        })
        assert _is_locked(mkv, 'RO000H', '20260315', cfg) is False

    def test_fallback_to_sidecar(self, tmp_path):
        """When state.json is missing, falls back to .locked sidecar."""
        cfg = {'videocapture_path': str(tmp_path / 'color_capture')}
        night = tmp_path / 'color_capture' / 'RO000H' / '20260315'
        night.mkdir(parents=True)
        mkv = _make_mkv(night, 'RO000H_20260315_210000_color.mkv')
        # No state.json — create .locked sidecar
        (night / (mkv.name + '.locked')).write_text('{}')
        assert _is_locked(mkv, 'RO000H', '20260315', cfg) is True

    def test_fallback_no_sidecar(self, tmp_path):
        """When neither state.json nor sidecar exist, returns False."""
        cfg = {'videocapture_path': str(tmp_path / 'color_capture')}
        night = tmp_path / 'color_capture' / 'RO000H' / '20260315'
        night.mkdir(parents=True)
        mkv = _make_mkv(night, 'RO000H_20260315_210000_color.mkv')
        assert _is_locked(mkv, 'RO000H', '20260315', cfg) is False

    def test_state_json_chunk_not_listed(self, tmp_path):
        """Chunk not in state.json falls through to sidecar check."""
        cfg = {'videocapture_path': str(tmp_path / 'color_capture')}
        night = tmp_path / 'color_capture' / 'RO000H' / '20260315'
        night.mkdir(parents=True)
        mkv = _make_mkv(night, 'RO000H_20260315_210000_color.mkv')
        _make_state(night, {
            'station': 'RO000H', 'date': '20260315',
            'chunks': {},  # chunk not listed
        })
        # No sidecar either
        assert _is_locked(mkv, 'RO000H', '20260315', cfg) is False


# ---------------------------------------------------------------------------
# _Uploader init
# ---------------------------------------------------------------------------

class TestUploaderInit:

    def test_enabled_defaults_false(self, tmp_path):
        """When archive block is missing, enabled defaults to False."""
        cfg = {'videocapture_path': str(tmp_path)}
        up = _Uploader(cfg)
        assert up.enabled is False

    def test_enabled_from_config(self, tmp_path):
        cfg = _archive_cfg(tmp_path, enabled=True)
        up = _Uploader(cfg)
        assert up.enabled is True

    def test_upload_flags_defaults(self, tmp_path):
        cfg = _archive_cfg(tmp_path)
        up = _Uploader(cfg)
        assert up.upload_meteors is True
        assert up.upload_timelapses is True
        # _archive_cfg sets upload_stacks explicitly False; explicit config wins.
        assert up.upload_stacks is False

    def test_upload_stacks_defaults_true_when_unset(self, tmp_path):
        """Regression: the code default for upload_stacks must be True (matching
        config_defaults.json). A False default silently drifts stations off the
        dashboard plot pipeline whenever a config omits the flag."""
        cfg = {
            'videocapture_path': str(tmp_path / 'cc'),
            'archive': {'enabled': True},
            'stations': {},
        }
        up = _Uploader(cfg)
        assert up.upload_stacks is True

    def test_videocapture_path_fallback_color_video(self, tmp_path):
        cfg = {
            'color_video_path': str(tmp_path / 'cv'),
            'archive': {'enabled': True},
            'stations': {},
        }
        up = _Uploader(cfg)
        assert up.videocapture_path == tmp_path / 'cv'

    def test_videocapture_path_fallback_reenc(self, tmp_path):
        cfg = {
            'reenc_path': str(tmp_path / 'reenc'),
            'archive': {'enabled': True},
            'stations': {},
        }
        up = _Uploader(cfg)
        assert up.videocapture_path == tmp_path / 'reenc'

    def test_videocapture_path_fallback_color_capture(self, tmp_path):
        cfg = {
            'color_capture_path': str(tmp_path / 'cc'),
            'archive': {'enabled': True},
            'stations': {},
        }
        up = _Uploader(cfg)
        assert up.videocapture_path == tmp_path / 'cc'

    def test_videocapture_path_fallback_ssd(self, tmp_path):
        cfg = {
            'ssd_color_path': str(tmp_path / 'ssd'),
            'archive': {'enabled': True},
            'stations': {},
        }
        up = _Uploader(cfg)
        assert up.videocapture_path == tmp_path / 'ssd'

    def test_videocapture_path_ultimate_fallback(self):
        cfg = {'archive': {'enabled': True}, 'stations': {}}
        up = _Uploader(cfg)
        assert up.videocapture_path == Path.home() / 'color_capture'


# ---------------------------------------------------------------------------
# _Uploader._mark_uploaded
# ---------------------------------------------------------------------------

class TestMarkUploaded:

    def _night(self, tmp_path):
        cfg = _archive_cfg(tmp_path)
        night = tmp_path / 'color_capture' / 'RO000H' / '20260315'
        night.mkdir(parents=True)
        return cfg, night

    def _state(self, night):
        return json.loads((night / 'state.json').read_text())

    def test_color_mkv(self, tmp_path):
        cfg, night = self._night(tmp_path)
        _Uploader(cfg)._mark_uploaded(night / 'RO000H_20260315_210000_color.mkv',
                                      'RO000H', '20260315')
        assert self._state(night)['chunks']['RO000H_20260315_210000_color.mkv']['uploaded'] is True

    def test_night_stack_webp(self, tmp_path):
        """_night_stack.webp is the night_stack product (and sets the legacy flag)."""
        cfg, night = self._night(tmp_path)
        ns = night / 'RO000H_20260315_night_stack.webp'
        ns.write_bytes(b'x')
        _Uploader(cfg)._mark_uploaded(ns, 'RO000H', '20260315')
        state = self._state(night)
        assert state['night_stack_uploaded'] is True
        assert state['products_uploaded']['night_stack'][0] == 1
        assert state['chunks'] == {}

    def test_chunk_stack_webp(self, tmp_path):
        """Regular _stack.webp marks its chunk's stack_uploaded."""
        cfg, night = self._night(tmp_path)
        _Uploader(cfg)._mark_uploaded(night / 'stacks' / 'RO000H_20260315_210000_stack.webp',
                                      'RO000H', '20260315')
        chunk = self._state(night)['chunks']['RO000H_20260315_210000_color.mkv']
        assert chunk['stack_uploaded'] is True and chunk['uploaded'] is False

    def test_timelapse_mp4(self, tmp_path):
        cfg, night = self._night(tmp_path)
        tl = night / 'RO000H_20260315_timelapse.mp4'
        tl.write_bytes(b'xyz')
        _Uploader(cfg)._mark_uploaded(tl, 'RO000H', '20260315')
        state = self._state(night)
        assert state['timelapse_uploaded'] is True
        assert state['products_uploaded']['timelapse'][0] == 3

    def test_night_stack_checked_before_regular_stack(self, tmp_path):
        """Night stack suffix _night_stack.webp ends with _stack.webp too.

        Ensure the night stack branch is hit, not the regular stack branch.
        """
        cfg, night = self._night(tmp_path)
        ns = night / 'RO000H_20260315_night_stack.webp'
        ns.write_bytes(b'x')
        _Uploader(cfg)._mark_uploaded(ns, 'RO000H', '20260315')
        assert self._state(night)['chunks'] == {}

    def test_oserror_is_logged_not_raised(self, tmp_path):
        """OSError in flags_manager is caught and logged."""
        cfg = _archive_cfg(tmp_path)
        up = _Uploader(cfg)
        mkv = Path('/tmp/RO000H_20260315_210000_color.mkv')
        with patch('flags_manager.mark_uploaded_batch', side_effect=OSError('disk full')):
            # Should not raise
            up._mark_uploaded(mkv, 'RO000H', '20260315')


# ---------------------------------------------------------------------------
# _Uploader._upload_rms_files — re-POST churn guard
# ---------------------------------------------------------------------------

class TestUploadRmsFiles:

    def _rms_file(self, tmp_path, size: int) -> Path:
        night = tmp_path / 'color_capture' / 'RO000H' / '20260315'
        night.mkdir(parents=True, exist_ok=True)
        f = night / 'FTPdetectinfo_RO000H_20260315_200000_000000.txt'
        f.write_bytes(b'x' * size)
        return f

    def test_skips_already_uploaded_same_size(self, tmp_path):
        """The churn bug: without an uploaded-marker the daemon re-POSTs every
        night's RMS text on every cycle. Second cycle must upload nothing."""
        cfg = _archive_cfg(tmp_path)
        up = _Uploader(cfg)
        f = self._rms_file(tmp_path, 100)
        with patch.object(up, '_find_rms_files', return_value=[f]), \
             patch.object(up, '_post_one', return_value=True) as post:
            n1 = up._upload_rms_files('RO000H', '20260315')
            n2 = up._upload_rms_files('RO000H', '20260315')
        assert n1 == 1
        assert n2 == 0
        assert post.call_count == 1

    def test_reuploads_when_size_grows(self, tmp_path):
        """A file that grew (new detections through the night) re-uploads."""
        cfg = _archive_cfg(tmp_path)
        up = _Uploader(cfg)
        f = self._rms_file(tmp_path, 100)
        with patch.object(up, '_find_rms_files', return_value=[f]), \
             patch.object(up, '_post_one', return_value=True) as post:
            up._upload_rms_files('RO000H', '20260315')
            f.write_bytes(b'x' * 250)  # grew
            n = up._upload_rms_files('RO000H', '20260315')
        assert n == 1
        assert post.call_count == 2

    def test_failed_post_is_retried_next_cycle(self, tmp_path):
        """A failed upload must not be marked, so it retries next cycle."""
        cfg = _archive_cfg(tmp_path)
        up = _Uploader(cfg)
        f = self._rms_file(tmp_path, 100)
        with patch.object(up, '_find_rms_files', return_value=[f]), \
             patch.object(up, '_post_one', return_value=False) as post:
            up._upload_rms_files('RO000H', '20260315')
            up._upload_rms_files('RO000H', '20260315')
        assert post.call_count == 2


# ---------------------------------------------------------------------------
# _Uploader._find_rms_files
# ---------------------------------------------------------------------------

class TestFindRmsFiles:
    """The Calibration page (dashboard) resolves 'latest night with an
    FF-intervals plot' from the archive alone (cache_store.py's
    _ArchiveIndex), not a live station call -- so the plot PNG has to ride
    along in the same upload sweep as FTPdetectinfo/radiants."""

    def _session_dir(self, tmp_path, sub: str = 'ArchivedFiles') -> Path:
        rms_root = tmp_path / 'rms_data'
        session = rms_root / sub / 'RO000H_20260315_200000_000000'
        session.mkdir(parents=True, exist_ok=True)
        return session

    def _cfg_with_rms_path(self, tmp_path) -> dict:
        return _archive_cfg(tmp_path, stations={
            'RO000H': {'rms_data_path': str(tmp_path / 'rms_data')},
        })

    def test_finds_ff_intervals_png(self, tmp_path):
        session = self._session_dir(tmp_path)
        png = session / 'RO000H_20260315_200000_000000_ff_intervals.png'
        png.write_bytes(b'\x89PNG')
        up = _Uploader(self._cfg_with_rms_path(tmp_path))
        files = up._find_rms_files('RO000H', '20260315')
        assert png in files

    def test_finds_calibration_variation_png(self, tmp_path):
        """The astrometry-residual plot the Calibration page's second frame
        shows (RO000H card layout: stack+mask, then FF intervals + this side
        by side) rides the same upload sweep as ff_intervals."""
        session = self._session_dir(tmp_path)
        png = session / 'RO000H_20260315_200000_000000_calibration_variation.png'
        png.write_bytes(b'\x89PNG')
        up = _Uploader(self._cfg_with_rms_path(tmp_path))
        files = up._find_rms_files('RO000H', '20260315')
        assert png in files

    def test_still_finds_ftpdetectinfo_and_radiants(self, tmp_path):
        session = self._session_dir(tmp_path)
        ftp = session / 'FTPdetectinfo_RO000H_20260315_200000_000000.txt'
        ftp.write_text('x')
        rad = session / 'RO000H_20260315_200000_000000_radiants.txt'
        rad.write_text('x')
        up = _Uploader(self._cfg_with_rms_path(tmp_path))
        files = up._find_rms_files('RO000H', '20260315')
        assert set(files) == {ftp, rad}

    def test_no_plot_yet_returns_empty(self, tmp_path):
        self._session_dir(tmp_path)  # session dir exists but no plot inside
        up = _Uploader(self._cfg_with_rms_path(tmp_path))
        assert up._find_rms_files('RO000H', '20260315') == []


# ---------------------------------------------------------------------------
# _Uploader._fmt_size
# ---------------------------------------------------------------------------

class TestFmtSize:

    def test_gb_range(self, tmp_path):
        cfg = _archive_cfg(tmp_path)
        up = _Uploader(cfg)
        assert up._fmt_size(2 * 1024 ** 3) == '2.0 GB'
        assert up._fmt_size(int(1.5 * 1024 ** 3)) == '1.5 GB'

    def test_mb_range(self, tmp_path):
        cfg = _archive_cfg(tmp_path)
        up = _Uploader(cfg)
        assert up._fmt_size(150 * 1024 ** 2) == '150 MB'
        assert up._fmt_size(1 * 1024 ** 2) == '1 MB'

    def test_kb_range(self, tmp_path):
        cfg = _archive_cfg(tmp_path)
        up = _Uploader(cfg)
        assert up._fmt_size(500 * 1024) == '500 KB'
        assert up._fmt_size(1024) == '1 KB'

    def test_boundary_gb(self, tmp_path):
        cfg = _archive_cfg(tmp_path)
        up = _Uploader(cfg)
        # Exactly 1 GB
        assert up._fmt_size(1024 ** 3) == '1.0 GB'

    def test_boundary_mb(self, tmp_path):
        cfg = _archive_cfg(tmp_path)
        up = _Uploader(cfg)
        # Exactly 1 MB
        assert up._fmt_size(1024 ** 2) == '1 MB'


# ---------------------------------------------------------------------------
# _Uploader._scan_all().locked_mkvs
# ---------------------------------------------------------------------------

class TestScanLockedMkvs:

    def test_returns_locked_reencoded_not_uploaded(self, tmp_path):
        """MKV that is locked + reencoded + not uploaded should appear in results."""
        cfg = _archive_cfg(tmp_path)
        capture = tmp_path / 'color_capture'
        night = capture / 'RO000H' / '20260315'
        night.mkdir(parents=True)
        mkv = _make_mkv(night, 'RO000H_20260315_210000_color.mkv')
        _make_state(night, {
            'station': 'RO000H', 'date': '20260315',
            'chunks': {
                mkv.name: {
                    'lock': {'lock_type': 'detection'},
                    'reencoded': True,
                    'uploaded': False,
                    'stack_uploaded': False,
                },
            },
        })
        up = _Uploader(cfg)
        results = up._scan_all().locked_mkvs
        assert len(results) == 1
        assert results[0][0] == mkv
        assert results[0][1] == 'RO000H'
        assert results[0][2] == '20260315'

    def test_skips_already_uploaded(self, tmp_path):
        """MKV already marked as uploaded should not appear."""
        cfg = _archive_cfg(tmp_path)
        capture = tmp_path / 'color_capture'
        night = capture / 'RO000H' / '20260315'
        night.mkdir(parents=True)
        mkv = _make_mkv(night, 'RO000H_20260315_210000_color.mkv')
        _make_state(night, {
            'station': 'RO000H', 'date': '20260315',
            'chunks': {mkv.name: {
                'lock': {'lock_type': 'detection'},
                'reencoded': True,
                'uploaded': True,
            }},
        })
        up = _Uploader(cfg)
        assert up._scan_all().locked_mkvs == []

    def test_skips_unlocked(self, tmp_path):
        """MKV with lock=None should not appear."""
        cfg = _archive_cfg(tmp_path)
        capture = tmp_path / 'color_capture'
        night = capture / 'RO000H' / '20260315'
        night.mkdir(parents=True)
        mkv = _make_mkv(night, 'RO000H_20260315_210000_color.mkv')
        _make_state(night, {
            'station': 'RO000H', 'date': '20260315',
            'chunks': {mkv.name: {
                'lock': None,
                'reencoded': True,
                'uploaded': False,
            }},
        })
        up = _Uploader(cfg)
        assert up._scan_all().locked_mkvs == []

    def test_skips_not_reencoded(self, tmp_path):
        """MKV that is locked but not reencoded should not appear."""
        cfg = _archive_cfg(tmp_path)
        capture = tmp_path / 'color_capture'
        night = capture / 'RO000H' / '20260315'
        night.mkdir(parents=True)
        mkv = _make_mkv(night, 'RO000H_20260315_210000_color.mkv')
        _make_state(night, {
            'station': 'RO000H', 'date': '20260315',
            'chunks': {mkv.name: {
                'lock': {'lock_type': 'detection'},
                'reencoded': False,
                'uploaded': False,
            }},
        })
        up = _Uploader(cfg)
        assert up._scan_all().locked_mkvs == []

    def test_skips_non_8digit_dirs(self, tmp_path):
        """Directories that don't look like YYYYMMDD dates are skipped."""
        cfg = _archive_cfg(tmp_path)
        capture = tmp_path / 'color_capture'
        bad_dir = capture / 'RO000H' / 'notes'
        bad_dir.mkdir(parents=True)
        _make_mkv(bad_dir, 'RO000H_20260315_210000_color.mkv')
        up = _Uploader(cfg)
        assert up._scan_all().locked_mkvs == []

    def test_skips_nonexistent_station_dir(self, tmp_path):
        """Station dir not on disk is silently skipped."""
        cfg = _archive_cfg(tmp_path)
        up = _Uploader(cfg)
        assert up._scan_all().locked_mkvs == []

    def test_multiple_stations_and_dates(self, tmp_path):
        """Scan across multiple stations and dates."""
        cfg = _archive_cfg(tmp_path, stations={'RO000H': {}, 'RO000J': {}})
        capture = tmp_path / 'color_capture'
        for station, date in [('RO000H', '20260315'), ('RO000J', '20260316')]:
            night = capture / station / date
            night.mkdir(parents=True)
            mkv = _make_mkv(night, f'{station}_{date}_210000_color.mkv')
            _make_state(night, {
                'station': station, 'date': date,
                'chunks': {mkv.name: {
                    'lock': {'lock_type': 'detection'},
                    'reencoded': True,
                    'uploaded': False,
                }},
            })
        up = _Uploader(cfg)
        results = up._scan_all().locked_mkvs
        assert len(results) == 2
        stations_found = {r[1] for r in results}
        assert stations_found == {'RO000H', 'RO000J'}


# ---------------------------------------------------------------------------
# _Uploader._scan_all().stacks
# ---------------------------------------------------------------------------

class TestScanStacks:

    def test_finds_unuploaded_stacks(self, tmp_path):
        cfg = _archive_cfg(tmp_path)
        capture = tmp_path / 'color_capture'
        stacks = capture / 'RO000H' / '20260315' / 'stacks'
        stacks.mkdir(parents=True)
        webp = stacks / 'RO000H_20260315_210000_stack.webp'
        webp.write_bytes(b'\x00' * 100)
        night = capture / 'RO000H' / '20260315'
        _make_state(night, {
            'station': 'RO000H', 'date': '20260315',
            'chunks': {'RO000H_20260315_210000_color.mkv': {
                'stack_uploaded': False,
            }},
        })
        up = _Uploader(cfg)
        results = up._scan_all().stacks
        assert len(results) == 1
        assert results[0][0] == webp

    def test_skips_uploaded_stacks(self, tmp_path):
        cfg = _archive_cfg(tmp_path)
        capture = tmp_path / 'color_capture'
        stacks = capture / 'RO000H' / '20260315' / 'stacks'
        stacks.mkdir(parents=True)
        webp = stacks / 'RO000H_20260315_210000_stack.webp'
        webp.write_bytes(b'\x00' * 100)
        night = capture / 'RO000H' / '20260315'
        _make_state(night, {
            'station': 'RO000H', 'date': '20260315',
            'chunks': {'RO000H_20260315_210000_color.mkv': {
                'stack_uploaded': True,
            }},
        })
        up = _Uploader(cfg)
        assert up._scan_all().stacks == []


# ---------------------------------------------------------------------------
# _Uploader._scan_all().timelapses
# ---------------------------------------------------------------------------

class TestScanTimelapses:

    def test_finds_timelapse_mp4(self, tmp_path):
        cfg = _archive_cfg(tmp_path)
        capture = tmp_path / 'color_capture'
        night = capture / 'RO000H' / '20260315'
        night.mkdir(parents=True)
        tl = night / 'RO000H_20260315_timelapse.mp4'
        tl.write_bytes(b'\x00' * 100)
        _old(tl)
        _make_state(night, {
            'station': 'RO000H', 'date': '20260315',
            'timelapse_uploaded': False,
        })
        up = _Uploader(cfg)
        results = up._scan_all().timelapses
        assert any(r[0] == tl for r in results)

    def test_finds_night_stack_webp(self, tmp_path):
        cfg = _archive_cfg(tmp_path)
        capture = tmp_path / 'color_capture'
        night = capture / 'RO000H' / '20260315'
        night.mkdir(parents=True)
        ns = night / 'RO000H_20260315_night_stack.webp'
        ns.write_bytes(b'\x00' * 100)
        _old(ns)
        _make_state(night, {
            'station': 'RO000H', 'date': '20260315',
            'night_stack_uploaded': False,
        })
        up = _Uploader(cfg)
        results = up._scan_all().timelapses
        assert any(r[0] == ns for r in results)

    def test_skips_uploaded_timelapse(self, tmp_path):
        cfg = _archive_cfg(tmp_path)
        capture = tmp_path / 'color_capture'
        night = capture / 'RO000H' / '20260315'
        night.mkdir(parents=True)
        tl = night / 'RO000H_20260315_timelapse.mp4'
        tl.write_bytes(b'\x00' * 100)
        _old(tl)
        _make_state(night, {
            'station': 'RO000H', 'date': '20260315',
            'timelapse_uploaded': True,
        })
        up = _Uploader(cfg)
        results = up._scan_all().timelapses
        timelapse_results = [r for r in results if r[0].name.endswith('_timelapse.mp4')]
        assert timelapse_results == []


# ---------------------------------------------------------------------------
# _Uploader._scan_all().state_jsons
# ---------------------------------------------------------------------------

class TestScanStateJsons:

    def test_finds_state_jsons(self, tmp_path):
        cfg = _archive_cfg(tmp_path)
        capture = tmp_path / 'color_capture'
        night = capture / 'RO000H' / '20260315'
        night.mkdir(parents=True)
        _make_state(night, {'station': 'RO000H', 'date': '20260315'})
        up = _Uploader(cfg)
        results = up._scan_all().state_jsons
        assert len(results) == 1
        assert results[0][0] == night / 'state.json'
        assert results[0][1] == 'RO000H'
        assert results[0][2] == '20260315'

    def test_skips_non_date_dirs(self, tmp_path):
        cfg = _archive_cfg(tmp_path)
        capture = tmp_path / 'color_capture'
        bad_dir = capture / 'RO000H' / 'logs'
        bad_dir.mkdir(parents=True)
        (bad_dir / 'state.json').write_text('{}')
        up = _Uploader(cfg)
        assert up._scan_all().state_jsons == []

    def test_skips_missing_state_json(self, tmp_path):
        cfg = _archive_cfg(tmp_path)
        capture = tmp_path / 'color_capture'
        night = capture / 'RO000H' / '20260315'
        night.mkdir(parents=True)
        # No state.json
        up = _Uploader(cfg)
        assert up._scan_all().state_jsons == []


# ---------------------------------------------------------------------------
# _Uploader._scan_all
# ---------------------------------------------------------------------------

class TestScanAll:

    def test_returns_all_categories(self, tmp_path):
        cfg = _archive_cfg(tmp_path)
        capture = tmp_path / 'color_capture'
        night = capture / 'RO000H' / '20260315'
        night.mkdir(parents=True)
        stacks_dir = night / 'stacks'
        stacks_dir.mkdir()

        mkv = _make_mkv(night, 'RO000H_20260315_210000_color.mkv')
        webp = stacks_dir / 'RO000H_20260315_210000_stack.webp'
        webp.write_bytes(b'\x00' * 100)
        tl = night / 'RO000H_20260315_timelapse.mp4'
        tl.write_bytes(b'\x00' * 100)
        _old(tl)
        ns = night / 'RO000H_20260315_night_stack.webp'
        ns.write_bytes(b'\x00' * 100)
        _old(ns)
        _make_state(night, {
            'station': 'RO000H', 'date': '20260315',
            'chunks': {
                mkv.name: {
                    'lock': {'lock_type': 'detection'},
                    'reencoded': True,
                    'uploaded': False,
                    'stack_uploaded': False,
                },
            },
            'timelapse_uploaded': False,
            'night_stack_uploaded': False,
        })

        up = _Uploader(cfg)
        scan = up._scan_all()
        assert len(scan.locked_mkvs) == 1
        assert len(scan.stacks) == 1
        assert len(scan.timelapses) == 2
        assert len(scan.state_jsons) == 1

    def test_empty_tree(self, tmp_path):
        cfg = _archive_cfg(tmp_path)
        up = _Uploader(cfg)
        scan = up._scan_all()
        assert scan.locked_mkvs == []
        assert scan.stacks == []
        assert scan.timelapses == []
        assert scan.state_jsons == []

    def test_orders_nights_newest_first(self, tmp_path):
        """Backlog safety: _scan_all yields newest nights first so the current
        night's data uploads before historical backlog. Without this a daemon
        with a large backlog (e.g. a dead camera's un-reaped sessions) can spend
        a whole cycle on ancient data and never reach the current night's stacks
        before the nightly reboot restarts the scan."""
        cfg = _archive_cfg(tmp_path)
        capture = tmp_path / 'color_capture'
        # Create nights out of chronological order on disk.
        for date in ('20260310', '20260315', '20260312'):
            night = capture / 'RO000H' / date
            (night / 'stacks').mkdir(parents=True)
            (night / 'stacks' / f'RO000H_{date}_210000_stack.webp').write_bytes(b'\x00' * 10)
            _make_state(night, {'station': 'RO000H', 'date': date, 'chunks': {}})

        up = _Uploader(cfg)
        scan = up._scan_all()

        assert [d for _p, _s, d in scan.stacks] == ['20260315', '20260312', '20260310']
        assert [d for _p, _s, d in scan.state_jsons] == ['20260315', '20260312', '20260310']


# ---------------------------------------------------------------------------
# Plots-only station: RMS files upload independently of the colour pipeline
# (upload_rms gate + RMS-tree night discovery with no state.json)
# ---------------------------------------------------------------------------

class TestPlotsOnlyRmsUpload:
    """A plots-only station ships only the RMS detection/plot files: it runs with
    upload_meteors / upload_stacks / upload_timelapses all off and never runs
    colour capture, so no state.json is ever written anywhere."""

    NIGHT = '20260805'
    SESSION = 'RO0001_20260805_182140_000814'

    def _plots_only_cfg(self, tmp_path: Path, rms_path: Path) -> dict:
        return {
            # A dir that is never created — a plots-only station has no colour
            # capture tree, so night discovery cannot come from state.json.
            'videocapture_path': str(tmp_path / 'color_capture'),
            'stations': {'RO0001': {'rms_data_path': str(rms_path)}},
            'archive': {
                'enabled': True,
                'host': '10.0.0.1', 'port': 2222, 'user': 'rovimen',
                'base_path': '/mnt/archive',
                'upload_meteors': False,
                'upload_stacks': False,
                'upload_timelapses': False,
                'upload_rms': True,
            },
        }

    def _rms_tree(self, tmp_path: Path, *, sub: str = 'ArchivedFiles') -> tuple[Path, set[str]]:
        """Create one RMS session dir with the detection text files and every
        check plot the dashboard labels, plus decoys that must not upload.
        Returns (rms_data_path, {expected filenames})."""
        session = tmp_path / 'RMS_data' / sub / self.SESSION
        session.mkdir(parents=True)
        keep = {
            f'{self.SESSION}_radiants.txt',
            f'FTPdetectinfo_{self.SESSION}.txt',
            f'{self.SESSION}_ff_intervals.png',
            f'{self.SESSION}_calibration_variation.png',
            f'{self.SESSION}_radiants.png',
            f'{self.SESSION}_fieldsums.png',
            f'{self.SESSION}_photometry_variation.png',
            f'{self.SESSION}_captured_stack.jpg',
            f'{self.SESSION}_stack_62_meteors.jpg',
        }
        drop = {
            f'FTPdetectinfo_{self.SESSION}_unfiltered.txt',
            f'FTPdetectinfo_{self.SESSION}_backup_20260806_025111.161648.txt',
            f'{self.SESSION}.config',                 # not a plot
            'platepar_cmn2010.cal',
            f'{self.SESSION}_detected_stack.txt',     # unknown suffix
        }
        for name in keep | drop:
            (session / name).write_bytes(b'x' * 32)
        return tmp_path / 'RMS_data', keep

    def test_find_rms_files_lands_the_detection_files_and_every_plot(self, tmp_path):
        """FTPdetectinfo, radiants and every catalogued check plot reach the
        archive — and nothing else in the session dir."""
        rms_path, keep = self._rms_tree(tmp_path)
        up = _Uploader(self._plots_only_cfg(tmp_path, rms_path))
        got = {p.name for p in up._find_rms_files('RO0001', self.NIGHT)}
        assert got == keep

    def test_cycle_uploads_rms_with_meteors_stacks_timelapses_off(self, tmp_path):
        """The independent gate: cycle() ships the RMS files even though every
        other upload flag is false (the plots-only case)."""
        rms_path, keep = self._rms_tree(tmp_path)
        up = _Uploader(self._plots_only_cfg(tmp_path, rms_path))
        posted: list[tuple[str, str]] = []

        def fake_post(path, camera, date, subdir):
            posted.append((path.name, subdir))
            return True

        with patch.object(up, '_post_one', side_effect=fake_post):
            n = up.cycle()

        assert n == len(keep)
        assert {name for name, _ in posted} == keep
        assert {subdir for _, subdir in posted} == {'rms'}

    def test_discovers_night_from_rms_tree_without_state_json(self, tmp_path):
        """Night discovery with no state.json anywhere: a plots-only station never
        writes one, so cycle() must learn the night from the RMS tree instead."""
        rms_path, _keep = self._rms_tree(tmp_path)
        up = _Uploader(self._plots_only_cfg(tmp_path, rms_path))
        assert up._scan_all().state_jsons == []          # no colour-capture nights
        assert up._discover_rms_nights() == {('RO0001', self.NIGHT)}

    def test_discovers_night_from_capturedfiles_before_archive_mirror(self, tmp_path):
        """RMS writes CapturedFiles first and mirrors to ArchivedFiles later —
        discovery must work from CapturedFiles alone."""
        rms_path, _keep = self._rms_tree(tmp_path, sub='CapturedFiles')
        up = _Uploader(self._plots_only_cfg(tmp_path, rms_path))
        assert up._discover_rms_nights() == {('RO0001', self.NIGHT)}

    def test_post_midnight_session_resolves_to_previous_night(self, tmp_path):
        """A session started after 00:00 UTC belongs to the previous observing
        night, so discovery must not create a spurious next-day night."""
        session = tmp_path / 'RMS_data' / 'ArchivedFiles' / 'RO0001_20260806_010500_000000'
        session.mkdir(parents=True)
        (session / 'FTPdetectinfo_RO0001_20260806_010500_000000.txt').write_bytes(b'x')
        up = _Uploader(self._plots_only_cfg(tmp_path, tmp_path / 'RMS_data'))
        assert up._discover_rms_nights() == {('RO0001', '20260805')}

    def test_upload_rms_false_ships_nothing(self, tmp_path):
        """The gate closes both ways: upload_rms=false uploads no RMS files even
        when a night is present in the tree."""
        rms_path, _keep = self._rms_tree(tmp_path)
        cfg = self._plots_only_cfg(tmp_path, rms_path)
        cfg['archive']['upload_rms'] = False
        up = _Uploader(cfg)
        with patch.object(up, '_post_one', return_value=True) as post:
            n = up.cycle()
        assert n == 0
        assert post.call_count == 0


# ---------------------------------------------------------------------------
# starprint details variants (<stem>_color.details.mkv)
#
# overlay_details.py renders a derived per-clip file that grows the bottom bar
# with one row per detected meteor. archive_upload ships it off-station ONLY
# after the base chunk it annotates is already uploaded + locked, publishes a
# faststart .details.mp4 sibling, and — because the file is derived and
# re-creatable — deletes the local .details.mkv after a confirmed upload. No
# flags_manager marker: a still-present local file IS the "not yet shipped"
# state.
# ---------------------------------------------------------------------------

class TestDetailsScan:

    def _night(self, tmp_path, cfg):
        capture = Path(cfg['videocapture_path'])
        night = capture / 'RO000H' / '20260315'
        night.mkdir(parents=True, exist_ok=True)
        return night

    def _base_and_details(self, night):
        base = _make_mkv(night, 'RO000H_20260315_210000_color.mkv')
        details = _make_mkv(night, 'RO000H_20260315_210000_color.details.mkv')
        return base, details

    def test_details_scan_does_not_pick_up_base_glob(self, tmp_path):
        """The base '*_color.mkv' glob must not match a '*.details.mkv' file —
        different suffix, no overlap. The details scan is the only path that
        enqueues them."""
        cfg = _archive_cfg(tmp_path)
        night = self._night(tmp_path, cfg)
        base, details = self._base_and_details(night)
        _make_state(night, {
            'station': 'RO000H', 'date': '20260315',
            'chunks': {base.name: {
                'lock': {'lock_type': 'detection'},
                'reencoded': True, 'uploaded': False,
            }},
        })
        up = _Uploader(cfg)
        scan = up._scan_all()
        # The base is a locked+reencoded+not-uploaded chunk -> locked_mkvs.
        assert [p.name for p, _s, _d in scan.locked_mkvs] == [base.name]
        # The details file is NOT in locked_mkvs (base glob doesn't match it)
        # and NOT in details_mkvs (base is not uploaded yet).
        assert all(not p.name.endswith('.details.mkv')
                   for p, _s, _d in scan.locked_mkvs)
        assert scan.details_mkvs == []

    def test_details_enqueued_when_base_uploaded_and_locked(self, tmp_path):
        """A details file is shipped only once its base chunk is uploaded AND
        locked — we annotate real, already-shipped detections."""
        cfg = _archive_cfg(tmp_path)
        night = self._night(tmp_path, cfg)
        base, details = self._base_and_details(night)
        _make_state(night, {
            'station': 'RO000H', 'date': '20260315',
            'chunks': {base.name: {
                'lock': {'lock_type': 'detection'},
                'reencoded': True, 'uploaded': True,  # base already shipped
            }},
        })
        up = _Uploader(cfg)
        scan = up._scan_all()
        assert [p.name for p, _s, _d in scan.details_mkvs] == [details.name]
        # The uploaded base is not re-enqueued in locked_mkvs.
        assert scan.locked_mkvs == []

    def test_details_not_enqueued_when_base_not_uploaded(self, tmp_path):
        """No base upload yet -> no details shipment (idempotent: it waits)."""
        cfg = _archive_cfg(tmp_path)
        night = self._night(tmp_path, cfg)
        base, details = self._base_and_details(night)
        _make_state(night, {
            'station': 'RO000H', 'date': '20260315',
            'chunks': {base.name: {
                'lock': {'lock_type': 'detection'},
                'reencoded': True, 'uploaded': False,  # base NOT shipped
            }},
        })
        up = _Uploader(cfg)
        assert up._scan_all().details_mkvs == []

    def test_details_not_enqueued_when_base_unlocked(self, tmp_path):
        """Base uploaded but not locked -> not a real detection -> no details."""
        cfg = _archive_cfg(tmp_path)
        night = self._night(tmp_path, cfg)
        base, details = self._base_and_details(night)
        _make_state(night, {
            'station': 'RO000H', 'date': '20260315',
            'chunks': {base.name: {
                'lock': None,
                'reencoded': True, 'uploaded': True,
            }},
        })
        up = _Uploader(cfg)
        assert up._scan_all().details_mkvs == []

    def test_upload_details_variant_ships_and_deletes(self, monkeypatch, tmp_path):
        """A confirmed upload ships the .details.mkv to meteors/ and deletes
        the LOCAL .details.mkv. No .details.mp4 is made or sent: the VPS
        derives it at ingest (dashboard/faststart_sibling.py)."""
        cfg = _archive_cfg(tmp_path, publish_faststart_mp4=True)
        up = _Uploader(cfg)
        night = self._night(tmp_path, cfg)
        details = _make_mkv(night, 'RO000H_20260315_210000_color.details.mkv')

        posted: list[tuple[str, str]] = []
        monkeypatch.setattr(up, '_post_one',
                            lambda local, cam, date, subdir:
                            (posted.append((Path(local).name, subdir)), True)[1])

        assert up._upload_details_variant(details, 'RO000H', '20260315') is True
        assert posted == [('RO000H_20260315_210000_color.details.mkv', 'meteors')]
        assert not details.exists()          # local derived file removed
        assert not list(night.glob('*.mp4'))  # nothing remuxed on the station

    def test_upload_details_variant_keeps_local_on_failed_upload(self, monkeypatch, tmp_path):
        """Crash-safe: a failed .details.mkv upload must NOT delete the local
        file, so the next cycle retries it."""
        cfg = _archive_cfg(tmp_path)
        up = _Uploader(cfg)
        night = self._night(tmp_path, cfg)
        details = _make_mkv(night, 'RO000H_20260315_210000_color.details.mkv')
        monkeypatch.setattr(up, '_post_one', lambda *a, **k: False)
        assert up._upload_details_variant(details, 'RO000H', '20260315') is False
        assert details.exists()               # kept for retry

    def test_cycle_ships_details_after_base_uploaded(self, monkeypatch, tmp_path):
        """End-to-end through cycle(): with the base uploaded+locked, the details
        variant ships to meteors/ and its local copy is deleted."""
        cfg = _archive_cfg(tmp_path, upload_stacks=False, publish_faststart_mp4=False)
        up = _Uploader(cfg)
        night = self._night(tmp_path, cfg)
        base, details = self._base_and_details(night)
        _make_state(night, {
            'station': 'RO000H', 'date': '20260315',
            'chunks': {base.name: {
                'lock': {'lock_type': 'detection'},
                'reencoded': True, 'uploaded': True,
            }},
        })
        posted: list[tuple[str, str]] = []
        monkeypatch.setattr(up, '_post_one',
                            lambda local, cam, date, subdir:
                            (posted.append((Path(local).name, subdir)), True)[1])
        up.cycle()
        assert (details.name, 'meteors') in posted
        assert not details.exists()
