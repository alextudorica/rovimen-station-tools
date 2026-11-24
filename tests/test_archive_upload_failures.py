"""Failure injection for the archive upload passes (archive_upload.py).

A failing disk must cost only what is on it: an unreadable night directory or
colour disk is skipped, the rest of the pass (other nights, the RMS files on
their own disk) still uploads, and nothing raises out of the pass.
"""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path

import pytest

import archive_upload
import flags_manager
from archive_upload import _Uploader

CAM = 'RO000H'

needs_non_root = pytest.mark.skipif(
    hasattr(os, 'geteuid') and os.geteuid() == 0,
    reason='permission bits do not stop root')


def _night(capture: Path, date: str, n_stacks: int = 2) -> Path:
    night = capture / CAM / date
    (night / 'stacks').mkdir(parents=True)
    for i in range(n_stacks):
        (night / 'stacks' / f'{CAM}_{date}_2100{i:02d}_stack.webp').write_bytes(b'webp')
    state = flags_manager._empty_state(CAM, date)
    (night / 'state.json').write_text(json.dumps(state))
    return night


def _rms(tmp_path: Path, date: str) -> Path:
    rms = tmp_path / 'rms'
    session = rms / 'ArchivedFiles' / f'{CAM}_{date}_210000_000000'
    session.mkdir(parents=True)
    (session / f'FTPdetectinfo_{CAM}_{date}_210000_000000.txt').write_text('ftp')
    return rms


def _cfg(tmp_path: Path, rms: Path | None = None) -> dict:
    capture = tmp_path / 'color_capture'
    capture.mkdir(exist_ok=True)
    station: dict = {'rms_data_path': str(rms)} if rms else {}
    return {
        'videocapture_path': str(capture),
        'stations': {CAM: station},
        'archive': {'enabled': True, 'upload_stacks': True, 'upload_meteors': True,
                    'upload_timelapses': True, 'upload_rms': True},
    }


@pytest.fixture
def posts(monkeypatch) -> list[str]:
    seen: list[str] = []

    def fake_post(url, key, data, timeout=180):
        seen.append(url.split(f'/blob/{CAM}/', 1)[1])
        return 200

    monkeypatch.setattr(archive_upload, 'post_blob_status', fake_post)
    return seen


def _uploader(cfg: dict) -> _Uploader:
    up = _Uploader(cfg)
    up.ingest_base_url = 'https://vps.example'
    up.ingest_key = 'SECRET'
    up.ingest_host_key = 'gmnro03'
    return up


@needs_non_root
def test_unreadable_night_skips_only_that_night(tmp_path, posts):
    rms = _rms(tmp_path, '20260920')
    cfg = _cfg(tmp_path, rms)
    capture = Path(cfg['videocapture_path'])
    _night(capture, '20260920')
    bad = _night(capture, '20260919')
    bad.chmod(0)
    try:
        _uploader(cfg).cycle()
    finally:
        bad.chmod(0o755)
    assert any(p.startswith('20260920/stacks/') for p in posts)
    assert not any(p.startswith('20260919/') for p in posts)
    assert '20260920/rms/FTPdetectinfo_RO000H_20260920_210000_000000.txt' in posts


@needs_non_root
def test_unreadable_capture_root_still_uploads_rms(tmp_path, posts):
    rms = _rms(tmp_path, '20260920')
    cfg = _cfg(tmp_path, rms)
    capture = Path(cfg['videocapture_path'])
    _night(capture, '20260920')
    station_dir = capture / CAM
    station_dir.chmod(0)
    try:
        _uploader(cfg).cycle()
    finally:
        station_dir.chmod(0o755)
    assert posts == ['20260920/rms/FTPdetectinfo_RO000H_20260920_210000_000000.txt']


def test_one_night_pass_uploads_rms_without_a_colour_night(tmp_path, posts):
    rms = _rms(tmp_path, '20260920')
    cfg = _cfg(tmp_path, rms)
    _uploader(cfg).cycle_night(CAM, '20260920')
    assert posts == ['20260920/rms/FTPdetectinfo_RO000H_20260920_210000_000000.txt']


@needs_non_root
def test_one_night_pass_uploads_rms_when_the_colour_night_is_unreadable(tmp_path, posts):
    rms = _rms(tmp_path, '20260920')
    cfg = _cfg(tmp_path, rms)
    night = _night(Path(cfg['videocapture_path']), '20260920')
    night.chmod(0)
    try:
        _uploader(cfg).cycle_night(CAM, '20260920')
    finally:
        night.chmod(0o755)
    assert posts == ['20260920/rms/FTPdetectinfo_RO000H_20260920_210000_000000.txt']


def test_enospc_while_marking_does_not_abort_the_pass(tmp_path, posts, monkeypatch):
    """A full disk under state.json loses the flags (the files re-upload next
    pass, which the archive dedups) but never stops the pass."""
    cfg = _cfg(tmp_path)
    _night(Path(cfg['videocapture_path']), '20260920', n_stacks=3)
    real_replace = os.replace

    def full_disk(src, dst, *a, **k):
        if str(dst).endswith('state.json'):
            raise OSError(errno.ENOSPC, 'No space left on device')
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(flags_manager.os, 'replace', full_disk)
    _uploader(cfg).cycle()
    stacks = [p for p in posts if '/stacks/' in p]
    assert len(stacks) == 3


def test_file_deleted_after_the_scan_is_skipped(tmp_path, posts, monkeypatch):
    cfg = _cfg(tmp_path)
    night = _night(Path(cfg['videocapture_path']), '20260920', n_stacks=3)
    real_scan = _Uploader._scan_all
    victim = night / 'stacks' / f'{CAM}_20260920_210001_stack.webp'

    def scan_then_delete(self):
        results = real_scan(self)
        victim.unlink()
        return results

    monkeypatch.setattr(_Uploader, '_scan_all', scan_then_delete)
    _uploader(cfg).cycle()
    stacks = sorted(p for p in posts if '/stacks/' in p)
    assert stacks == [f'20260920/stacks/{CAM}_20260920_210000_stack.webp',
                      f'20260920/stacks/{CAM}_20260920_210002_stack.webp']
