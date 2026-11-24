"""Characterization of the archive upload passes (archive_upload.py).

One fixture night holds every kind of file the uploader ships: a locked and
re-encoded clip with its stack, an unlocked clip, a locked clip that is not
re-encoded yet, an already-uploaded clip with a starprint details variant, chunk
stacks, the colour meteor stack, the timelapse and night stack, FF thumbnails,
state.json and the RMS detection files. Each test records the order of the blob
POSTs (as ``subdir/filename`` relative to the night) and the night's state.json
afterwards, for the one-night pass (``cycle_night``, dawn) and the full scan
(``cycle``, the daemon).

These pin the observable contract so a restructuring of the passes can be
checked against it. Any intended change to the order or the final flags has to
edit the expectations here, which keeps it visible in review.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

import archive_upload
import flags_manager
from archive_upload import _Uploader

CAM = 'RO000H'
NIGHT = '20260920'

CLIP_A = f'{CAM}_{NIGHT}_210000_color.mkv'   # locked + re-encoded, pending
CLIP_B = f'{CAM}_{NIGHT}_210020_color.mkv'   # not locked
CLIP_C = f'{CAM}_{NIGHT}_210040_color.mkv'   # locked, not re-encoded yet
CLIP_D = f'{CAM}_{NIGHT}_210100_color.mkv'   # locked, already uploaded
DETAILS_D = f'{CAM}_{NIGHT}_210100_color.details.mkv'
METEOR_STACK = f'{CAM}_{NIGHT}_color_meteor_stack.webp'
TIMELAPSE = f'{CAM}_{NIGHT}_timelapse.mp4'
NIGHT_STACK = f'{CAM}_{NIGHT}_night_stack.webp'
SESSION = f'{CAM}_{NIGHT}_210000_000000'
FTP = f'FTPdetectinfo_{SESSION}.txt'
RADIANTS = f'{SESSION}_radiants.txt'
PLOT = f'{SESSION}_fieldsums.png'


def _stack(clip: str) -> str:
    return clip.replace('_color.mkv', '_stack.webp')


def _chunk(**kw) -> dict:
    base = {'ready': True, 'stacked': True, 'reencoded': False, 'lock': None,
            'uploaded': False, 'stack_uploaded': False}
    base.update(kw)
    return base


def _build(tmp_path: Path, **archive) -> tuple[dict, Path]:
    capture = tmp_path / 'color_capture'
    night = capture / CAM / NIGHT
    (night / 'stacks').mkdir(parents=True)
    (night / 'ffthumbs').mkdir()
    lock = {'lock_type': 'detection'}
    for clip in (CLIP_A, CLIP_B, CLIP_C, CLIP_D):
        (night / clip).write_bytes(b'mkv-' + clip.encode())
    (night / DETAILS_D).write_bytes(b'details')
    for clip in (CLIP_A, CLIP_B, CLIP_C, CLIP_D):
        (night / 'stacks' / _stack(clip)).write_bytes(b'webp-' + clip.encode())
    (night / 'stacks' / METEOR_STACK).write_bytes(b'meteor-stack')
    (night / TIMELAPSE).write_bytes(b'timelapse')
    (night / NIGHT_STACK).write_bytes(b'night-stack')
    (night / 'ffthumbs' / 'FF_new.webp').write_bytes(b'thumb-new')
    (night / 'ffthumbs' / 'FF_old.webp').write_bytes(b'thumb-old')

    state = flags_manager._empty_state(CAM, NIGHT)
    state['chunks'] = {
        CLIP_A: _chunk(lock=lock, reencoded=True),
        CLIP_B: _chunk(),
        CLIP_C: _chunk(lock=lock),
        CLIP_D: _chunk(lock=lock, reencoded=True, uploaded=True, stack_uploaded=True),
    }
    state['ffthumbs_uploaded'] = {'FF_old.webp': True}
    (night / 'state.json').write_text(json.dumps(state, indent=2))

    rms = tmp_path / 'rms'
    session = rms / 'ArchivedFiles' / SESSION
    session.mkdir(parents=True)
    (session / FTP).write_text('ftp')
    (session / RADIANTS).write_text('radiants')
    (session / PLOT).write_bytes(b'plot')

    # Everything was written an hour ago: night products younger than the
    # uploader's minimum age are held back.
    old = time.time() - 3600
    for f in list(capture.rglob('*')) + list(rms.rglob('*')):
        if f.is_file():
            os.utime(f, (old, old))

    cfg = {
        'videocapture_path': str(capture),
        'stations': {CAM: {'rms_data_path': str(rms)}},
        'archive': {
            'enabled': True,
            'upload_meteors': True,
            'upload_stacks': True,
            'upload_timelapses': True,
            'upload_rms': True,
            'publish_faststart_mp4': True,
            **archive,
        },
    }
    return cfg, night


@pytest.fixture
def recorded(monkeypatch):
    """Record every blob POST as ``subdir/name`` and let it succeed."""
    posts: list[str] = []
    marker = f'/blob/{CAM}/{NIGHT}/'

    def fake_post(url, key, data, timeout=180):
        posts.append(url.split(marker, 1)[1])
        return 200

    monkeypatch.setattr(archive_upload, 'post_blob_status', fake_post)
    return posts


def _uploader(cfg: dict) -> _Uploader:
    up = _Uploader(cfg)
    up.ingest_base_url = 'https://vps.example'
    up.ingest_key = 'SECRET'
    up.ingest_host_key = 'gmnro03'
    return up


def _groups(posts: list[str]) -> list[tuple[str, list[str]]]:
    """Consecutive POSTs grouped by subdir, names sorted within a group.

    The full scan lists a night's files in filesystem order, so the order
    inside one group is not part of the contract; the order of the groups is.
    """
    out: list[tuple[str, list[str]]] = []
    for rel in posts:
        subdir, _, name = rel.rpartition('/')
        if out and out[-1][0] == subdir:
            out[-1][1].append(name)
        else:
            out.append((subdir, [name]))
    return [(subdir, sorted(names)) for subdir, names in out]


def _state(night: Path) -> dict:
    return json.loads((night / 'state.json').read_text())


# ---------------------------------------------------------------------------
# One-night pass (dawn Phase 3 / Phase 5)
# ---------------------------------------------------------------------------

CYCLE_NIGHT_GROUPS = [
    ('meteors', [CLIP_A]),
    ('stacks', [_stack(CLIP_A)]),
    ('meteors', [DETAILS_D]),
    ('', ['state.json']),
    ('rms', sorted([FTP, PLOT, RADIANTS])),
    ('ffthumbs', ['FF_new.webp']),
    ('stacks', sorted([_stack(CLIP_B), _stack(CLIP_C), METEOR_STACK])),
    ('timelapse', sorted([NIGHT_STACK, TIMELAPSE])),
]


def test_cycle_night_post_order(tmp_path, recorded):
    cfg, _night = _build(tmp_path)
    _uploader(cfg).cycle_night(CAM, NIGHT)
    assert _groups(recorded) == CYCLE_NIGHT_GROUPS


def test_cycle_night_final_state(tmp_path, recorded):
    cfg, night = _build(tmp_path)
    count = _uploader(cfg).cycle_night(CAM, NIGHT)
    state = _state(night)
    chunks = state['chunks']
    assert chunks[CLIP_A]['uploaded'] is True
    assert chunks[CLIP_A]['stack_uploaded'] is True
    assert chunks[CLIP_B]['uploaded'] is False and chunks[CLIP_B]['stack_uploaded'] is True
    assert chunks[CLIP_C]['uploaded'] is False and chunks[CLIP_C]['stack_uploaded'] is True
    # The colour meteor stack is a night product, not a chunk: no phantom entry.
    assert set(chunks) == {CLIP_A, CLIP_B, CLIP_C, CLIP_D}
    assert set(state['products_uploaded']) == {'color_meteor_stack', 'timelapse', 'night_stack'}
    assert state['timelapse_uploaded'] is True
    assert state['night_stack_uploaded'] is True
    assert set(state['ffthumbs_uploaded']) == {'FF_new.webp', 'FF_old.webp'}
    assert set(state['rms_uploaded']) == {FTP, RADIANTS, PLOT}
    # The details variant ships in the one-night pass too, then goes.
    assert not (night / DETAILS_D).exists()
    # No faststart sibling is made on the station (the VPS derives it).
    assert not (night / CLIP_A.replace('.mkv', '.mp4')).exists()
    # Clips, stacks, products, thumbnails and RMS files count; state.json
    # doesn't.
    assert count == 1 + 1 + 1 + 3 + 1 + 3 + 2


# ---------------------------------------------------------------------------
# Full scan (the archive-upload daemon, dawn's backfill sweep)
# ---------------------------------------------------------------------------

CYCLE_GROUPS = CYCLE_NIGHT_GROUPS


def test_cycle_post_order(tmp_path, recorded):
    cfg, _night = _build(tmp_path)
    _uploader(cfg).cycle()
    assert _groups(recorded) == CYCLE_GROUPS


def test_cycle_final_state_and_details_shipped(tmp_path, recorded):
    cfg, night = _build(tmp_path)
    _uploader(cfg).cycle()
    state = _state(night)
    assert state['chunks'][CLIP_A]['uploaded'] is True
    assert not (night / DETAILS_D).exists()
    assert set(state['rms_uploaded']) == {FTP, RADIANTS, PLOT}


def test_second_pass_resends_only_what_changed(tmp_path, recorded):
    """After a one-night pass the full scan only resends state.json, which the
    marks after its upload changed."""
    cfg, _night = _build(tmp_path)
    _uploader(cfg).cycle_night(CAM, NIGHT)
    recorded.clear()
    _uploader(cfg).cycle()
    assert _groups(recorded) == [('', ['state.json'])]


def test_a_second_uploader_does_not_resend_what_the_first_shipped(tmp_path, recorded):
    """Dawn, the daemon and the station API can work on the same night. An
    uploader whose plan is stale must see the other's flags before each POST."""
    cfg, _night = _build(tmp_path)
    first, second = _uploader(cfg), _uploader(cfg)
    real_scan = _Uploader._scan_all

    def scan_then_let_the_other_run(self):
        scan = real_scan(self)
        second.cycle()
        recorded.clear()
        return scan

    first._scan_all = scan_then_let_the_other_run.__get__(first)  # type: ignore[method-assign]
    first.cycle()
    assert [p for p in recorded if p != 'state.json'] == []


def test_stacks_off_sends_the_clip_stack_to_meteors(tmp_path, recorded):
    """With upload_stacks off, a clip's stack still reaches the archive next to
    the clip (meteors/), and no other stack is sent."""
    cfg, _night = _build(tmp_path, upload_stacks=False)
    _uploader(cfg).cycle_night(CAM, NIGHT)
    stacks = [p for p in recorded if p.endswith('_stack.webp') and 'night_stack' not in p]
    assert stacks == [f'meteors/{_stack(CLIP_A)}']


# ---------------------------------------------------------------------------
# Entry points: upload_chunk, delete_chunk, run_upload_loop
# ---------------------------------------------------------------------------

def _ready(monkeypatch) -> None:
    monkeypatch.setattr(archive_upload, '_resolve_base_url', lambda cfg: 'https://vps.example')
    monkeypatch.setattr(archive_upload, '_resolve_ingest_key', lambda: 'SECRET')
    monkeypatch.setattr(archive_upload, '_resolve_host_key', lambda cfg: 'gmnro03')


def test_upload_chunk_encodes_then_ships_clip_stack_and_state(tmp_path, recorded, monkeypatch):
    cfg, night = _build(tmp_path)
    _ready(monkeypatch)
    mkv = night / CLIP_C
    encoded: list[str] = []

    def fake_encode(path, cfg):
        encoded.append(path.name)
        flags_manager.mark_reencoded(CAM, NIGHT, path.name, cfg)

    monkeypatch.setattr(archive_upload.encoder, 'process_chunk', fake_encode)
    assert archive_upload.upload_chunk(CAM, NIGHT, mkv, cfg) is True
    assert encoded == [CLIP_C]
    assert recorded == [
        f'meteors/{CLIP_C}',
        f'meteors/{_stack(CLIP_C)}', 'state.json',
    ]
    chunk = _state(night)['chunks'][CLIP_C]
    assert chunk['uploaded'] is True and chunk['stack_uploaded'] is True


def test_upload_chunk_leaves_a_clip_that_failed_to_encode(tmp_path, recorded, monkeypatch):
    """A failed re-encode must not put the raw capture in the archive: dawn
    re-encodes the clip in place later, and a clip marked uploaded is never
    sent again, so the archive would keep the raw file forever."""
    cfg, night = _build(tmp_path)
    _ready(monkeypatch)

    def boom(path, cfg):
        raise RuntimeError('vaapi died')

    monkeypatch.setattr(archive_upload.encoder, 'process_chunk', boom)
    assert archive_upload.upload_chunk(CAM, NIGHT, night / CLIP_C, cfg) is False
    assert recorded == []
    assert _state(night)['chunks'][CLIP_C]['uploaded'] is False


def test_upload_chunk_leaves_a_clip_the_encoder_skipped(tmp_path, recorded, monkeypatch):
    """The encoder skips a chunk that is still being written; the raw file
    must not go up in its place."""
    cfg, night = _build(tmp_path)
    _ready(monkeypatch)
    monkeypatch.setattr(archive_upload.encoder, 'process_chunk', lambda path, cfg: False)
    assert archive_upload.upload_chunk(CAM, NIGHT, night / CLIP_C, cfg) is False
    assert recorded == []


def test_upload_chunk_does_not_resend_an_uploaded_clip(tmp_path, recorded, monkeypatch):
    """A lock or a play-to-keep on a clip dawn already shipped used to send it
    again."""
    cfg, night = _build(tmp_path)
    _ready(monkeypatch)
    monkeypatch.setattr(archive_upload.encoder, 'process_chunk', lambda path, cfg: True)
    assert archive_upload.upload_chunk(CAM, NIGHT, night / CLIP_D, cfg) is True
    assert recorded == []


def test_upload_chunk_rechecks_after_the_encode(tmp_path, recorded, monkeypatch):
    """Another uploader shipped the clip while this one was encoding it."""
    cfg, night = _build(tmp_path)
    _ready(monkeypatch)

    def encode_while_dawn_uploads(path, cfg):
        flags_manager.mark_reencoded(CAM, NIGHT, path.name, cfg)
        flags_manager.mark_chunk_uploaded(CAM, NIGHT, path.name, cfg)

    monkeypatch.setattr(archive_upload.encoder, 'process_chunk', encode_while_dawn_uploads)
    assert archive_upload.upload_chunk(CAM, NIGHT, night / CLIP_C, cfg) is True
    assert recorded == []


def test_upload_chunk_leaves_a_held_night_to_its_holder(tmp_path, recorded, monkeypatch):
    """Dawn or the daemon holds the night: the clip is encoded and queued, not
    sent alongside them, and the next pass ships it."""
    cfg, night = _build(tmp_path)
    _ready(monkeypatch)
    monkeypatch.setattr(archive_upload.encoder, 'process_chunk',
                        lambda path, cfg: flags_manager.mark_reencoded(CAM, NIGHT, path.name, cfg))
    holder = archive_upload._NightLock(CAM, NIGHT, archive_upload._upload_state_dir())
    assert holder.acquire()
    try:
        assert archive_upload.upload_chunk(CAM, NIGHT, night / CLIP_C, cfg) is True
    finally:
        holder.release()
    assert recorded == []
    assert _state(night)['chunks'][CLIP_C]['uploaded'] is False
    _uploader(cfg).cycle()
    assert f'meteors/{CLIP_C}' in recorded


def test_upload_chunk_force_resends_an_uploaded_clip(tmp_path, recorded, monkeypatch):
    """process_clip ?force=1: the flag says uploaded but the archive copy is
    gone."""
    cfg, night = _build(tmp_path)
    _ready(monkeypatch)
    monkeypatch.setattr(archive_upload.encoder, 'process_chunk', lambda path, cfg: True)
    assert archive_upload.upload_chunk(CAM, NIGHT, night / CLIP_D, cfg, force=True) is True
    assert recorded[0] == f'meteors/{CLIP_D}'


def test_no_ingest_settings_means_no_scan_and_one_warning(tmp_path, recorded, monkeypatch, caplog):
    cfg, _night = _build(tmp_path)
    monkeypatch.setattr(archive_upload, '_INGEST_MISSING_WARNED', False)
    monkeypatch.setattr(_Uploader, '_scan_all', lambda self: pytest.fail('scanned'))
    with caplog.at_level('WARNING', logger=archive_upload.logger.name):
        for _ in range(3):
            up = _Uploader(cfg)
            up.ingest_base_url = up.ingest_key = up.ingest_host_key = None
            assert up.cycle() == 0
            assert up.cycle_night(CAM, NIGHT) == 0
    warnings = [r for r in caplog.records if 'ingest not configured' in r.getMessage()]
    assert len(warnings) == 1
    assert recorded == []


def test_delete_chunk_clears_flags_and_resends_state(tmp_path, recorded, monkeypatch):
    cfg, night = _build(tmp_path)
    _ready(monkeypatch)
    assert archive_upload.delete_chunk(CAM, NIGHT, CLIP_D, cfg) is True
    chunk = _state(night)['chunks'][CLIP_D]
    assert chunk['uploaded'] is False and chunk['stack_uploaded'] is False
    assert recorded == ['state.json']


class _StopAfter:
    """A stop event that sets itself after ``n`` waits and records them."""

    def __init__(self, n: int) -> None:
        self._event = threading.Event()
        self._left = n
        self.waits: list[float] = []

    def is_set(self) -> bool:
        return self._event.is_set()

    def set(self) -> None:
        self._event.set()

    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(timeout if timeout is not None else -1)
        self._left -= 1
        if self._left <= 0:
            self._event.set()
        return self._event.is_set()


def test_run_upload_loop_holds_for_the_stagger_then_cycles(tmp_path, monkeypatch):
    cfg, _night = _build(tmp_path, interval_minutes=1)
    holds = iter([120.0, 0.0])
    monkeypatch.setattr(archive_upload, 'stagger_hold_s', lambda *a, **k: next(holds, 0.0))
    cycles: list[int] = []
    monkeypatch.setattr(_Uploader, 'cycle',
                        lambda self, *a, **k: cycles.append(1) or 0)
    stop = _StopAfter(2)
    archive_upload.run_upload_loop(cfg, stop)  # type: ignore[arg-type]
    assert stop.waits[0] == 120.0
    assert cycles == [1]


def test_run_upload_loop_survives_a_failing_cycle(tmp_path, monkeypatch):
    cfg, _night = _build(tmp_path, interval_minutes=1)
    monkeypatch.setattr(archive_upload, 'stagger_hold_s', lambda *a, **k: 0.0)
    calls: list[int] = []

    def flaky(self, *a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise OSError('disk hiccup')
        return 0

    monkeypatch.setattr(_Uploader, 'cycle', flaky)
    # 12 waits of 5 s per 60 s interval: the second cycle runs after the first
    # interval, then the loop stops during the second.
    stop = _StopAfter(13)
    archive_upload.run_upload_loop(cfg, stop)  # type: ignore[arg-type]
    assert len(calls) == 2


def test_run_upload_loop_returns_when_disabled(tmp_path, monkeypatch):
    cfg, _night = _build(tmp_path, enabled=False)
    monkeypatch.setattr(_Uploader, 'cycle', lambda self, *a, **k: pytest.fail('ran'))
    archive_upload.run_upload_loop(cfg, _StopAfter(1))  # type: ignore[arg-type]
