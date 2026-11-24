"""Upload churn + per-scan state.json cache + live-thumbnail render cache.

Covers three station-side hot paths measured on the fleet 2026-09-24:
- archive_upload re-POSTed every night's state.json after each restart
  (the dedup map lived only in memory) -> persisted _SentSigs;
- the uploader and janitor re-parsed a night's state.json once per chunk
  per check -> flags_manager.scan_cache / peek;
- station_api re-rendered the same FF file on every live-thumb poll.
"""

from __future__ import annotations

import io
import json
import os
import threading
from pathlib import Path

import pytest

import archive_upload
import flags_manager
import janitor_storage_watchdog
import station_api
from archive_upload import _SentSigs, _Uploader

# Passes skip without ingest settings; these drive them through stubbed POSTs.
pytestmark = pytest.mark.usefixtures('ingest_ready')

STATION = 'RO000H'


def _cfg(tmp_path: Path) -> dict:
    capture = tmp_path / 'color_capture'
    capture.mkdir(exist_ok=True)
    return {
        'videocapture_path': str(capture),
        'stations': {STATION: {}},
        'archive': {'enabled': True, 'upload_meteors': True, 'upload_stacks': False,
                    'upload_timelapses': False, 'upload_rms': False},
    }


def _night(cfg: dict, date: str, chunks: dict | None = None) -> Path:
    d = Path(cfg['videocapture_path']) / STATION / date
    d.mkdir(parents=True, exist_ok=True)
    state = flags_manager._empty_state(STATION, date)
    state['chunks'] = chunks or {}
    (d / 'state.json').write_text(json.dumps(state))
    return d / 'state.json'


class _Recorder:
    """Stands in for the HTTP POST: records state.json uploads, always succeeds."""

    def __init__(self) -> None:
        self.posted: list[Path] = []

    def __call__(self, local: Path, camera: str, date: str, subdir: str) -> bool:
        self.posted.append(local)
        return True


def _uploader(cfg: dict, recorder: _Recorder) -> _Uploader:
    up = _Uploader(cfg)
    up._post_one = recorder  # type: ignore[method-assign]
    return up


# ---------------------------------------------------------------------------
# Persisted state.json signatures
# ---------------------------------------------------------------------------

def test_persisted_sig_survives_restart_and_skips_unchanged(tmp_path):
    cfg = _cfg(tmp_path)
    sj1 = _night(cfg, '20260920')
    sj2 = _night(cfg, '20260921')

    rec = _Recorder()
    _uploader(cfg, rec).cycle()
    assert sorted(rec.posted) == sorted([sj1, sj2])

    # A fresh process (new _Uploader) must not re-send settled nights.
    rec2 = _Recorder()
    _uploader(cfg, rec2).cycle()
    assert rec2.posted == []


def test_changed_state_json_uploads_again(tmp_path):
    cfg = _cfg(tmp_path)
    sj = _night(cfg, '20260920')
    _uploader(cfg, _Recorder()).cycle()

    flags_manager.mark_chunk_uploaded(STATION, '20260920', 'x_color.mkv', cfg)
    rec = _Recorder()
    _uploader(cfg, rec).cycle()
    assert rec.posted == [sj]


def test_same_size_rewrite_with_new_mtime_uploads(tmp_path):
    cfg = _cfg(tmp_path)
    sj = _night(cfg, '20260920')
    _uploader(cfg, _Recorder()).cycle()
    st = sj.stat()
    os.utime(sj, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    rec = _Recorder()
    _uploader(cfg, rec).cycle()
    assert rec.posted == [sj]


def test_failed_post_is_not_recorded(tmp_path):
    cfg = _cfg(tmp_path)
    sj = _night(cfg, '20260920')
    up = _Uploader(cfg)
    up._post_one = lambda *a: False  # type: ignore[method-assign]
    up.cycle()
    rec = _Recorder()
    _uploader(cfg, rec).cycle()
    assert rec.posted == [sj]


@pytest.mark.parametrize('garbage', ['', '{not json', '[1,2]', '{"sigs": {"a": 5}}', '\x00\xff'])
def test_corrupt_sig_file_is_treated_as_empty(tmp_path, garbage):
    cfg = _cfg(tmp_path)
    sj = _night(cfg, '20260920')
    sig_path = Path(os.environ['ROVIMEN_STATE_SIGS_PATH'])
    sig_path.write_text(garbage)
    rec = _Recorder()
    _uploader(cfg, rec).cycle()
    assert rec.posted == [sj]
    # ...and the file is healthy again afterwards.
    assert str(sj) in json.loads(sig_path.read_text())['sigs']


def test_entries_for_deleted_nights_are_pruned(tmp_path):
    cfg = _cfg(tmp_path)
    sj_old = _night(cfg, '20260501')
    _night(cfg, '20260920')
    _uploader(cfg, _Recorder()).cycle()
    sig_path = Path(os.environ['ROVIMEN_STATE_SIGS_PATH'])
    assert str(sj_old) in json.loads(sig_path.read_text())['sigs']

    sj_old.unlink()
    sj_old.parent.rmdir()
    _night(cfg, '20260922')
    _uploader(cfg, _Recorder()).cycle()
    assert str(sj_old) not in json.loads(sig_path.read_text())['sigs']


def test_one_shot_and_daemon_merge_without_losing_entries(tmp_path):
    """Two processes flushing different nights both end up on disk."""
    sig_path = tmp_path / 'sigs.json'
    nights = []
    for i in range(40):
        d = tmp_path / 'n' / f'{i:02d}'
        d.mkdir(parents=True)
        (d / 'state.json').write_text('{}')
        nights.append(d / 'state.json')

    a, b = _SentSigs(sig_path), _SentSigs(sig_path)
    errors: list[BaseException] = []

    def work(sigs: _SentSigs, paths: list[Path]) -> None:
        try:
            for p in paths:
                sigs.record(p, (1, 1))
                sigs.flush()
        except BaseException as exc:  # pragma: no cover - failure path
            errors.append(exc)

    t1 = threading.Thread(target=work, args=(a, nights[::2]))
    t2 = threading.Thread(target=work, args=(b, nights[1::2]))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert not errors
    on_disk = json.loads(sig_path.read_text())['sigs']
    assert set(on_disk) == {str(p) for p in nights}


def test_stale_writer_can_only_cause_reupload_not_skip(tmp_path):
    """A process holding an older signature that flushes last must never make
    a newer file version look already-uploaded."""
    cfg = _cfg(tmp_path)
    sj = _night(cfg, '20260920')
    stale = _Uploader(cfg)          # loads the (empty) map now
    _uploader(cfg, _Recorder()).cycle()  # another process uploads v1
    flags_manager.mark_chunk_uploaded(STATION, '20260920', 'x_color.mkv', cfg)  # v2
    st = sj.stat()
    stale._state_sigs.record(sj, (st.st_size - 1, st.st_mtime_ns - 1))  # bogus old sig
    stale._state_sigs.flush()
    rec = _Recorder()
    _uploader(cfg, rec).cycle()
    assert rec.posted == [sj]


def test_flush_failure_keeps_dirty_and_does_not_raise(tmp_path, monkeypatch):
    blocker = tmp_path / 'file'
    blocker.write_text('x')
    sigs = _SentSigs(blocker / 'sub' / 'sigs.json')  # parent is a file -> OSError
    d = tmp_path / 'n'
    d.mkdir()
    sigs.record(d / 'state.json', (1, 2))
    sigs.flush()
    assert sigs.matches(d / 'state.json', (1, 2))
    assert sigs._dirty


# ---------------------------------------------------------------------------
# Per-scan state.json cache
# ---------------------------------------------------------------------------

def _count_loads(monkeypatch) -> list[int]:
    calls = [0]
    real = flags_manager.load

    def counting(*a, **kw):
        calls[0] += 1
        return real(*a, **kw)

    monkeypatch.setattr(flags_manager, 'load', counting)
    return calls


def test_scan_cache_parses_once_and_matches_uncached(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    chunks = {f'c{i}_color.mkv': {**flags_manager._empty_chunk(), 'uploaded': i % 2 == 0,
                                  'stack_uploaded': i % 3 == 0}
              for i in range(50)}
    _night(cfg, '20260920', chunks)
    names = list(chunks) + ['missing_color.mkv']

    def answers() -> list[tuple[bool, bool]]:
        return [(flags_manager.is_chunk_uploaded(STATION, '20260920', n, cfg),
                 flags_manager.is_chunk_stack_uploaded(STATION, '20260920', n, cfg))
                for n in names]

    uncached = answers()
    calls = _count_loads(monkeypatch)
    with flags_manager.scan_cache():
        cached = answers()
    assert cached == uncached
    assert calls[0] == 1


def test_scan_cache_sees_in_scan_writes(tmp_path):
    cfg = _cfg(tmp_path)
    _night(cfg, '20260920', {'a_color.mkv': flags_manager._empty_chunk()})
    with flags_manager.scan_cache():
        assert not flags_manager.is_chunk_uploaded(STATION, '20260920', 'a_color.mkv', cfg)
        flags_manager.mark_chunk_uploaded(STATION, '20260920', 'a_color.mkv', cfg)
        assert flags_manager.is_chunk_uploaded(STATION, '20260920', 'a_color.mkv', cfg)


def test_scan_cache_sees_other_writer_replacing_file(tmp_path):
    """A write that bypasses flags_manager (another process) is caught by the
    inode/mtime/size signature, even at identical size."""
    cfg = _cfg(tmp_path)
    sj = _night(cfg, '20260920', {'a_color.mkv': {**flags_manager._empty_chunk(), 'lock': None}})
    with flags_manager.scan_cache():
        assert flags_manager.peek(STATION, '20260920', cfg)['chunks']['a_color.mkv']['lock'] is None
        state = json.loads(sj.read_text())
        state['chunks']['a_color.mkv']['uploaded'] = True   # same length as 'false' - 1
        tmp = sj.with_name('other.tmp')
        tmp.write_text(json.dumps(state))
        os.replace(tmp, sj)
        assert flags_manager.is_chunk_uploaded(STATION, '20260920', 'a_color.mkv', cfg)


def test_scan_cache_is_bounded(tmp_path):
    cfg = _cfg(tmp_path)
    for i in range(flags_manager._SCAN_CACHE_MAX_NIGHTS + 5):
        _night(cfg, f'202609{i:02d}')
    with flags_manager.scan_cache():
        for i in range(flags_manager._SCAN_CACHE_MAX_NIGHTS + 5):
            flags_manager.is_timelapse_uploaded(STATION, f'202609{i:02d}', cfg)
        assert len(flags_manager._scan_cache.get()) == flags_manager._SCAN_CACHE_MAX_NIGHTS
    assert flags_manager._scan_cache.get() is None


def test_uploader_scan_parses_each_night_once(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    for date in ('20260920', '20260921'):
        chunks = {}
        night = Path(cfg['videocapture_path']) / STATION / date
        night.mkdir(parents=True)
        (night / 'stacks').mkdir()
        for i in range(30):
            name = f'{STATION}_{date}_0000{i:02d}_color.mkv'
            (night / name).write_bytes(b'x')
            (night / 'stacks' / name.replace('_color.mkv', '_stack.webp')).write_bytes(b'x')
            chunks[name] = {**flags_manager._empty_chunk(), 'lock': {'t': 1}, 'reencoded': True,
                            'uploaded': i % 2 == 0, 'stack_uploaded': True}
        _night(cfg, date, chunks)
    up = _Uploader(cfg)
    uncached = up._scan_all()
    calls = _count_loads(monkeypatch)
    with flags_manager.scan_cache():
        cached = up._scan_all()
    assert cached == uncached
    assert len(cached.locked_mkvs) == 30
    assert calls[0] == 2


def test_janitor_run_uses_scan_cache(tmp_path, monkeypatch):
    seen: list[bool] = []
    jan = janitor_storage_watchdog.Janitor.__new__(janitor_storage_watchdog.Janitor)
    jan.capture_path = tmp_path
    jan.same_fs = True
    jan.rms_on_capture_fs = True
    jan.station_ids = []
    jan.log_base = tmp_path
    monkeypatch.setattr(jan, '_run_normal_sweep',
                        lambda: seen.append(flags_manager._scan_cache.get() is not None))
    monkeypatch.setattr(jan, '_run_disk_check',
                        lambda: seen.append(flags_manager._scan_cache.get() is not None))
    monkeypatch.setattr(jan, '_write_state', lambda *_a: None)
    jan.run()
    assert seen == [True, True]


def test_janitor_is_locked_reads_through_peek(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _night(cfg, '20260920', {'a_color.mkv': {**flags_manager._empty_chunk(), 'lock': {'t': 1}},
                             'b_color.mkv': flags_manager._empty_chunk()})
    night = Path(cfg['videocapture_path']) / STATION / '20260920'
    calls = _count_loads(monkeypatch)
    with flags_manager.scan_cache():
        assert janitor_storage_watchdog._is_locked(night / 'a_color.mkv', STATION, '20260920', cfg)
        assert not janitor_storage_watchdog._is_locked(night / 'b_color.mkv', STATION, '20260920', cfg)
        assert flags_manager.is_chunk_uploaded(STATION, '20260920', 'a_color.mkv', cfg) is False
    assert calls[0] == 1


# ---------------------------------------------------------------------------
# station_api live thumbnail
# ---------------------------------------------------------------------------

@pytest.fixture
def ff_env(tmp_path, monkeypatch, station_patch):
    rms = tmp_path / 'RMS_data'
    session = rms / 'CapturedFiles' / f'{STATION}_20260923_180000_000000'
    session.mkdir(parents=True)
    cfg = {'stations': {STATION: {'rms_data_path': str(rms)}}}
    station_patch('load_config', lambda: cfg)
    renders: list[list[Path]] = []

    def fake_render(paths):
        renders.append(list(paths))
        return io.BytesIO(b'RIFF' + Path(paths[0]).name.encode())

    station_patch('_ff_maxpixel_to_webp', fake_render)
    for fn in (station_api._latest_ff_webp_bytes, station_api._cam_sessions_newest_first,
               station_api._newest_ff_in_session):
        fn.cache_clear()
    return session, renders


def _get(cam: str = STATION):
    return station_api.app.test_client().get(f'/api/latest_ff_maxpixel/{cam}')


def test_live_thumb_cache_hit_and_miss_on_ff_change(ff_env):
    session, renders = ff_env
    ff1 = session / f'FF_{STATION}_20260923_200000_123_0001.fits'
    ff1.write_bytes(b'a' * 10)

    r1 = _get()
    assert r1.status_code == 200
    assert r1.mimetype == 'image/webp'
    assert r1.headers['X-FF-Timestamp'] == '2026-09-23T20:00:00.123Z'
    assert r1.headers['Cache-Control'] == 'no-store, no-cache, must-revalidate'
    r2 = _get()
    assert r2.data == r1.data
    assert len(renders) == 1  # cache hit

    # Same file rewritten -> re-render.
    st = ff1.stat()
    os.utime(ff1, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000))
    _get()
    assert len(renders) == 2

    # A newer FF appears -> served and rendered.
    ff2 = session / f'FF_{STATION}_20260923_200010_456_0002.fits'
    ff2.write_bytes(b'b' * 10)
    st_dir = session.stat()
    os.utime(session, ns=(st_dir.st_atime_ns, st_dir.st_mtime_ns + 5_000_000))
    r4 = _get()
    assert r4.headers['X-FF-Timestamp'] == '2026-09-23T20:00:10.456Z'
    assert renders[-1] == [ff2]
    assert len(renders) == 3


def test_live_thumb_render_failure_not_cached(ff_env, monkeypatch, station_patch):
    session, renders = ff_env
    (session / f'FF_{STATION}_20260923_200000_123_0001.fits').write_bytes(b'a')
    station_patch('_ff_maxpixel_to_webp', lambda p: None)
    assert _get().status_code == 503
    station_patch('_ff_maxpixel_to_webp', lambda p: io.BytesIO(b'ok'))
    r = _get()
    assert r.status_code == 200 and r.data == b'ok'


def test_live_thumb_404_without_ff(ff_env):
    assert _get().status_code == 404
