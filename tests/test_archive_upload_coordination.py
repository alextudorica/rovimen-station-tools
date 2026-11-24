"""Coordination between the uploaders of one station (archive_upload.py).

The archive-upload daemon and dawn's passes run in separate processes over the
same nights. On gmnro01 (Ghirdoveni), night 20261023, they sent 283 files
(2.8 GB) twice, and a power loss at 06:10 re-sent 113 files whose upload marks
were still queued in memory. A pass now holds a per-camera-night lock while it
sends that night, and journals each confirmed upload before the batched
state.json write.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

import archive_upload
import flags_manager
from archive_upload import _MarkJournal, _NightLock, _Uploader

pytestmark = pytest.mark.usefixtures('ingest_ready')

CAM, NIGHT, OTHER = 'RO000A', '20261023', '20261022'


def _clip(i: int, night: str = NIGHT) -> str:
    return f'{CAM}_{night}_21{i:04d}_color.mkv'


def _night(capture: Path, night: str, n: int) -> list[str]:
    d = capture / CAM / night
    d.mkdir(parents=True)
    lock = {'lock_type': 'detection'}
    state = flags_manager._empty_state(CAM, night)
    names = [_clip(i, night) for i in range(n)]
    for name in names:
        (d / name).write_bytes(b'mkv')
        state['chunks'][name] = {'ready': True, 'stacked': True, 'reencoded': True,
                                 'lock': lock, 'uploaded': False, 'stack_uploaded': False}
    (d / 'state.json').write_text(json.dumps(state))
    return names


@pytest.fixture
def station(tmp_path) -> tuple[dict, Path]:
    capture = tmp_path / 'color_capture'
    cfg = {'videocapture_path': str(capture), 'stations': {CAM: {}},
           'archive': {'enabled': True, 'upload_rms': False}}
    return cfg, capture


@pytest.fixture
def posts(monkeypatch) -> list[str]:
    sent: list[str] = []
    marker = f'/blob/{CAM}/'

    def fake_post(url, key, data, timeout=180):
        sent.append(url.split(marker, 1)[1])
        return 200

    monkeypatch.setattr(archive_upload, 'post_blob_status', fake_post)
    return sent


def _coord_root() -> Path:
    return archive_upload._upload_state_dir()


def _uploaded(cfg: dict, night: str = NIGHT) -> set[str]:
    state = flags_manager.load(CAM, night, cfg)
    return {n for n, c in state['chunks'].items() if c.get('uploaded')}


# ---------------------------------------------------------------------------
# Per-night lock
# ---------------------------------------------------------------------------

def test_the_lock_excludes_a_second_holder_in_the_same_process(tmp_path):
    a, b = _NightLock(CAM, NIGHT, tmp_path), _NightLock(CAM, NIGHT, tmp_path)
    assert a.acquire()
    assert not b.acquire()
    a.release()
    assert b.acquire()
    b.release()


def test_the_daemon_leaves_a_held_night_to_its_holder(station, posts, caplog):
    cfg, capture = station
    _night(capture, NIGHT, 2)
    older = _night(capture, OTHER, 1)
    other = _NightLock(CAM, NIGHT, _coord_root())
    assert other.acquire()  # dawn is uploading tonight
    try:
        with caplog.at_level('INFO', logger='archive_upload'):
            _Uploader(cfg).cycle()
    finally:
        other.release()
    assert [p for p in posts if p.startswith(NIGHT)] == []
    assert f'{OTHER}/meteors/{older[0]}' in posts
    assert 'Another uploader holds this night' in caplog.text


def test_dawn_gives_up_on_a_held_night_after_its_wait(station, posts, monkeypatch):
    cfg, capture = station
    _night(capture, NIGHT, 2)
    clock = {'t': 0.0}
    monkeypatch.setattr(archive_upload.time, 'monotonic', lambda: clock['t'])
    monkeypatch.setattr(archive_upload.time, 'sleep',
                        lambda s: clock.__setitem__('t', clock['t'] + s))
    other = _NightLock(CAM, NIGHT, _coord_root())
    assert other.acquire()
    try:
        assert _Uploader(cfg).cycle_night(CAM, NIGHT) == 0
    finally:
        other.release()
    assert posts == []
    assert clock['t'] >= archive_upload._DAWN_LOCK_WAIT_S


def test_dawn_takes_the_night_once_the_holder_lets_go(station, posts, monkeypatch):
    cfg, capture = station
    names = _night(capture, NIGHT, 2)
    other = _NightLock(CAM, NIGHT, _coord_root())
    assert other.acquire()
    monkeypatch.setattr(archive_upload.time, 'sleep', lambda s: other.release())
    assert _Uploader(cfg).cycle_night(CAM, NIGHT) == 2
    assert sorted(p for p in posts if p.endswith('.mkv')) == sorted(f'{NIGHT}/meteors/{n}' for n in names)


def test_the_next_holder_sees_the_first_holders_uploads(station, posts):
    """Marks are in state.json by the time the night is released, so a pass
    that takes it next sends nothing again."""
    cfg, capture = station
    _night(capture, NIGHT, 3)
    up = _Uploader(cfg)
    assert up.cycle_night(CAM, NIGHT) == 3
    lock = _NightLock(CAM, NIGHT, _coord_root())
    assert lock.acquire()  # released at the end of the pass
    lock.release()
    posts.clear()
    assert _Uploader(cfg).cycle() == 0
    assert posts == []


def test_concurrent_passes_send_each_file_once(station, posts, monkeypatch):
    cfg, capture = station
    names = _night(capture, NIGHT, 6)
    monkeypatch.setattr(archive_upload, '_DAWN_LOCK_WAIT_S', 0.0)  # no real-time wait
    gate = threading.Barrier(2)

    def run(fn):
        gate.wait()
        fn()

    threads = [threading.Thread(target=run, args=(_Uploader(cfg).cycle,)),
               threading.Thread(target=run, args=(lambda: _Uploader(cfg).cycle_night(CAM, NIGHT),))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    clip_posts = [p for p in posts if p.endswith('_color.mkv')]
    assert sorted(clip_posts) == sorted(f'{NIGHT}/meteors/{n}' for n in names)


# ---------------------------------------------------------------------------
# Crash-safe marks
# ---------------------------------------------------------------------------

class _PowerLoss(BaseException):
    """Stops the process where it stands: no finally block gets to save."""


def test_marks_survive_a_crash_and_nothing_is_sent_twice(station, monkeypatch):
    cfg, capture = station
    names = _night(capture, NIGHT, 5)
    sent: list[str] = []
    power = {'lost': False}

    def post(url, key, data, timeout=180):
        if power['lost'] is False and len(sent) == 3:
            power['lost'] = True
            raise _PowerLoss
        sent.append(url.rsplit('/', 1)[1])
        return 200

    real_batch = flags_manager.mark_uploaded_batch

    def batch(*a, **k):
        if power['lost']:
            raise _PowerLoss  # the finally-block flush never runs either
        return real_batch(*a, **k)

    monkeypatch.setattr(archive_upload, 'post_blob_status', post)
    monkeypatch.setattr(flags_manager, 'mark_uploaded_batch', batch)
    with pytest.raises(_PowerLoss):
        _Uploader(cfg).cycle()
    first = list(sent)
    assert len(first) == 3
    assert _uploaded(cfg) == set()  # nothing reached state.json
    journal = _MarkJournal(CAM, NIGHT, _coord_root())
    assert [m[1] for m in journal.read()] == first

    # After the reboot (the kernel dropped the lock with the process).
    power['lost'] = None
    sent.clear()
    assert _Uploader(cfg).cycle_night(CAM, NIGHT) == 2
    assert set(sent).isdisjoint(first)
    assert _uploaded(cfg) == set(names)
    assert not journal.path.exists()


def test_the_journal_is_cleared_once_its_marks_are_in_state_json(station, posts):
    cfg, capture = station
    _night(capture, NIGHT, 2)
    _Uploader(cfg).cycle_night(CAM, NIGHT)
    assert not _MarkJournal(CAM, NIGHT, _coord_root()).path.exists()


def test_a_torn_last_line_is_skipped(tmp_path):
    journal = _MarkJournal(CAM, NIGHT, tmp_path)
    journal.append((flags_manager.MARK_CHUNK, _clip(1), True))
    journal.append((flags_manager.MARK_PRODUCT, 'timelapse', [10, 20]))
    with open(journal.path, 'a') as fh:
        fh.write('["chunk","RO000A_2026')  # power lost mid-write
    assert journal.read() == [(flags_manager.MARK_CHUNK, _clip(1), True),
                              (flags_manager.MARK_PRODUCT, 'timelapse', [10, 20])]


def test_marks_still_batch_into_few_state_json_saves(station, posts, monkeypatch):
    """The journal keeps #1291's batching: 120 uploads, a handful of saves."""
    cfg, capture = station
    _night(capture, NIGHT, 120)
    saves: list[int] = []
    real = flags_manager.mark_uploaded_batch

    def counting(cam, date, marks, c):
        saves.append(len(marks))
        return real(cam, date, marks, c)

    monkeypatch.setattr(flags_manager, 'mark_uploaded_batch', counting)
    assert _Uploader(cfg).cycle_night(CAM, NIGHT) == 120
    assert len(saves) <= 120 // archive_upload._MARK_FLUSH_EVERY + 2
    assert len(_uploaded(cfg)) == 120


# ---------------------------------------------------------------------------
# The daemon follows config.json
# ---------------------------------------------------------------------------

class _StopAfter:
    """A stop event that sets itself after ``n`` waits; ``on_wait`` runs at each."""

    def __init__(self, n: int, on_wait=None) -> None:
        self._event = threading.Event()
        self._left = n
        self._on_wait = on_wait

    def is_set(self) -> bool:
        return self._event.is_set()

    def set(self) -> None:
        self._event.set()

    def wait(self, timeout: float | None = None) -> bool:
        if self._on_wait is not None:
            self._on_wait()
        self._left -= 1
        if self._left <= 0:
            self._event.set()
        return self._event.is_set()


def test_the_daemon_picks_up_a_camera_added_to_config_json(tmp_path, monkeypatch):
    path = tmp_path / 'config.json'
    cfg = {'videocapture_path': str(tmp_path / 'cc'), 'stations': {'RO000Z': {}},
           'archive': {'enabled': True, 'interval_minutes': 1}}
    path.write_text(json.dumps(cfg))
    monkeypatch.setattr(archive_upload, 'stagger_hold_s', lambda *a, **k: 0.0)
    seen: list[list[str]] = []
    monkeypatch.setattr(_Uploader, 'cycle',
                        lambda self, *a, **k: seen.append(sorted(self.stations)) or 0)

    def add_camera() -> None:
        if len(seen) == 1 and 'RO0013' not in path.read_text():
            new = dict(cfg, stations={'RO000Z': {}, 'RO0013': {}})
            tmp = path.with_name('config.json.tmp')
            tmp.write_text(json.dumps(new))
            tmp.replace(path)  # what config_patch.py does

    # 12 waits of 5 s per 60 s interval: two cycles, then stop.
    archive_upload.run_upload_loop(cfg, _StopAfter(13, add_camera),  # type: ignore[arg-type]
                                   config_path=path)
    assert seen == [['RO000Z'], ['RO000Z', 'RO0013']]


def test_an_unreadable_config_keeps_the_old_one(tmp_path):
    path = tmp_path / 'config.json'
    path.write_text('{"stations": {"A": {}}}')
    watch = archive_upload._ConfigWatch(path)
    assert watch.changed() is None
    path.write_text('{"stations": ')  # torn
    assert watch.changed() is None
    path.write_text('{"stations": {"A": {}, "B": {}}}')
    assert watch.changed() == {'stations': {'A': {}, 'B': {}}}
    assert watch.changed() is None
