"""Night products (timelapse, night stack, colour meteor stack) in the archive
uploader and their producers.

A night product is uploaded once per version: its marker is the (size,
mtime_ns) that was uploaded, so a rebuilt file goes up again and an unchanged
one never does. A product younger than the minimum age waits, and the
producers write through a hidden temp file so the uploader never sees half a
file under the real name.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from PIL import Image

import archive_upload
import flags_manager
import timelapse_build
from archive_upload import _Uploader

CAM = 'RO000H'
NIGHT = '20260920'
TIMELAPSE = f'{CAM}_{NIGHT}_timelapse.mp4'
NIGHT_STACK = f'{CAM}_{NIGHT}_night_stack.webp'
METEOR_STACK = f'{CAM}_{NIGHT}_color_meteor_stack.webp'


def _age(path: Path, seconds: float) -> None:
    t = time.time() - seconds
    os.utime(path, (t, t))


@pytest.fixture
def night(tmp_path) -> tuple[dict, Path]:
    capture = tmp_path / 'color_capture'
    night = capture / CAM / NIGHT
    (night / 'stacks').mkdir(parents=True)
    state = flags_manager._empty_state(CAM, NIGHT)
    (night / 'state.json').write_text(json.dumps(state))
    cfg = {
        'videocapture_path': str(capture),
        'stations': {CAM: {}},
        'archive': {'enabled': True, 'upload_stacks': True, 'upload_meteors': False,
                    'upload_timelapses': True, 'upload_rms': False},
    }
    return cfg, night


@pytest.fixture
def posts(monkeypatch) -> list[str]:
    seen: list[str] = []
    monkeypatch.setattr(archive_upload, 'post_blob_status',
                        lambda url, *a, **k: (seen.append(url.split(f'/{NIGHT}/', 1)[1]), 200)[1])
    return seen


def _uploader(cfg: dict) -> _Uploader:
    up = _Uploader(cfg)
    up.ingest_base_url = 'https://vps.example'
    up.ingest_key = 'SECRET'
    up.ingest_host_key = 'gmnro03'
    return up


def _state(night: Path) -> dict:
    return json.loads((night / 'state.json').read_text())


@pytest.mark.parametrize('pass_', ['cycle', 'cycle_night'])
def test_a_rebuilt_timelapse_uploads_again(night, posts, pass_):
    cfg, nd = night
    tl = nd / TIMELAPSE
    tl.write_bytes(b'first build')
    _age(tl, 3600)

    def run() -> None:
        up = _uploader(cfg)
        up.cycle() if pass_ == 'cycle' else up.cycle_night(CAM, NIGHT)

    run()
    assert posts == [f'timelapse/{TIMELAPSE}']
    run()
    assert posts == [f'timelapse/{TIMELAPSE}'], 'unchanged file: no second upload'
    tl.write_bytes(b'second, longer build')
    _age(tl, 600)
    run()
    assert posts == [f'timelapse/{TIMELAPSE}'] * 2
    st = tl.stat()
    assert _state(nd)['products_uploaded']['timelapse'] == [st.st_size, st.st_mtime_ns]
    assert _state(nd)['timelapse_uploaded'] is True


def test_a_night_flagged_before_the_marker_existed_is_not_resent(night, posts):
    cfg, nd = night
    state = _state(nd)
    state['timelapse_uploaded'] = True
    state['night_stack_uploaded'] = True
    (nd / 'state.json').write_text(json.dumps(state))
    for name in (TIMELAPSE, NIGHT_STACK):
        (nd / name).write_bytes(b'x')
        _age(nd / name, 3600)
    _uploader(cfg).cycle()
    assert posts == []


def test_a_fresh_product_waits_for_the_minimum_age(night, posts):
    cfg, nd = night
    (nd / TIMELAPSE).write_bytes(b'still being written?')
    _uploader(cfg).cycle()
    assert posts == []
    _age(nd / TIMELAPSE, archive_upload._PRODUCT_MIN_AGE_S + 1)
    _uploader(cfg).cycle()
    assert posts == [f'timelapse/{TIMELAPSE}']


@pytest.mark.parametrize('pass_', ['cycle', 'cycle_night'])
def test_meteor_stack_is_a_product_not_a_chunk(night, posts, pass_):
    cfg, nd = night
    ms = nd / 'stacks' / METEOR_STACK
    ms.write_bytes(b'meteor composite')
    _age(ms, 3600)
    up = _uploader(cfg)
    up.cycle() if pass_ == 'cycle' else up.cycle_night(CAM, NIGHT)
    assert posts == [f'stacks/{METEOR_STACK}']
    state = _state(nd)
    assert state['chunks'] == {}, 'no phantom chunk entry'
    assert 'color_meteor_stack' in state['products_uploaded']
    # Rebuilt after more chunks were stacked: it goes up again.
    ms.write_bytes(b'meteor composite with more meteors')
    _age(ms, 600)
    up = _uploader(cfg)
    up.cycle() if pass_ == 'cycle' else up.cycle_night(CAM, NIGHT)
    assert posts == [f'stacks/{METEOR_STACK}'] * 2


def test_marker_is_the_version_read_not_a_later_rebuild(night, monkeypatch):
    """A rebuild that lands while the old version is uploading must still
    count as not uploaded: the marker is the signature taken before the read."""
    cfg, nd = night
    tl = nd / TIMELAPSE
    tl.write_bytes(b'v1')
    _age(tl, 3600)
    before = tl.stat()

    def post_then_rebuild(url, *a, **k):
        tl.write_bytes(b'v2, rebuilt mid-upload')
        return 200

    monkeypatch.setattr(archive_upload, 'post_blob_status', post_then_rebuild)
    _uploader(cfg).cycle()
    assert _state(nd)['products_uploaded']['timelapse'] == [before.st_size, before.st_mtime_ns]


# ---------------------------------------------------------------------------
# Producers write atomically
# ---------------------------------------------------------------------------

def _webp(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(np.full((8, 8, 3), 40, dtype=np.uint8)).save(path)


def test_timelapse_is_not_visible_until_ffmpeg_finishes(tmp_path):
    stacks = tmp_path / 'stacks'
    _webp(stacks / f'{CAM}_{NIGHT}_210000_stack.webp')
    out_dir = tmp_path / 'out'
    final = out_dir / TIMELAPSE
    seen_during: list[bool] = []

    def ffmpeg(cmd, *a, **k):
        seen_during.append(final.exists())
        target = Path(cmd[-1])
        assert target.name.startswith('.') and target.name.endswith('.tmp')
        target.write_bytes(b'mp4')
        return 0

    with patch('timelapse_build.subprocess.call', side_effect=ffmpeg):
        assert timelapse_build.build_timelapse(NIGHT, CAM, stacks, out_dir) == final
    assert seen_during == [False]
    assert final.read_bytes() == b'mp4'
    assert [p.name for p in out_dir.iterdir()] == [TIMELAPSE]


def test_failed_timelapse_leaves_nothing_behind(tmp_path):
    stacks = tmp_path / 'stacks'
    _webp(stacks / f'{CAM}_{NIGHT}_210000_stack.webp')
    out_dir = tmp_path / 'out'

    def ffmpeg(cmd, *a, **k):
        Path(cmd[-1]).write_bytes(b'partial')
        return 1

    with patch('timelapse_build.subprocess.call', side_effect=ffmpeg):
        assert timelapse_build.build_timelapse(NIGHT, CAM, stacks, out_dir) is None
    assert list(out_dir.iterdir()) == []


def test_night_stack_is_written_atomically(tmp_path, monkeypatch):
    thumbs = tmp_path / 'thumbs'
    _webp(thumbs / f'{CAM}_{NIGHT}_210000_stack.webp')
    out_dir = tmp_path / 'out'
    out_dir.mkdir()
    monkeypatch.setattr(timelapse_build.cc, 'rebalance_composite', lambda acc, cfg: acc)
    replaced: list[tuple[str, str]] = []
    real = os.replace

    def spy(src, dst):
        replaced.append((Path(src).name, Path(dst).name))
        return real(src, dst)

    monkeypatch.setattr(timelapse_build.os, 'replace', spy)
    timelapse_build.build_night_stack(NIGHT, CAM, thumbs, out_dir)
    assert len(replaced) == 1
    src, dst = replaced[0]
    assert src.startswith('.') and src.endswith('.tmp') and dst == NIGHT_STACK
    assert Image.open(out_dir / NIGHT_STACK).format == 'WEBP'


@pytest.mark.skipif(__import__('shutil').which('ffmpeg') is None, reason='ffmpeg not installed')
def test_timelapse_frames_exclude_the_colour_meteor_stack(tmp_path):
    """The plain timelapse's ffmpeg glob used to be '*_stack.webp', which also
    took the night's colour meteor composite as its last frame."""
    import subprocess
    stacks = tmp_path / 'stacks'
    for hhmmss in ('210000', '210020'):
        _webp(stacks / f'{CAM}_{NIGHT}_{hhmmss}_stack.webp')
    _webp(stacks / METEOR_STACK)
    out = timelapse_build.build_timelapse(NIGHT, CAM, stacks, tmp_path / 'out')
    assert out is not None
    probe = subprocess.run(
        ['ffprobe', '-v', 'error', '-count_frames', '-select_streams', 'v:0',
         '-show_entries', 'stream=nb_read_frames', '-of', 'csv=p=0', str(out)],
        capture_output=True, text=True, timeout=60)
    assert probe.stdout.strip() == '2'


def test_a_pass_writes_state_json_in_batches_not_per_file(night, posts, monkeypatch):
    """One save per ~50 uploaded files, not one per file."""
    cfg, nd = night
    for i in range(120):
        (nd / 'stacks' / f'{CAM}_{NIGHT}_2{i:05d}_stack.webp').write_bytes(b'webp')
    saves: list[int] = []
    real_save = flags_manager.save
    monkeypatch.setattr(flags_manager, 'save',
                        lambda *a, **k: (saves.append(1), real_save(*a, **k))[1])
    assert _uploader(cfg).cycle() == 120
    assert len(saves) <= 120 // archive_upload._MARK_FLUSH_EVERY + 2
    chunks = _state(nd)['chunks']
    assert len(chunks) == 120 and all(c['stack_uploaded'] for c in chunks.values())
