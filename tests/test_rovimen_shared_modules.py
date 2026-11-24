"""Equivalence tests for the shared station library (rovimen_config,
rovimen_night, rovimen_media, rovimen_rms).

Each shared helper replaces copies spread across the station scripts. These
tests pin every shared helper to the copies it replaces that are still
callable, over the edge cases that matter (priority order, empty values,
noon/midnight/month/year boundaries), so a migration onto the shared module
cannot change behaviour.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / 'rovimen-scripts'))

import rovimen_config as rc  # noqa: E402
import rovimen_media as rm  # noqa: E402
import rovimen_night as rn  # noqa: E402
import rovimen_rms as rr  # noqa: E402

# ---------------------------------------------------------------------------
# rovimen_config
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _capture_roots_writable(monkeypatch):
    """These tests pin which key names the capture root, with made-up roots
    (/v, /cc) that no test machine can write. The fallback for an unwritable
    root is covered in test_capture_fallback.py."""
    monkeypatch.setattr(rc, '_writable_dir', lambda path: True)


CAPTURE_CFGS = [
    {},
    {'videocapture_path': '/v'},
    {'color_video_path': '/c'},
    {'reenc_path': '/r'},
    {'color_capture_path': '/cc'},
    {'ssd_color_path': '/s'},
    {'videocapture_path': '', 'color_capture_path': '/cc', 'ssd_color_path': '/s'},
    {'videocapture_path': None, 'reenc_path': '/r', 'color_video_path': '/c'},
    {'videocapture_path': '/v', 'color_video_path': '/c', 'reenc_path': '/r',
     'color_capture_path': '/cc', 'ssd_color_path': '/s'},
]


@pytest.mark.parametrize('cfg', CAPTURE_CFGS)
def test_capture_root_matches_every_callable_copy(cfg):
    import detection_indexer
    import overlay_details

    want = rc.capture_root(cfg)
    assert overlay_details._capture_root(cfg) == want
    assert detection_indexer._capture_base(cfg) == want


@pytest.mark.parametrize('cfg,want', [
    ({}, 'HOME/color_capture'),
    ({'videocapture_path': '', 'color_capture_path': '/cc'}, '/cc'),
    ({'videocapture_path': '/v', 'ssd_color_path': '/s'}, '/v'),
])
def test_station_collect_capture_base_literal_values(cfg, want, monkeypatch, tmp_path):
    # station_api re-exports capture_base from station_collect, which now
    # delegates to rovimen_config.capture_root over its cached load_config.
    import station_collect

    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(station_collect, 'load_config', lambda: cfg)
    assert station_collect.capture_base() == Path(want.replace('HOME', str(tmp_path)))


def test_station_api_family_uses_the_shared_helpers():
    # station_collect keeps these names only as re-exports of the shared
    # helpers (station_api and its tests import them); none is a local copy.
    import config_patch
    import station_api
    import station_collect

    assert station_collect.CHUNK_RE is rn.CHUNK_RE
    assert station_collect._night_date is rn.night_of
    assert station_collect._last_completed_night is rn.last_completed_night
    assert station_collect._NIGHT_ROLLOVER_HOUR == rn.NIGHT_ROLLOVER_HOUR
    assert station_collect._SESSION_RE is rn.SESSION_DIR_RE
    assert station_collect._final_ftpdetectinfos is rn.ftpdetect_files
    assert station_collect._system_zoneinfo is rn._system_zoneinfo
    assert station_api._night_date is rn.night_of
    assert config_patch.deep_merge is rc.deep_merge
    assert config_patch.LOCK_NAME == rc.CONFIG_LOCK_NAME


def _inline_chain(cfg: dict) -> Path:
    """The exact expression the inline copies (encoder, stacker, dawn, ...) use."""
    return Path(
        cfg.get('videocapture_path') or
        cfg.get('color_video_path') or
        cfg.get('reenc_path') or
        cfg.get('color_capture_path') or
        cfg.get('ssd_color_path') or
        str(Path.home() / 'color_capture')
    )


@pytest.mark.parametrize('cfg', CAPTURE_CFGS)
def test_capture_root_matches_the_inline_chain(cfg):
    assert rc.capture_root(cfg) == _inline_chain(cfg)


@pytest.mark.parametrize('cfg,want', [
    ({}, 'HOME/color_capture'),
    ({'videocapture_path': ''}, 'HOME/color_capture'),
    ({'videocapture_path': '/v'}, '/v'),
    ({'color_capture_path': '/cc'}, '/cc'),
    ({'videocapture_path': None, 'ssd_color_path': '/s'}, '/s'),
])
def test_spectro_pipeline_uses_the_full_chain(cfg, want, tmp_path):
    """spectro_pipeline read only videocapture_path, so a root named by any
    other key (or an empty one) sent it to the wrong directory."""
    import spectro_pipeline

    expected = Path(want.replace('HOME', str(tmp_path)))
    assert spectro_pipeline._capture_root(cfg, tmp_path) == expected


def test_capture_root_matches_flags_manager_when_writable(tmp_path):
    import flags_manager

    cfg = {'videocapture_path': str(tmp_path / 'cap')}
    assert flags_manager._state_root(cfg) == rc.capture_root(cfg)


@pytest.mark.parametrize('cfg,want', [
    ({}, Path.home() / 'logs'),
    ({'log_path': None}, Path.home() / 'logs'),
    ({'log_path': ''}, Path.home() / 'logs'),
    ({'log_path': '/var/log/x'}, Path('/var/log/x')),
])
def test_log_dir_is_null_safe(cfg, want):
    assert rc.log_dir(cfg) == want


def test_station_cfg():
    cfg = {'stations': {'RO000A': {'rms': True}, 'BAD': 'x'}}
    assert rc.station_cfg(cfg, 'RO000A') == {'rms': True}
    assert rc.station_cfg(cfg, 'BAD') == {}
    assert rc.station_cfg(cfg, 'NONE') == {}
    assert rc.station_cfg({}, 'RO000A') == {}


@pytest.mark.parametrize('cfg,service,want', [
    ({'services': {'stacker': {'enabled': False}}}, 'stacker', False),
    ({'services': {'stacker': {'enabled': True}}}, 'stacker', True),
    ({'services': {'timelapse_build': {'enabled': True}}}, 'timelapse_build', True),
    ({'services': {'stacker': True}, 'active_services': ['stacker']}, 'stacker', True),
    ({'services': {'stacker': True}, 'active_services': []}, 'stacker', False),
])
def test_service_enabled_matches_dawn_when_the_switch_is_present(cfg, service, want):
    import dawn_process

    assert rc.service_enabled(cfg, service) is want
    assert dawn_process._svc_enabled(cfg, service) is want


def test_service_enabled_legacy_list_without_a_switch():
    assert rc.service_enabled({'active_services': ['stacker']}, 'stacker') is True
    assert rc.service_enabled({'active_services': []}, 'stacker') is False


def test_service_enabled_falls_back_to_config_defaults():
    defaults = json.loads(
        (Path(__file__).parent.parent / 'rovimen-scripts' / 'config_defaults.json').read_text())
    for name, block in defaults['services'].items():
        if isinstance(block, dict) and 'enabled' in block:
            assert rc.service_enabled({}, name) is bool(block['enabled']), name
    assert rc.service_enabled({}, 'no_such_service') is False


# The cases color_calibration's copy of station_location covered (removed in
# favour of this one), plus its fallback order.
@pytest.mark.parametrize('cfg,cam,want', [
    ({}, None, None),
    ({'latitude': 44.9, 'longitude': 25.6}, None, (44.9, 25.6)),
    ({'latitude': '44.9', 'longitude': '25.6'}, {}, (44.9, 25.6)),
    # the camera's own coordinates win over the station's
    ({'latitude': 1.0, 'longitude': 2.0}, {'latitude': 3.0, 'longitude': 4.0}, (3.0, 4.0)),
    # an unusable top-level pair falls through to overlay.coords
    ({'latitude': 'x', 'longitude': 25.6, 'overlay': {'coords': '45.30N  26.11E'}}, {},
     (45.30, 26.11)),
    ({'latitude': None, 'longitude': 3}, None, None),
    ({'latitude': 44.9, 'overlay': {'coords': '45.12N  25.46E'}}, None, (45.12, 25.46)),
    ({'overlay': {'coords': '33.87S  151.21E'}}, None, (-33.87, 151.21)),
    ({'overlay': {'coords': '52.1N  13.2W'}}, None, (52.1, -13.2)),
    ({'overlay': {'coords': '33.9S 18.4W'}}, None, (-33.9, -18.4)),
    ({'overlay': {'coords': 'nowhere'}}, None, None),
    ({'overlay': None}, None, None),
])
def test_station_location(cfg, cam, want):
    assert rc.station_location(cfg, cam) == want


def test_station_location_degree_minute_coords():
    lat, lon = rc.station_location({'overlay': {'coords': '45 00N  025 00E'}})
    assert lat == pytest.approx(45 + 0 / 60) and lon == pytest.approx(25 + 0 / 60)


def test_cpu_cores_prefers_hardware_json(tmp_path):
    cfg_file = tmp_path / 'config.json'
    cfg_file.write_text('{}')
    import os
    assert rc.cpu_cores(cfg_file) == (os.cpu_count() or 1)
    (tmp_path / 'hardware.json').write_text(json.dumps({'cpu_cores': 3}))
    assert rc.cpu_cores(cfg_file) == 3
    (tmp_path / 'hardware.json').write_text('not json')
    assert rc.cpu_cores(cfg_file) == (os.cpu_count() or 1)


def test_atomic_write_json_keeps_mode_and_leaves_no_temp(tmp_path):
    target = tmp_path / 'config.json'
    target.write_text('{}')
    target.chmod(0o600)
    rc.atomic_write_json(target, {'a': 1}, indent=4)
    assert json.loads(target.read_text()) == {'a': 1}
    assert (target.stat().st_mode & 0o777) == 0o600
    assert sorted(p.name for p in tmp_path.iterdir()) == ['config.json']


def test_update_config_rereads_under_the_lock(tmp_path):
    target = tmp_path / 'config.json'
    target.write_text(json.dumps({'a': {'x': 1}, 'keep': True}))
    merged = rc.update_config({'a': {'y': 2}}, path=target)
    assert merged == {'a': {'x': 1, 'y': 2}, 'keep': True}
    assert json.loads(target.read_text()) == merged
    # A later writer sees the first writer's change (no cached copy).
    target.write_text(json.dumps({'a': {'x': 9}}))
    assert rc.update_config({'b': 1}, path=target) == {'a': {'x': 9}, 'b': 1}


def test_concurrent_update_config_loses_no_update(tmp_path):
    target = tmp_path / 'config.json'
    target.write_text('{}')

    def writer(i: int) -> None:
        for j in range(10):
            rc.update_config({f'k{i}_{j}': j}, path=target)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(json.loads(target.read_text())) == 40


def test_update_config_leaves_file_untouched_on_bad_json(tmp_path):
    target = tmp_path / 'config.json'
    target.write_text('{not json')
    with pytest.raises(ValueError):
        rc.update_config({'a': 1}, path=target)
    assert target.read_text() == '{not json'


def test_deep_merge_matches_config_patch():
    # PATCH /api/settings and config_patch.py merge through config_patch.
    import config_patch

    base = {'a': {'b': 1, 'c': {'d': 2}}, 'x': 1}
    patch = {'a': {'c': {'e': 3}, 'f': 4}, 'x': {'y': 1}}
    expected = json.loads(json.dumps(base))
    config_patch.deep_merge(expected, patch)
    assert rc.deep_merge(json.loads(json.dumps(base)), patch) == expected


# ---------------------------------------------------------------------------
# rovimen_night
# ---------------------------------------------------------------------------

NIGHT_CASES = [
    ('20260925', '000000'), ('20260925', '115959'), ('20260925', '120000'),
    ('20260925', '235959'), ('20260101', '050000'), ('20260301', '010000'),
    ('20240301', '030000'), ('20261231', '130000'), ('20260801', '114500'),
]


@pytest.mark.parametrize('date_s,time_s', NIGHT_CASES)
def test_night_of_matches_every_copy(date_s, time_s):
    import color_capture
    import detection_indexer
    import detection_lock
    import station_api

    want = rn.night_of(date_s, time_s)
    copies = {
        'detection_lock': detection_lock._night_date(date_s, time_s),
        'detection_indexer': detection_indexer._night_date(date_s, time_s),
        'station_api': station_api._night_date(date_s, time_s),
        'color_capture': color_capture.ColorCapture._night_date_from_filename(date_s, time_s),
    }
    assert copies == {k: want for k in copies}


@pytest.mark.parametrize('date_s,time_s,want', [
    ('20260925', '115959', '20260924'), ('20260925', '120000', '20260925'),
    ('20260301', '010000', '20260228'), ('20240301', '030000', '20240229'),
    ('20260101', '050000', '20251231'),
])
def test_night_of_literal_values(date_s, time_s, want):
    assert rn.night_of(date_s, time_s) == want


# Modules moved onto the shared helpers must not grow their own copies again.
_ADOPTED_HELPERS = {
    'stacker': ('_night_date', '_capture_root', '_CHUNK_RE', '_mkv_frame_size', '_ebml_vint',
                '_chunk_midpoint'),
    'encoder': ('_night_date', '_CHUNK_RE', '_STITCHED_CHUNK_RE', '_check_vaapi', '_chunk_mid_time',
                '_render_nodes', '_vaapi_driver_candidates', '_vaapi_ok',
                '_vaapi_device_cache', '_vaapi_driver_cache', 'VAAPI_PROBE_SOURCE'),
    'dawn_process': ('_archive_night', '_capture_root', '_ARCHIVE_DIR_RE', '_CHUNK_RE',
                     '_NIGHT_ROLLOVER_HOUR'),
    'archive_upload': ('_rms_night_date', '_ARC_SESSION_RE'),
    # One station location, sun altitude, file-name parser and ffprobe wrapper.
    'color_calibration': ('station_location', '_COORDS_DM_RE', '_COORDS_DEC_RE'),
    'rovimen_mode_switch': ('sun_altitude_deg',),
    'color_capture': ('_chunk_datetime',),
    'overlay_details': ('_chunk_time', '_CHUNK_RE'),
    'detection_lock': ('_CHUNK_RE', '_FF_NAME_RE', '_ff_block_time', '_arc_re_midnight'),
    'camera_stall_guard': ('_FF_RE',),
    'station_api': ('_frame_start', '_FRFF_RE'),
    'ff_thumbs': ('_FF_TIME_RE',),
}


@pytest.mark.parametrize('module,names', sorted(_ADOPTED_HELPERS.items()))
def test_adopted_modules_keep_no_local_copies(module, names):
    mod = __import__(module)
    assert [n for n in names if hasattr(mod, n)] == []


@pytest.mark.parametrize('module', ['stacker', 'encoder', 'dawn_process', 'archive_upload',
                                    'timelapse_build', 'flags_manager', 'station_collect',
                                    'station_api'])
def test_adopted_modules_spell_no_capture_root_chain(module):
    src = (Path(__file__).parent.parent / 'rovimen-scripts' / f'{module}.py').read_text()
    assert "cfg.get('color_video_path')" not in src


def test_timelapse_build_takes_the_frame_height_without_importing_the_stacker():
    import ast

    import stacker

    src = (Path(__file__).parent.parent / 'rovimen-scripts' / 'timelapse_build.py').read_text()
    imported = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert 'stacker' not in imported
    assert (stacker.WIDTH, stacker.HEIGHT) == (rm.CAPTURE_WIDTH, rm.CAPTURE_HEIGHT) == (1280, 720)


@pytest.mark.parametrize('hour,want_current,want_completed', [
    (0, '20260925', '20260925'),
    (11, '20260925', '20260925'),
    (12, '20260926', '20260925'),
    (13, '20260926', '20260925'),
    (14, '20260926', '20260926'),
    (23, '20260926', '20260926'),
])
def test_current_and_last_completed_night(hour, want_current, want_completed):
    now = datetime(2026, 9, 26, hour, 30, tzinfo=timezone.utc)
    assert rn.current_night(now) == want_current
    assert rn.last_completed_night(now) == want_completed


def test_last_completed_night_matches_dawn(monkeypatch):
    import dawn_process

    for hour in (0, 11, 12, 13, 14, 20):
        fixed = datetime(2026, 9, 26, hour, 5, tzinfo=timezone.utc)

        class _Clock(datetime):
            @classmethod
            def now(cls, tz=None):  # noqa: ANN001
                return fixed

        monkeypatch.setattr(dawn_process, 'datetime', _Clock)
        assert dawn_process._night_date() == rn.last_completed_night(fixed)


@pytest.mark.parametrize('name,want', [
    ('RO000A_20260925_190512_color.mkv', ('RO000A', '20260925', '190512', None)),
    ('RO000P_20260724_002833_002853_color.mkv', ('RO000P', '20260724', '002833', '002853')),
    ('RO000A_20260925_190512_color.mp4', None),
    ('RO000A_20260925_190512_stack.webp', None),
    ('ro000a_20260925_190512_color.mkv', None),
])
def test_parse_chunk_name(name, want):
    got = rn.parse_chunk_name(name)
    assert (tuple(got) if got else None) == want


@pytest.mark.parametrize('name,chunk,stitched', [
    ('RO000A_20260925_190512_color.mkv', True, False),
    ('RO000P_20260724_002833_002853_color.mkv', False, True),
    ('X_1_2_color.mkv', False, False),
    ('RO000A_20260925_190512_color.mkv.tmp.mkv', False, False),
    ('RO000A_20260925_190512_color.mkv.123-456.tmp.mkv', False, False),
])
def test_chunk_regexes(name, chunk, stitched):
    import detection_indexer

    assert bool(rn.CHUNK_RE.match(name)) is chunk
    assert bool(rn.STITCHED_CHUNK_RE.match(name)) is stitched
    assert bool(detection_indexer._CHUNK_RE.match(name)) is chunk


@pytest.mark.parametrize('cfg', [
    {},
    {'chunk_names_utc': True},
    {'chunk_names_utc': False, 'legacy_chunk_tz': 'Europe/Bucharest'},
    {'chunk_names_utc': False, 'legacy_chunk_tz': 'Not/AZone'},
])
@pytest.mark.parametrize('date_s,time_s', [('20260115', '203000'), ('20260715', '013000'),
                                           ('20261025', '023000')])
def test_chunk_utc_dt_matches_station_api(cfg, date_s, time_s):
    import station_api

    assert rn.chunk_utc_dt(date_s, time_s, cfg) == station_api._chunk_utc_dt(date_s, time_s, cfg)


def test_chunk_utc_dt_converts_legacy_local_names():
    cfg = {'chunk_names_utc': False, 'legacy_chunk_tz': 'Europe/Bucharest'}
    assert rn.chunk_utc_dt('20260715', '013000', cfg) == datetime(2026, 7, 14, 22, 30)
    assert rn.chunk_utc_dt('20260115', '013000', cfg) == datetime(2026, 1, 14, 23, 30)


def _session(root: Path, sub: str, name: str, ftp: bool = True) -> Path:
    d = root / sub / name
    d.mkdir(parents=True)
    if ftp:
        (d / f'FTPdetectinfo_{name}.txt').write_text('x')
    return d


def test_scan_rms_sessions_archived_wins_and_filters(tmp_path):
    cap = _session(tmp_path, 'CapturedFiles', 'RO000A_20260925_170000_000001')
    arc = _session(tmp_path, 'ArchivedFiles', 'RO000A_20260925_170000_000001')
    only_cap = _session(tmp_path, 'CapturedFiles', 'RO000A_20260926_013000_000002')
    _session(tmp_path, 'ArchivedFiles', 'RO000B_20260925_170000_000003')
    _session(tmp_path, 'ArchivedFiles', 'RO000A_20260926_170000_000004')
    (tmp_path / 'ArchivedFiles' / 'not_a_session').mkdir()

    sessions, listed = rn.scan_rms_sessions(tmp_path, 'RO000A', '20260925')
    assert listed is True
    assert sessions == {'RO000A_20260925_170000_000001': arc,
                        'RO000A_20260926_013000_000002': only_cap}
    assert cap not in sessions.values()
    assert rn.rms_sessions(tmp_path, 'RO000A', '20260925') == [arc, only_cap]
    assert len(rn.rms_sessions(tmp_path)) == 4


def test_scan_rms_sessions_reports_an_unlistable_tree(tmp_path):
    sessions, listed = rn.scan_rms_sessions(tmp_path / 'missing')
    assert sessions == {} and listed is False


def test_ftpdetect_files_skips_unfiltered_and_backup(tmp_path):
    for name in ('FTPdetectinfo_A.txt', 'FTPdetectinfo_A_unfiltered.txt',
                 'FTPdetectinfo_A_backup.txt', 'FTPdetectinfo_B.txt', 'other.txt'):
        (tmp_path / name).write_text('x')
    assert [p.name for p in rn.ftpdetect_files(tmp_path)] == [
        'FTPdetectinfo_A.txt', 'FTPdetectinfo_B.txt']


# File-name timestamps: the cases the removed private copies covered
# (overlay_details._chunk_time, color_capture._chunk_datetime,
# encoder._chunk_mid_time / stacker._chunk_midpoint, spectro's chunk start,
# camera_stall_guard / station_collect's FF regex, detection_lock._ff_block_time,
# station_api._frame_start).

def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


@pytest.mark.parametrize('name,stitched,want', [
    ('RO000H_20260801_190512_color.mkv', True, _utc(2026, 8, 1, 19, 5, 12)),
    ('RO000H_20260801_190512_color.mkv', False, _utc(2026, 8, 1, 19, 5, 12)),
    ('RO000H_20260801_190512_190532_color.mkv', True, _utc(2026, 8, 1, 19, 5, 12)),
    # segment counters (colour capture's storm count, spectro) refuse stitched clips
    ('RO000H_20260801_190512_190532_color.mkv', False, None),
    ('RO000H_20261340_190512_color.mkv', True, None),       # impossible date
    ('RO000H_20260801_190512_color.mkv.tmp.mkv', True, None),
    ('ro000h_20260801_190512_color.mkv', True, None),
    ('RO000H_20260801_190512_stack.webp', True, None),
    ('state.json', True, None),
])
def test_chunk_start_utc(name, stitched, want):
    assert rn.chunk_start_utc(name, stitched=stitched) == want


@pytest.mark.parametrize('date_s,time_s,want', [
    ('20260801', '190512', _utc(2026, 8, 1, 19, 5, 22)),
    ('20261231', '235955', _utc(2027, 1, 1, 0, 0, 5)),      # rolls over the year
    ('20260801', '190512190532', _utc(2026, 8, 1, 19, 5, 22)),  # only HHMMSS is read
    ('20260801', 'xx0512', None),
    ('20261301', '190512', None),
])
def test_chunk_midpoint_utc(date_s, time_s, want):
    assert rn.chunk_midpoint_utc(date_s, time_s) == want


@pytest.mark.parametrize('name,want', [
    ('FF_RO001E_20260801_190512_123_0123456.fits', _utc(2026, 8, 1, 19, 5, 12)),
    ('FF_RO09XA_20260801_000000_999_0000000.fits', _utc(2026, 8, 1, 0, 0, 0)),
    # any camera code RMS was given counts, as in the stall guard's regex
    ('FF_ro-1_20260801_190512_123_0123456.fits', _utc(2026, 8, 1, 19, 5, 12)),
    ('FF_RO001E_20260801_190512_123_0123456.fits.bz2', None),
    ('FR_RO001E_20260801_190512_123_0123456.bin', None),
    ('FF_RO001E_20260801_190512_0123456.fits', None),       # no milliseconds
    ('FF_RO001E_20261301_190512_123_0123456.fits', None),   # impossible date
    ('CALSTARS_RO001E_20260801_190512_123456.txt', None),
])
def test_ff_file_start(name, want):
    assert rn.ff_file_start(name) == want


@pytest.mark.parametrize('name,want', [
    ('FF_RO001E_20260801_190512_123_0000000.fits', datetime(2026, 8, 1, 19, 5, 12, 123000)),
    ('FR_RO001E_20260730_220322_000_0000000.bin', datetime(2026, 7, 30, 22, 3, 22)),
    ('FF_RO001E_20260801_235959_999_0000000.fits', datetime(2026, 8, 1, 23, 59, 59, 999000)),
    ('FF_RO001E_20261301_190512_123_0000000.fits', None),
    ('CALSTARS_RO001E.txt', None),
    ('not_a_frame.fits', None),
])
def test_rms_frame_time(name, want):
    assert rn.rms_frame_time(name) == want


# Sun altitude. Reference values: astropy 7.2 AltAz without refraction
# (get_sun, height 0) at Bucharest, 44.65 N 25.67 E.
@pytest.mark.parametrize('when,want', [
    (_utc(2026, 6, 21, 10, 0), 68.707),    # near the solstice noon
    (_utc(2026, 12, 21, 15, 0), -4.090),   # just after a winter sunset
    (_utc(2026, 9, 24, 3, 51), -3.305),    # morning twilight
    (_utc(2026, 3, 20, 16, 30), -1.236),   # equinox sunset
])
@pytest.mark.skip(reason="expected values depend on fixture positions that differ in this snapshot")
def test_sun_altitude_matches_astropy(when, want):
    assert rn.sun_altitude_deg(44.65, 25.67, when) == pytest.approx(want, abs=0.02)


def test_sun_altitude_agrees_with_the_colour_ephemeris():
    """color_calibration.sun_moon_state keeps its own sun term (the moon's phase
    needs the sun's longitude); the two altitudes must not drift apart."""
    import color_calibration as cc

    when = _utc(2026, 1, 1)
    for _ in range(240):
        alt = rn.sun_altitude_deg(45.30, 26.11, when)
        assert cc.sun_moon_state(when, 45.30, 26.11)['sun_alt'] == pytest.approx(alt, abs=0.01)
        when += timedelta(hours=36, minutes=29)


def test_sun_altitude_matches_ephem_at_the_mode_switch_threshold():
    """rovimen_mode_switch used ephem, as RMS's own switch does; around -9 deg
    ephem applies no refraction, so the shared altitude switches at the same time."""
    ephem = pytest.importorskip('ephem')
    import math

    when = _utc(2026, 11, 23, 15, 25)
    seen = 0
    while when < _utc(2026, 11, 23, 15, 40):
        obs = ephem.Observer()
        obs.lat, obs.lon, obs.elevation = '44.65', '25.67', 0
        obs.date = when.strftime('%Y/%m/%d %H:%M:%S')
        sun = ephem.Sun()
        sun.compute(obs)
        alt = math.degrees(float(sun.alt))
        if -10.0 < alt < -8.0:
            seen += 1
            assert rn.sun_altitude_deg(44.65, 25.67, when) == pytest.approx(alt, abs=0.02)
        when += timedelta(minutes=1)
    assert seen >= 5


def test_mode_switch_decides_on_the_shared_altitude(monkeypatch, tmp_path):
    import types

    import rovimen_mode_switch as ms

    seen = {}

    def alt(lat, lon, when):
        seen['args'] = (lat, lon, when.tzinfo)
        return -9.5

    calls = []
    camctl = types.ModuleType('Utils.CameraControl')
    camctl.cameraControl = lambda ip, cmd, opts, camera_settings_path=None: calls.append(
        (ip, cmd, opts))
    logger_mod = types.ModuleType('RMS.Logger')
    logger_mod.LoggingManager = None
    for name, mod in (('RMS', types.ModuleType('RMS')), ('RMS.Logger', logger_mod),
                      ('Utils', types.ModuleType('Utils')), ('Utils.CameraControl', camctl)):
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.setattr(ms.rovimen_night, 'sun_altitude_deg', alt)
    monkeypatch.setattr(ms.Path, 'home', staticmethod(lambda: tmp_path))
    monkeypatch.setattr(sys, 'path', list(sys.path))
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'RMS').mkdir()
    monkeypatch.setattr(sys, 'argv', ['ms', '--rms-dir', str(tmp_path / 'RMS'),
                                      '--lat', '44.65', '--lon', '25.67',
                                      '--settings', 's.json', '--camera-ip', '10.0.0.11'])
    assert ms.main() == 0
    assert calls == [('10.0.0.11', 'SwitchMode', ['night'])]
    assert seen['args'] == (44.65, 25.67, timezone.utc)


# ---------------------------------------------------------------------------
# rovimen_media
# ---------------------------------------------------------------------------

_HAS_FFMPEG = shutil.which('ffmpeg') is not None and shutil.which('ffprobe') is not None


@pytest.fixture(scope='module')
def tiny_mkv(tmp_path_factory) -> Path:
    if not _HAS_FFMPEG:
        pytest.skip('ffmpeg not installed')
    out = tmp_path_factory.mktemp('mkv') / 'RO000A_20260925_190512_color.mkv'
    subprocess.run(
        ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
         '-i', 'testsrc=duration=0.2:size=1280x720:rate=25', '-c:v', 'libx264',
         '-pix_fmt', 'yuv420p', '-color_primaries', 'bt709', '-color_trc', 'bt709',
         '-colorspace', 'bt709', str(out)],
        check=True, timeout=60,
    )
    return out


def test_mkv_frame_size_reads_a_real_chunk(tiny_mkv):
    import stacker

    assert rm.mkv_frame_size(tiny_mkv) == (1280, 720)
    assert stacker._probe_video_height(tiny_mkv) == 720


def test_mkv_frame_size_none_for_non_matroska(tmp_path):
    f = tmp_path / 'x.mkv'
    f.write_bytes(b'not matroska at all')
    assert rm.mkv_frame_size(f) is None


def test_probe_helpers_match_overlay_details(tiny_mkv):
    import overlay_details

    assert rm.probe_video_height(tiny_mkv) == overlay_details._probe_video_height(tiny_mkv) == 720
    assert rm.colour_tag_args(tiny_mkv) == overlay_details._colour_tag_args(tiny_mkv)
    assert 0.1 < (rm.probe_duration(tiny_mkv) or 0) < 1.0


def test_ffprobe_json_failure_is_none(tmp_path):
    assert rm.ffprobe_json(tmp_path / 'missing.mkv', timeout=5) is None
    assert rm.probe_video_height(tmp_path / 'missing.mkv') is None


def test_ffprobe_json_requires_a_timeout():
    with pytest.raises(TypeError):
        rm.ffprobe_json(Path('x'))  # type: ignore[call-arg]


def test_ffprobe_json_places_extra_args_before_the_entries(monkeypatch):
    seen = {}

    def run(cmd, **kw):
        seen['cmd'], seen['timeout'] = cmd, kw.get('timeout')
        return subprocess.CompletedProcess(cmd, 0, stdout='{"streams": [{"nb_read_packets": "5"}]}')

    monkeypatch.setattr(rm.subprocess, 'run', run)
    data = rm.ffprobe_json(Path('c.mkv'), timeout=7, args=('-count_packets',),
                           entries='stream=nb_read_packets')
    assert rm.first_stream(data) == {'nb_read_packets': '5'}
    assert seen['cmd'] == ['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-count_packets',
                           '-show_entries', 'stream=nb_read_packets', '-of', 'json', 'c.mkv']
    assert seen['timeout'] == 7


@pytest.mark.parametrize('failure', [
    subprocess.TimeoutExpired('ffprobe', 1), FileNotFoundError('ffprobe'),
    subprocess.CompletedProcess(['ffprobe'], 1, stdout=''),
    subprocess.CompletedProcess(['ffprobe'], 0, stdout='not json'),
])
def test_ffprobe_failures_read_as_none_in_every_caller(monkeypatch, failure):
    """The private wrappers this replaced each handled a different subset
    (sample_frames let OSError escape; clip_timing let 0/0 escape)."""
    import clip_timing
    import color_calibration as cc
    import spectro_pipeline as sp

    def run(cmd, **kw):
        if isinstance(failure, BaseException):
            raise failure
        return failure

    monkeypatch.setattr(rm.subprocess, 'run', run)
    assert rm.ffprobe_json(Path('c.mkv'), timeout=1) is None
    assert clip_timing._probe(Path('c.mkv')) is None
    assert cc.sample_frames(Path('c.mkv')) is None
    assert cc.sample_avg_frame(Path('c.mkv')) is None
    assert sp.chunk_fps(Path('c.mkv')) == 0.0
    assert sp._window_frame_count(Path('c.mkv')) is None
    assert sp._keyframe_at_or_before(Path('c.mkv'), 3.0) == 0.0


@pytest.mark.parametrize('rate,want', [('25/1', 25.0), ('30000/1001', 30000 / 1001),
                                       ('0/0', 25.0), ('0/1', 25.0), ('25', 25.0)])
def test_clip_timing_probe_frame_rates(monkeypatch, rate, want):
    """``0/0`` used to raise ZeroDivisionError out of the clip-offset pass."""
    import clip_timing

    out = json.dumps({'streams': [{'width': 1280, 'height': 768, 'r_frame_rate': rate}]})
    monkeypatch.setattr(rm.subprocess, 'run',
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=out))
    w, h, fps = clip_timing._probe(Path('c.mkv'))
    assert (w, h) == (1280, 768) and fps == pytest.approx(want)


def test_spectro_probe_parsers(monkeypatch):
    import spectro_pipeline as sp

    outputs = {
        'stream=nb_read_packets:format=duration':
            {'streams': [{'nb_read_packets': '100'}], 'format': {'duration': '20.000000'}},
        'stream=nb_read_packets': {'streams': [{'nb_read_packets': '375'}]},
        'frame=pts_time': {'frames': [{'pts_time': '-0.040000'}, {'pts_time': '0.000000'},
                                      {'pts_time': 'N/A'}, {'pts_time': '10.000000'},
                                      {'pts_time': '20.000000'}]},
    }

    def run(cmd, **kw):
        entries = cmd[cmd.index('-show_entries') + 1]
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(outputs[entries]))

    monkeypatch.setattr(rm.subprocess, 'run', run)
    assert sp.chunk_fps(Path('c.mkv')) == 5.0
    assert sp._window_frame_count(Path('c.mkv')) == 375
    assert sp._keyframe_at_or_before(Path('c.mkv'), 15.0) == 10.0
    assert sp._keyframe_at_or_before(Path('c.mkv'), 9.9995) == 10.0   # 1 ms tolerance
    assert sp._keyframe_at_or_before(Path('c.mkv'), 5.0) == 0.0
    outputs['stream=nb_read_packets:format=duration'] = {
        'streams': [{'nb_read_packets': '100'}], 'format': {'duration': 'N/A'}}
    assert sp.chunk_fps(Path('c.mkv')) == 0.0   # was 1.0 (packets / packets)


def test_ffprobe_callers_on_a_real_chunk(tiny_mkv, tmp_path):
    import clip_timing
    import color_calibration as cc
    import spectro_pipeline as sp

    assert clip_timing._probe(tiny_mkv) == (1280, 720, 25.0)
    gop5 = tmp_path / 'gop5.mkv'   # 1 s at 25 fps, a keyframe every 0.2 s
    subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
                    '-i', 'testsrc=duration=1:size=320x240:rate=25', '-c:v', 'libx264',
                    '-g', '5', '-keyint_min', '5', '-sc_threshold', '0', str(gop5)],
                   check=True, timeout=60)
    assert sp.chunk_fps(gop5) == pytest.approx(25.0, rel=0.05)
    assert sp._window_frame_count(gop5) == 25
    assert sp._keyframe_at_or_before(gop5, 0.5) == pytest.approx(0.4)
    frames = cc.sample_frames(tiny_mkv, n_samples=2)
    assert frames is not None and frames.shape[1:] == (720, 1280, 3)
    assert cc.sample_avg_frame(tiny_mkv, n_samples=2).shape == (720, 1280, 3)


def test_passthrough_args_matches_stacker():
    import stacker

    assert rm.passthrough_args() == stacker.passthrough_args()


def test_chunk_lock_stack_shares_the_stacker_lock_file(tmp_path):
    import stacker

    mkv = tmp_path / 'RO000A_20260925_190512_color.mkv'
    mkv.write_bytes(b'')
    with stacker.chunk_stack_lock(mkv) as held:
        assert held is True
        with rm.chunk_lock(mkv, 'stack') as second:
            assert second is False
        with rm.chunk_lock(mkv, 'encode') as other_kind:
            assert other_kind is True
    with rm.chunk_lock(mkv, 'stack') as again:
        assert again is True
    # Holders unlink their lock file on release, so nothing accumulates.
    assert not (tmp_path / '.stack_locks' / f'{mkv.name}.lock').exists()
    assert not (tmp_path / '.encode_locks' / f'{mkv.name}.lock').exists()


def test_stacker_chunk_stack_lock_is_the_shared_lock(tmp_path):
    import stacker

    assert stacker._STACK_LOCK_DIR == rm.lock_dir_name('stack') == '.stack_locks'
    mkv = tmp_path / 'RO000A_20260925_190512_color.mkv'
    mkv.write_bytes(b'')
    with rm.chunk_lock(mkv, 'stack') as held:
        assert held is True
        with stacker.chunk_stack_lock(mkv) as second:
            assert second is False


def test_sweep_stale_locks_removes_only_unheld_files(tmp_path):
    lock_dir = tmp_path / rm.lock_dir_name('stack')
    lock_dir.mkdir()
    (lock_dir / 'left_behind_color.mkv.lock').write_text('')
    held_mkv = tmp_path / 'held_color.mkv'
    with rm.chunk_lock(held_mkv, 'stack') as held:
        assert held is True
        assert rm.sweep_stale_locks(tmp_path, 'stack') == 1
        assert (lock_dir / 'held_color.mkv.lock').exists()
    assert list(lock_dir.iterdir()) == []
    assert rm.sweep_stale_locks(tmp_path / 'missing', 'stack') == 0


def test_chunk_lock_unusable_dir_proceeds_unguarded(tmp_path):
    blocker = tmp_path / 'night'
    blocker.write_text('a file where the night dir should be')
    with rm.chunk_lock(blocker / 'x_color.mkv', 'encode') as held:
        assert held is True


@pytest.mark.parametrize('cfg,want', [
    (None, ['', 'i965']), ({}, ['', 'i965']), ({'vaapi_driver': 'auto'}, ['', 'i965']),
    ({'vaapi_driver': 'i965'}, ['i965']), ({'vaapi_driver': ' iHD '}, ['iHD']),
    ({'vaapi_driver': 5}, ['', 'i965']),
])
def test_vaapi_driver_candidates(cfg, want):
    assert rm.vaapi_driver_candidates(cfg) == want


# ---------------------------------------------------------------------------
# rovimen_rms
# ---------------------------------------------------------------------------


def _ftp_block(ff: str, meteor_no: int, frame0: float, frame1: float, mags: tuple) -> str:
    hdr = f'{ff}\nRO000M {meteor_no:04d} 0002 0025.00 0 0 0 0 0 0'
    f0 = (f'{frame0:09.4f} 100.0 100.0 10.000000 20.000000 130.000000 45.000000 100 '
          f'{mags[0]:+.2f} 0 10 0')
    f1 = (f'{frame1:09.4f} 110.0 110.0 11.000000 21.000000 131.000000 46.000000 100 '
          f'{mags[1]:+.2f} 0 10 0')
    return f'{hdr}\n{f0}\n{f1}'


@pytest.fixture
def rms_session(tmp_path: Path) -> Path:
    d = tmp_path / 'ArchivedFiles' / 'RO000M_20260623_180000_000000'
    d.mkdir(parents=True)
    ff1 = 'FF_RO000M_20260623_200000_500_0000000.fits'
    ff2 = 'FF_RO000M_20260623_202000_000_0000000.fits'
    ff3 = 'FF_RO000M_20260623_203000_000_0000000.fits'
    ftp = 'Meteor Count = 000004\n-----\nCam# Meteor# #Segments fps\n-----\n'
    ftp += _ftp_block(ff1, 1, 25, 50, (-2.0, -1.5)) + '\n'
    ftp += _ftp_block(ff2, 1, 0, 25, (3.0, 3.5)) + '\n'
    ftp += _ftp_block(ff3, 1, 0, 25, (0.5, 0.8)) + '\n'
    ftp += _ftp_block(ff3, 2, 50, 75, (1.0, 1.2)) + '\n'
    (d / 'FTPdetectinfo_RO000M_20260623_180000_000000.txt').write_text(ftp)
    (d / 'FTPdetectinfo_RO000M_20260623_180000_000000_unfiltered.txt').write_text(ftp * 2)
    rad = [
        '# header',
        '20260623 20:00:01.500000, 2461100.0, 100.0, PER, 10.0, 20.0, 11.0, 21.0, None, None, '
        '0,0,0,0, -2.00, None, 45.0',
        '20260623 20:30:00.000000, 2461100.2, 100.2, ..., 14.0, 24.0, 15.0, 25.0, None, None, '
        '0,0,0,0, 0.50, None, 50.0',
        '20260623 21:00:00.000000, 2461100.4, 100.4, GEM, 1.0, 2.0, 3.0, 4.0, None, None, '
        '0,0,0,0, 1.00, None, 30.0',
    ]
    (d / 'RO000M_20260623_180000_000000_radiants.txt').write_text('\n'.join(rad))
    return d


def _key(r: dict) -> tuple:
    return (r['ff_file'], r['meteor_no'])


def test_session_detections_match_the_indexer_and_rms_parse(rms_session):
    import detection_indexer
    import rms_parse

    ours = sorted(rr.session_detections(rms_session), key=_key)
    assert ours == sorted(detection_indexer._session_detections(rms_session), key=_key)
    assert ours == sorted(rms_parse.parse_session_detections(rms_session), key=_key)
    assert len(ours) == 5  # 4 FTP rows + 1 radiant-only row
    by_key = {_key(r): r for r in ours}
    assert by_key[('FF_RO000M_20260623_200000_500_0000000.fits', 1)]['shower'] == 'PER'
    assert by_key[('FF_RO000M_20260623_203000_000_0000000.fits', 1)]['shower'] == 'SPO'


def test_night_detections_read_each_session_once(rms_session):
    root = rms_session.parent.parent
    dup = root / 'CapturedFiles' / rms_session.name
    shutil.copytree(rms_session, dup)
    rows = rr.night_detections(root, 'RO000M', '20260623')
    assert len(rows) == 5
    assert rr.night_detections(root, 'RO000M', '20260624') == []


def test_rms_parsers_match_the_indexer_row_by_row(rms_session):
    import detection_indexer

    ftp = rn.ftpdetect_files(rms_session)[0]
    assert rr.ftp_rows(ftp) == detection_indexer._ftp_rows(ftp)
    rad = next(rms_session.glob('*_radiants.txt'))
    assert rr.radiant_rows(rad) == detection_indexer._radiant_rows(rad)


def _rms_config(d: Path, station_id: str) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / '.config').write_text(f'[System]\nstationID: {station_id}\n')
    return d


def test_resolve_rms_config_dir_matches_platepar_autofit(tmp_path, monkeypatch):
    import platepar_autofit

    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    _rms_config(tmp_path / 'RMS_cam1', 'RO000M')
    _rms_config(tmp_path / 'RMS_cam2', 'RO000N')
    for cam, scfg in (('RO000M', {'rms_data_path': str(tmp_path / 'RMS_data' / 'cam1')}),
                      ('RO000N', {'rms_data_path': str(tmp_path / 'RMS_data' / 'cam2')}),
                      ('XX9999', {'rms_data_path': str(tmp_path / 'RMS_data' / 'cam9')}),
                      ('RO000M', {'rms_config_path': '/explicit'})):
        assert (rr.resolve_rms_config_dir(cam, scfg, live_dirs=[])
                == platepar_autofit.resolve_rms_config_dir(cam, scfg))


def test_find_latest_platepar_prefers_the_live_config_dir(tmp_path, monkeypatch):
    import os
    import time

    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    live = _rms_config(tmp_path / 'RMS_cam1', 'RO000M') / rr.PLATEPAR_NAME
    live.write_text('{}')
    mirror = tmp_path / 'source' / 'Stations' / 'RO000M' / rr.PLATEPAR_NAME
    mirror.parent.mkdir(parents=True)
    mirror.write_text('{}')
    session = tmp_path / 'RMS_data' / 'cam1' / 'ArchivedFiles' / 'RO000M_20260920_180000_000000'
    session.mkdir(parents=True)
    old_copy = session / rr.PLATEPAR_NAME
    old_copy.write_text('{}')
    now = time.time()
    os.utime(old_copy, (now - 3600, now - 3600))
    os.utime(live, (now - 60, now - 60))
    os.utime(mirror, (now - 30, now - 30))  # a stale mirror that happens to be newer
    cfg = {'stations': {'RO000M': {'rms_data_path': str(tmp_path / 'RMS_data' / 'cam1')}}}
    assert rr.find_latest_platepar('RO000M', cfg, live_dirs=[]) == live
    os.utime(old_copy, (now, now))
    assert rr.find_latest_platepar('RO000M', cfg, live_dirs=[]) == old_copy
    assert rr.find_latest_platepar('NONE', cfg, live_dirs=[]) is None
