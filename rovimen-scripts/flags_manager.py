#!/usr/bin/env python3
"""flags_manager.py — Central state.json manager for the ROVIMEN pipeline.

Replaces all sidecar files (.ready, .stacked, .reencoded, .locked)
with a single JSON state file per night per station.

Location: {videocapture_path}/{station_id}/{YYYYMMDD}/state.json

All writes are atomic (write to .tmp then os.replace). Per-night locks
prevent concurrent corruption: threading.Lock for same-process thread safety,
fcntl.flock for cross-process safety (color_capture, detection_lock, dawn_process,
station_api all run as separate processes). A state.json that exists but cannot
be read is never written over, and readers that delete data treat its night as
locked (see _load_for_write and night_unreadable).

Schema
------
{
  "station": "RO000H",
  "date": "20260322",
  "chunks": {
    "RO000H_20260322_210000_color.mkv": {
      "ready": true,
      "stacked": false,
      "reencoded": false,
      "lock": null           // or {"lock_type":..., "detection_time":..., "meteor_time":...}
    }
  },
  "rms_complete": false,
  "morning_done": false,     // true once the full morning sequence has completed
  "stage_times": {           // UTC, "YYYY-MM-DDTHH:MM:SSZ"; each key appears when it happens
    "rms_complete": ...,     //   first time RMS was marked complete
    "morning_done": ...,     //   first time the morning sequence finished
    "first_upload": ...,     //   first colour clip confirmed on the archive
    "last_upload": ...       //   latest one
  }
}
"""

from __future__ import annotations

import fcntl
import json
import time
import logging
import os
import threading
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Callable, Iterable, Mapping, Tuple

import rovimen_config
import rovimen_lock

logger = logging.getLogger(__name__)
_unreadable_warned: set[str] = set()

# Per-night threading locks to prevent concurrent state corruption.
# Keyed by "station_id/date".
_locks: dict[str, threading.Lock] = {}
_locks_meta = threading.Lock()


def _get_lock(station_id: str, date_str: str) -> threading.Lock:
    key = f'{station_id}/{date_str}'
    with _locks_meta:
        if key not in _locks:
            # Evict stale entries to prevent unbounded growth in long-running daemons.
            # Clearing is safe: each returned Lock object is independent; callers
            # holding one are unaffected.  Cross-process safety comes from fcntl.
            if len(_locks) > 60:
                _locks.clear()
            _locks[key] = threading.Lock()
        return _locks[key]


@contextmanager
def _lock(station_id: str, date_str: str, cfg: dict):
    """Acquire thread + file lock for a load→modify→save sequence.

    threading.Lock prevents races between threads in the same process.
    fcntl.flock prevents races between separate processes (color_capture,
    detection_lock, dawn_process, station_api all run independently).

    Lock file lives in /tmp so it never needs the night dir to exist first.
    """
    with _get_lock(station_id, date_str):
        lock_path = Path(f'/tmp/rovimen_state_{station_id}_{date_str}.lock')
        with open(lock_path, 'w') as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            yield


# Where state.json goes when the configured capture root is unusable. Not a
# preference -- the fallback only ever applies when the real root cannot be
# written to, see _state_root.
_FALLBACK_STATE_ROOT = Path.home() / 'rovimen_scripts' / 'upload_state'

_fallback_warned: set[str] = set()


def _state_root(cfg: dict) -> Path:
    """Directory tree holding the per-night state.json files.

    Normally the colour-capture root, so a night's upload flags sit beside
    the video they describe. When that root is NOT writable, the flags move
    to a local fallback instead.

    That fallback exists because the capture disk is the fleet's most common
    hardware casualty and the failure it caused was out of all proportion:
    gmn0004's colour disk died, /mnt/data reverted to a root-owned stub on
    /, and every attempt to record an upload raised PermissionError out of
    ``save()`` -- which aborted the ENTIRE archive-upload cycle, for RMS
    plots and meteor clips that have nothing to do with colour video. The
    station kept POSTing the same files and dying before recording them,
    every 20 minutes, for five days (2026-08-07..12) and again for four
    (2026-09-17..21). Keeping the flags on a disk that is always present
    decouples "the colour disk is gone" from "this station stops archiving
    altogether"; the cost is that flags written while the capture root is
    down are not found again once it returns, which re-uploads at most one
    night's already-archived files.
    """
    root = rovimen_config.capture_root(cfg)
    if os.access(root, os.W_OK):
        return root
    # The root itself may legitimately not exist yet on a fresh station --
    # that is creatable, and not the failure this guards against.
    try:
        root.mkdir(parents=True, exist_ok=True)
        if os.access(root, os.W_OK):
            return root
    except OSError:
        pass
    key = str(root)
    if key not in _fallback_warned:
        _fallback_warned.add(key)
        logger.error('capture root %s is not writable — upload state falls back to %s '
                     '(check that the capture disk is mounted)', root, _FALLBACK_STATE_ROOT)
    return _FALLBACK_STATE_ROOT


def _state_path(station_id: str, date_str: str, cfg: dict) -> Path:
    return _state_root(cfg) / station_id / date_str / 'state.json'


def _empty_state(station_id: str, date_str: str) -> dict:
    return {
        'station': station_id,
        'date': date_str,
        'chunks': {},
        'rms_complete': False,
        'morning_done': False,
        'timelapse_done': False,
        'timelapse_deferred': False,
        'timelapse_uploaded': False,
        'night_stack_uploaded': False,
        'rms_uploaded': {},
        'ffthumbs_uploaded': {},
    }


def _stamp(state: dict, stage: str, keep_first: bool = True) -> None:
    """Record when *stage* happened in the night's ``stage_times``.

    The dashboard's night timeline draws the pipeline from these. A stage
    keeps its first time (a later reprocess must not stretch the night it
    describes), except ``last_upload``, which moves with every upload."""
    times = state.setdefault('stage_times', {})
    if keep_first and stage in times:
        return
    times[stage] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def _stamp_upload(state: dict) -> None:
    _stamp(state, 'first_upload')
    _stamp(state, 'last_upload', keep_first=False)


def _empty_chunk() -> dict:
    return {
        'ready': False, 'stacked': False, 'reencoded': False, 'lock': None,
        'uploaded': False, 'stack_uploaded': False,
    }


# ---------------------------------------------------------------------------
# Per-scan read cache
# ---------------------------------------------------------------------------

# Nights held at once. Scans walk one night at a time, so a handful covers
# every access pattern; the bound matters because a settled night's parsed
# state.json (~2000 chunks) costs a few MB, and gmnde00 keeps a year of them.
_SCAN_CACHE_MAX_NIGHTS = 8

# (st_ino, st_mtime_ns, st_size): save() replaces the file via os.replace, so
# every write -- ours or another process's -- lands on a fresh inode, which
# makes this signature change even when size and mtime tick happen to match.
# typing.Tuple, not tuple[...]: this alias is evaluated at import, and the
# RMS venvs on gmnro05/08 are Python 3.8, where tuple[...] raises TypeError
# and takes every station script that imports this module down with it.
_Sig = Tuple[int, int, int]

_scan_cache: ContextVar['OrderedDict[Path, tuple[_Sig, dict]] | None'] = ContextVar(
    'flags_manager_scan_cache', default=None)


@contextmanager
def scan_cache():
    """Parse each night's state.json at most once while the block runs.

    A scan asks is_chunk_uploaded / is_chunk_stack_uploaded / the lock and
    reencoded checks once per chunk, and each used to json.loads that night's
    whole state.json again -- O(chunks x state.json size) per pass, which kept
    gmnde00's uploader at half a core around the clock. Inside this block the
    read-only accessors share one parsed copy per night, re-validated against a
    stat() on every access, so a write by any process (a new inode) is picked
    up at the next read. Writers (the mark_* family) always re-read from disk
    under the lock and never see cached data. Nesting is a no-op; the cache is
    context-local, so threads started inside the block run uncached.
    """
    if _scan_cache.get() is not None:
        yield
        return
    token = _scan_cache.set(OrderedDict())
    try:
        yield
    finally:
        _scan_cache.reset(token)


def _file_sig(path: Path) -> _Sig | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_ino, st.st_mtime_ns, st.st_size)


def peek(station_id: str, date_str: str, cfg: dict) -> dict:
    """Read-only view of a night's state -- callers must NOT mutate the result.

    Identical to load() outside a scan_cache() block. Inside one it returns the
    shared parsed copy while the file's signature is unchanged.
    """
    cache = _scan_cache.get()
    if cache is None:
        return load(station_id, date_str, cfg)
    path = _state_path(station_id, date_str, cfg)
    sig = _file_sig(path)
    hit = cache.get(path)
    if hit is not None and sig is not None and hit[0] == sig:
        cache.move_to_end(path)
        return hit[1]
    # Stat before read: if the file changes in between, the stored signature is
    # the older one and the next access simply re-reads -- never the reverse.
    state = load(station_id, date_str, cfg)
    if sig is None:
        cache.pop(path, None)
        return state
    cache[path] = (sig, state)
    cache.move_to_end(path)
    while len(cache) > _SCAN_CACHE_MAX_NIGHTS:
        cache.popitem(last=False)
    return state


def _invalidate(path: Path) -> None:
    cache = _scan_cache.get()
    if cache is not None:
        cache.pop(path, None)


# ---------------------------------------------------------------------------
# Core load / save
# ---------------------------------------------------------------------------

class _UnreadableState(dict):
    """The empty state load() returns for a state.json that exists but cannot
    be read or parsed. It reads like an empty night, so the uploader and the
    station API carry on; whatever deletes data asks is_unreadable() and keeps
    the night instead, and no writer ever saves it (_load_for_write)."""


def is_unreadable(state: Mapping) -> bool:
    """Whether *state* is load()'s stand-in for an unreadable state.json."""
    return isinstance(state, _UnreadableState)


def night_unreadable(station_id: str, date_str: str, cfg: dict) -> bool:
    """Whether the night's state.json exists but cannot be read or parsed.

    Readers that delete (the janitor) must then treat the night as locked:
    its locks and upload flags are unknown, not absent."""
    return is_unreadable(peek(station_id, date_str, cfg))


# A night dir whose state.json is missing (FileNotFoundError), or whose path
# runs through a file (NotADirectoryError), simply has no state yet.
_MISSING = (FileNotFoundError, NotADirectoryError)


def _read_state(path: Path) -> dict:
    """Parse state.json. Raises an OSError when it cannot be read (one of
    _MISSING when it is not there) and a ValueError when its bytes are not a
    JSON object (JSONDecodeError and UnicodeDecodeError are ValueErrors)."""
    state = json.loads(path.read_bytes())
    if not isinstance(state, dict):
        raise ValueError('not a JSON object')
    return state


def load(station_id: str, date_str: str, cfg: dict) -> dict:
    """Load state.json. Returns an empty state dict if the file is missing,
    and the same empty state marked unreadable (is_unreadable) if it exists
    but cannot be read or parsed."""
    path = _state_path(station_id, date_str, cfg)
    try:
        return _read_state(path)
    except _MISSING:
        return _empty_state(station_id, date_str)
    except (OSError, ValueError) as exc:
        key = f'{station_id}/{date_str}'
        if key not in _unreadable_warned:
            _unreadable_warned.add(key)
            logger.warning('[%s/%s] state.json unreadable (%s) — using empty state',
                           station_id, date_str, exc)
        return _UnreadableState(_empty_state(station_id, date_str))


def _load_for_write(station_id: str, date_str: str, cfg: dict) -> dict | None:
    """The night's state for a writer's load-modify-save (under _lock), or
    None when saving would destroy what the file holds.

    A missing file starts empty. A file that cannot be read returns None and
    the write is skipped: saving an empty state plus one change over a
    state.json the disk could not read for a moment wiped that night's
    locks, upload flags and morning_done for good (gmnro01 (Ghirdoveni)'s USB
    disk returned EIO on 2026-11-09). A skipped upload flag costs a re-upload
    of an archived file. A file that reads but does not parse cannot be
    repaired: it is kept as ``state.json.bad-<UTC time>`` and the night
    starts empty (None if it cannot be moved).
    """
    path = _state_path(station_id, date_str, cfg)
    key = f'{station_id}/{date_str}'
    try:
        return _read_state(path)
    except _MISSING:
        return _empty_state(station_id, date_str)
    except ValueError as exc:
        bad = path.with_name('state.json.bad-' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()))
        try:
            os.replace(path, bad)
        except OSError as mv_exc:
            if f'{key}/write' not in _unreadable_warned:
                _unreadable_warned.add(f'{key}/write')
                logger.error('[%s/%s] state.json is corrupt (%s) and could not be moved aside '
                             '(%s) — not writing to it', station_id, date_str, exc, mv_exc)
            return None
        _invalidate(path)
        logger.error('[%s/%s] state.json is corrupt (%s): kept as %s, the night starts '
                     'again from an empty state', station_id, date_str, exc, bad.name)
        return _empty_state(station_id, date_str)
    except OSError as exc:
        if f'{key}/write' not in _unreadable_warned:
            _unreadable_warned.add(f'{key}/write')
            logger.error('[%s/%s] state.json unreadable (%s) — not writing to it, so the '
                         'night keeps its locks and flags; changes to it are lost until '
                         'it reads again', station_id, date_str, exc)
        return None


def save(state: dict, station_id: str, date_str: str, cfg: dict) -> bool:
    """Atomically write state.json (tmp file + os.replace). True if written.

    A failure is logged, never raised (see below); the return value lets a
    caller avoid logging a success that never reached the disk.
    """
    path = _state_path(station_id, date_str, cfg)
    tmp = path.with_name('state.json.tmp')
    try:
        # Inside the try: the state dir lives under videocapture_path, so a
        # missing or unwritable capture disk fails here rather than at the
        # write below. Left outside, it raised a bare PermissionError that
        # aborted the caller's whole upload cycle with no logged diagnostic.
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(state, indent=2))
        os.replace(tmp, path)
        _invalidate(path)
        return True
    except OSError as exc:
        _invalidate(path)
        # Logged, not raised. The callers are one-line "record that this file
        # uploaded" calls inside long sweeps; letting the exception out
        # unwinds the whole sweep and stops the station archiving anything
        # (see _state_root for what that cost twice). A lost flag costs one
        # redundant re-upload of a file that is already safely in the
        # archive, which is the cheaper of the two failures by a wide
        # margin.
        key = f'{station_id}/{date_str}'
        if key not in _unreadable_warned:
            _unreadable_warned.add(key)
            logger.error('[%s/%s] state.json write failed — this night\'s upload flags are '
                         'not being recorded, so already-archived files may re-upload: %s',
                         station_id, date_str, exc)
        return False


# ---------------------------------------------------------------------------
# Chunk-level reads with the legacy sidecar fallback
# ---------------------------------------------------------------------------

def is_chunk_locked(mkv: Path, station_id: str, date_str: str, cfg: dict) -> bool:
    """Whether a chunk is locked: state.json first, the legacy ``.locked``
    sidecar when the night's state does not list the chunk."""
    try:
        chunk = peek(station_id, date_str, cfg).get('chunks', {}).get(mkv.name)
        if chunk is not None:
            return chunk.get('lock') is not None
    except Exception:
        pass
    return rovimen_lock.is_locked(mkv)


def is_chunk_reencoded(mkv: Path, station_id: str, date_str: str, cfg: dict) -> bool:
    """Whether a chunk is re-encoded: state.json first, the legacy
    ``.reencoded`` sidecar when the night's state does not list the chunk."""
    try:
        chunk = peek(station_id, date_str, cfg).get('chunks', {}).get(mkv.name)
        if chunk is not None:
            return bool(chunk.get('reencoded', False))
    except Exception:
        pass
    return Path(str(mkv) + '.reencoded').exists()


# ---------------------------------------------------------------------------
# Chunk-level updates
# ---------------------------------------------------------------------------

def edit_state(station_id: str, date_str: str, cfg: dict,
               edit: Callable[[dict], object]) -> bool:
    """Apply *edit* to the night's state.json under its lock, then save.

    *edit* mutates the loaded state in place; returning ``False`` means
    nothing changed and skips the write. Returns True only when a write
    happened and succeeded. For read-modify-write updates the ``mark_*``
    helpers do not cover (dawn's EON sources and retry counters, the
    encoder's per-chunk encode record).
    """
    with _lock(station_id, date_str, cfg):
        state = _load_for_write(station_id, date_str, cfg)
        if state is None:
            return False
        if edit(state) is False:
            return False
        return save(state, station_id, date_str, cfg)


def update_chunk(station_id: str, date_str: str, chunk_name: str, cfg: dict,
                 set_fields: Mapping[str, object] | None = None,
                 drop_fields: Iterable[str] = ()) -> bool:
    """Set *set_fields* and remove *drop_fields* on one chunk, in one write."""
    def _edit(state: dict) -> None:
        entry = state['chunks'].setdefault(chunk_name, _empty_chunk())
        entry.update(set_fields or {})
        for key in drop_fields:
            entry.pop(key, None)

    return edit_state(station_id, date_str, cfg, _edit)


def mark_ready(station_id: str, date_str: str, chunk_name: str, cfg: dict) -> None:
    """Mark a chunk as ready (ffmpeg segment closed)."""
    with _lock(station_id, date_str, cfg):
        state = _load_for_write(station_id, date_str, cfg)
        if state is None:
            return None
        state['chunks'].setdefault(chunk_name, _empty_chunk())['ready'] = True
        save(state, station_id, date_str, cfg)
    logger.debug('[%s/%s] Ready: %s', station_id, date_str, chunk_name)


def mark_ready_batch(station_id: str, date_str: str, chunk_names: list[str], cfg: dict) -> None:
    """Mark multiple chunks as ready in a single load-modify-save cycle."""
    if not chunk_names:
        return
    with _lock(station_id, date_str, cfg):
        state = _load_for_write(station_id, date_str, cfg)
        if state is None:
            return None
        for name in chunk_names:
            state['chunks'].setdefault(name, _empty_chunk())['ready'] = True
        save(state, station_id, date_str, cfg)
    logger.debug('[%s/%s] Batch-ready: %d chunk(s)', station_id, date_str, len(chunk_names))


def lock_chunk(station_id: str, date_str: str, chunk_name: str,
               lock_info: dict, cfg: dict, *, ready: bool = True) -> None:
    """Set the lock field on a chunk (real-time detection lock).

    ``ready=False`` leaves the ready flag as it is: for a segment colour
    capture may still be writing, which its watcher marks ready on close."""
    with _lock(station_id, date_str, cfg):
        state = _load_for_write(station_id, date_str, cfg)
        if state is None:
            return None
        chunk = state['chunks'].setdefault(chunk_name, _empty_chunk())
        if ready:
            chunk['ready'] = True  # if not already marked
        chunk['lock'] = lock_info
        save(state, station_id, date_str, cfg)
    logger.debug('[%s/%s] Locked chunk: %s', station_id, date_str, chunk_name)


def lock_chunk_if_unlocked(station_id: str, date_str: str, chunk_name: str,
                           lock_info: dict, cfg: dict, *, ready: bool = True) -> dict | None:
    """Lock a chunk unless it already is; return the existing lock, or None
    if this call set it (or could not: an unreadable state.json is never
    written, see _load_for_write). A manual lock on a detection-locked chunk
    must not replace its detection and meteor times. ``ready`` as in lock_chunk."""
    with _lock(station_id, date_str, cfg):
        state = _load_for_write(station_id, date_str, cfg)
        if state is None:
            return None
        chunk = state['chunks'].setdefault(chunk_name, _empty_chunk())
        if chunk.get('lock') is not None:
            return chunk['lock']
        if ready:
            chunk['ready'] = True
        chunk['lock'] = lock_info
        save(state, station_id, date_str, cfg)
    logger.debug('[%s/%s] Locked chunk: %s', station_id, date_str, chunk_name)
    return None


def unlock_chunk(station_id: str, date_str: str, chunk_name: str, cfg: dict) -> None:
    """Clear the lock field on a chunk (false positive removal)."""
    with _lock(station_id, date_str, cfg):
        state = _load_for_write(station_id, date_str, cfg)
        if state is None:
            return None
        if chunk_name in state['chunks']:
            state['chunks'][chunk_name]['lock'] = None
            save(state, station_id, date_str, cfg)
    logger.debug('[%s/%s] Unlocked chunk: %s', station_id, date_str, chunk_name)


def relock_chunk(station_id: str, date_str: str, chunk_name: str,
                 lock_info: dict, cfg: dict) -> None:
    """Replace the lock field with updated metadata (EON sub-second precision pass)."""
    with _lock(station_id, date_str, cfg):
        state = _load_for_write(station_id, date_str, cfg)
        if state is None:
            return None
        chunk = state['chunks'].setdefault(chunk_name, _empty_chunk())
        chunk['ready'] = True
        chunk['lock'] = lock_info
        save(state, station_id, date_str, cfg)
    logger.debug('[%s/%s] Relocked chunk: %s', station_id, date_str, chunk_name)


def apply_lock_changes(station_id: str, date_str: str, cfg: dict,
                       relocks: Mapping[str, dict] | None = None,
                       unlocks: Iterable[str] = ()) -> bool:
    """Apply many relock_chunk / unlock_chunk changes in one load-modify-save.

    Each change has exactly the per-chunk helper's effect, relocks first in
    mapping order, then unlocks. The EON pass used to rewrite state.json once
    per chunk, ~300 locked rewrites a camera-night; on gmnro09 (Sibiciu),
    whose state.json shares a USB HDD with capture, each cost 3-4 s and 105
    of 348 took 8 s or more (2026-10-26). Returns whether the write reached
    the disk (see save()); no changes is True with no write.
    """
    relocks = relocks or {}
    unlocks = list(unlocks)
    if not relocks and not unlocks:
        return True
    with _lock(station_id, date_str, cfg):
        state = _load_for_write(station_id, date_str, cfg)
        if state is None:
            return False
        for name, lock_info in relocks.items():
            chunk = state['chunks'].setdefault(name, _empty_chunk())
            chunk['ready'] = True
            chunk['lock'] = lock_info
        for name in unlocks:
            if name in state['chunks']:
                state['chunks'][name]['lock'] = None
        saved = save(state, station_id, date_str, cfg)
    logger.debug('[%s/%s] Lock batch: %d relocked, %d unlocked',
                  station_id, date_str, len(relocks), len(unlocks))
    return saved


def mark_stacked(station_id: str, date_str: str, chunk_name: str, cfg: dict) -> None:
    with _lock(station_id, date_str, cfg):
        state = _load_for_write(station_id, date_str, cfg)
        if state is None:
            return None
        state['chunks'].setdefault(chunk_name, _empty_chunk())['stacked'] = True
        save(state, station_id, date_str, cfg)


def mark_reencoded(station_id: str, date_str: str, chunk_name: str, cfg: dict) -> None:
    with _lock(station_id, date_str, cfg):
        state = _load_for_write(station_id, date_str, cfg)
        if state is None:
            return None
        state['chunks'].setdefault(chunk_name, _empty_chunk())['reencoded'] = True
        save(state, station_id, date_str, cfg)


# ---------------------------------------------------------------------------
# Night-level updates
# ---------------------------------------------------------------------------

def mark_rms_complete(station_id: str, date_str: str, cfg: dict) -> bool:
    with _lock(station_id, date_str, cfg):
        state = _load_for_write(station_id, date_str, cfg)
        if state is None:
            return False
        state['rms_complete'] = True
        _stamp(state, 'rms_complete')
        saved = save(state, station_id, date_str, cfg)
    if saved:
        logger.info('[%s/%s] RMS marked complete', station_id, date_str)
    return saved


def mark_eon_done(station_id: str, date_str: str, cfg: dict) -> bool:
    """Mark the EON detection-lock pass as successfully completed for this night.

    Set only when detection_lock actually reconciled against an FTPdetectinfo —
    a skipped pass (RMS output not on disk yet) leaves the flag unset so dawn
    retries the night on a later run instead of losing its detections."""
    with _lock(station_id, date_str, cfg):
        state = _load_for_write(station_id, date_str, cfg)
        if state is None:
            return False
        state['eon_done'] = True
        saved = save(state, station_id, date_str, cfg)
    if saved:
        logger.info('[%s/%s] EON detection pass marked done', station_id, date_str)
    return saved


def is_eon_done(station_id: str, date_str: str, cfg: dict) -> bool:
    """Return True if the EON detection-lock pass has completed for this night."""
    state = peek(station_id, date_str, cfg)
    return bool(state.get('eon_done', False))


# ---------------------------------------------------------------------------
# Night completion
# ---------------------------------------------------------------------------

def mark_morning_done(station_id: str, date_str: str, cfg: dict) -> bool:
    """Mark the morning sequence as fully complete for this night."""
    with _lock(station_id, date_str, cfg):
        state = _load_for_write(station_id, date_str, cfg)
        if state is None:
            return False
        state['morning_done'] = True
        _stamp(state, 'morning_done')
        saved = save(state, station_id, date_str, cfg)
    if saved:
        logger.info('[%s/%s] Morning sequence marked done', station_id, date_str)
    return saved


def is_morning_done(station_id: str, date_str: str, cfg: dict) -> bool:
    """Return True if the morning sequence has already completed for this night."""
    state = peek(station_id, date_str, cfg)
    return bool(state.get('morning_done', False))


def mark_chunk_uploaded(station_id: str, date_str: str, chunk_name: str, cfg: dict) -> None:
    """Mark a chunk's MKV as uploaded to the archive."""
    with _lock(station_id, date_str, cfg):
        state = _load_for_write(station_id, date_str, cfg)
        if state is None:
            return None
        state['chunks'].setdefault(chunk_name, _empty_chunk())['uploaded'] = True
        _stamp_upload(state)
        save(state, station_id, date_str, cfg)


def is_chunk_uploaded(station_id: str, date_str: str, chunk_name: str, cfg: dict) -> bool:
    state = peek(station_id, date_str, cfg)
    return bool(state.get('chunks', {}).get(chunk_name, {}).get('uploaded', False))


def unmark_chunk_uploaded(station_id: str, date_str: str, chunk_name: str, cfg: dict) -> None:
    """Clear uploaded + stack_uploaded flags (e.g. after remote deletion on unlock)."""
    with _lock(station_id, date_str, cfg):
        state = _load_for_write(station_id, date_str, cfg)
        if state is None:
            return None
        chunk = state.get('chunks', {}).get(chunk_name)
        if chunk is not None:
            chunk['uploaded'] = False
            chunk['stack_uploaded'] = False
            save(state, station_id, date_str, cfg)


def mark_chunk_stack_uploaded(station_id: str, date_str: str, chunk_name: str, cfg: dict) -> None:
    """Mark a chunk's detection stack WebP as uploaded to the archive."""
    with _lock(station_id, date_str, cfg):
        state = _load_for_write(station_id, date_str, cfg)
        if state is None:
            return None
        state['chunks'].setdefault(chunk_name, _empty_chunk())['stack_uploaded'] = True
        save(state, station_id, date_str, cfg)


def is_chunk_stack_uploaded(station_id: str, date_str: str, chunk_name: str, cfg: dict) -> bool:
    state = peek(station_id, date_str, cfg)
    return bool(state.get('chunks', {}).get(chunk_name, {}).get('stack_uploaded', False))


def mark_timelapse_uploaded(station_id: str, date_str: str, cfg: dict) -> None:
    with _lock(station_id, date_str, cfg):
        state = _load_for_write(station_id, date_str, cfg)
        if state is None:
            return None
        state['timelapse_uploaded'] = True
        save(state, station_id, date_str, cfg)


def is_timelapse_uploaded(station_id: str, date_str: str, cfg: dict) -> bool:
    state = peek(station_id, date_str, cfg)
    return bool(state.get('timelapse_uploaded', False))


def mark_night_stack_uploaded(station_id: str, date_str: str, cfg: dict) -> None:
    with _lock(station_id, date_str, cfg):
        state = _load_for_write(station_id, date_str, cfg)
        if state is None:
            return None
        state['night_stack_uploaded'] = True
        save(state, station_id, date_str, cfg)


def is_night_stack_uploaded(station_id: str, date_str: str, cfg: dict) -> bool:
    state = peek(station_id, date_str, cfg)
    return bool(state.get('night_stack_uploaded', False))


# Night products shipped as whole files. The marker is the (size, mtime_ns) of
# the version that was uploaded, so a rebuilt file (a deferred timelapse
# finished on a later morning, a meteor stack rebuilt once more chunks were
# stacked) uploads again; a boolean could never say that. timelapse_uploaded /
# night_stack_uploaded are still set for the readers that know them, and a
# night that only has the boolean (uploaded before the marker existed) counts
# as uploaded.
_PRODUCT_LEGACY_FLAGS = {
    'timelapse': 'timelapse_uploaded',
    'night_stack': 'night_stack_uploaded',
}


def mark_product_uploaded(station_id: str, date_str: str, product: str,
                          size: int, mtime_ns: int, cfg: dict) -> None:
    """Record that this version (size, mtime_ns) of a night product uploaded."""
    with _lock(station_id, date_str, cfg):
        state = _load_for_write(station_id, date_str, cfg)
        if state is None:
            return None
        state.setdefault('products_uploaded', {})[product] = [size, mtime_ns]
        legacy = _PRODUCT_LEGACY_FLAGS.get(product)
        if legacy:
            state[legacy] = True
        save(state, station_id, date_str, cfg)


def is_product_uploaded(station_id: str, date_str: str, product: str,
                        size: int, mtime_ns: int, cfg: dict) -> bool:
    """True if exactly this version of the night product was uploaded."""
    state = peek(station_id, date_str, cfg)
    sig = state.get('products_uploaded', {}).get(product)
    if sig is not None:
        return list(sig) == [size, mtime_ns]
    legacy = _PRODUCT_LEGACY_FLAGS.get(product)
    return bool(legacy and state.get(legacy, False))


# Upload marks applied in bulk: (kind, name, value).
MARK_CHUNK = 'chunk'              # chunks[name].uploaded
MARK_CHUNK_STACK = 'chunk_stack'  # chunks[name].stack_uploaded (name: the chunk's MKV)
MARK_PRODUCT = 'product'          # products_uploaded[name] = value ([size, mtime_ns])
MARK_RMS = 'rms'                  # rms_uploaded[name] = value ([size, mtime_ns, sha1])
MARK_FFTHUMB = 'ffthumb'          # ffthumbs_uploaded[name] = True


def mark_uploaded_batch(station_id: str, date_str: str,
                        marks: list[tuple[str, str, object]], cfg: dict) -> bool:
    """Record many confirmed uploads for one night in one load-modify-save.

    An upload pass used to rewrite the night's whole state.json (~375 KB on a
    busy night) once per file: ~0.5 GB of writes for a 1,400-stack night, all
    under the night's lock that capture and dawn also take. Returns whether
    the write reached the disk (see save()).
    """
    if not marks:
        return True
    with _lock(station_id, date_str, cfg):
        state = _load_for_write(station_id, date_str, cfg)
        if state is None:
            return False
        for kind, name, value in marks:
            if kind == MARK_CHUNK:
                state['chunks'].setdefault(name, _empty_chunk())['uploaded'] = True
                _stamp_upload(state)
            elif kind == MARK_CHUNK_STACK:
                state['chunks'].setdefault(name, _empty_chunk())['stack_uploaded'] = True
            elif kind == MARK_PRODUCT:
                state.setdefault('products_uploaded', {})[name] = list(value)  # type: ignore[call-overload]
                legacy = _PRODUCT_LEGACY_FLAGS.get(name)
                if legacy:
                    state[legacy] = True
            elif kind == MARK_RMS:
                state.setdefault('rms_uploaded', {})[name] = value
            elif kind == MARK_FFTHUMB:
                state.setdefault('ffthumbs_uploaded', {})[name] = True
            else:
                raise ValueError(f'unknown upload mark {kind!r}')
        return save(state, station_id, date_str, cfg)


def mark_rms_file_uploaded(station_id: str, date_str: str, filename: str,
                           mark: object, cfg: dict) -> None:
    """Record the version of an RMS file (FTPdetectinfo, radiants, a check
    plot) that reached the archive, so the uploader stops re-POSTing it every
    cycle. ``mark`` is ``[size, mtime_ns, sha1]`` (see rms_upload_mark)."""
    with _lock(station_id, date_str, cfg):
        state = _load_for_write(station_id, date_str, cfg)
        if state is None:
            return None
        state.setdefault('rms_uploaded', {})[filename] = mark
        save(state, station_id, date_str, cfg)


def rms_upload_mark(station_id: str, date_str: str, filename: str,
                    cfg: dict) -> object:
    """The recorded upload of an RMS file: ``[size, mtime_ns, sha1]``, a bare
    size from before 2026-10-21 (legacy), or None if it never uploaded.

    A bare size cannot tell a rewrite from the original: an astrometric
    recalibration rewrites FTPdetectinfo's fixed-width columns at the same
    byte size, so the archive kept the old RA/Dec forever."""
    state = peek(station_id, date_str, cfg)
    return state.get('rms_uploaded', {}).get(filename)


def mark_ffthumb_uploaded(station_id: str, date_str: str, filename: str, cfg: dict) -> None:
    """Record that a per-detection FF thumbnail landed on the archive, so the
    indexer's live push and archive_upload's backfill sweep stop re-POSTing it."""
    with _lock(station_id, date_str, cfg):
        state = _load_for_write(station_id, date_str, cfg)
        if state is None:
            return None
        state.setdefault('ffthumbs_uploaded', {})[filename] = True
        save(state, station_id, date_str, cfg)


def is_ffthumb_uploaded(station_id: str, date_str: str, filename: str, cfg: dict) -> bool:
    state = peek(station_id, date_str, cfg)
    return bool(state.get('ffthumbs_uploaded', {}).get(filename))


def uploaded_ffthumbs(station_id: str, date_str: str, cfg: dict) -> set[str]:
    """Return the set of FF thumbnail filenames already uploaded for a night."""
    state = peek(station_id, date_str, cfg)
    return {k for k, v in state.get('ffthumbs_uploaded', {}).items() if v}


def mark_timelapse_done(station_id: str, date_str: str, cfg: dict) -> bool:
    """Mark timelapse as done for this night (built, skipped, or disabled)."""
    with _lock(station_id, date_str, cfg):
        state = _load_for_write(station_id, date_str, cfg)
        if state is None:
            return False
        state['timelapse_done'] = True
        state['timelapse_deferred'] = False
        saved = save(state, station_id, date_str, cfg)
    if saved:
        logger.info('[%s/%s] Timelapse marked done', station_id, date_str)
    return saved


def is_timelapse_done(station_id: str, date_str: str, cfg: dict) -> bool:
    """Return True if timelapse has been built or was not applicable for this night."""
    state = peek(station_id, date_str, cfg)
    return bool(state.get('timelapse_done', False))


def mark_timelapse_deferred(station_id: str, date_str: str, cfg: dict,
                            reason: str = 'stack coverage too low') -> bool:
    """Mark timelapse as deferred so a later dawn run retries it.

    ``reason`` is logged as given: a deadline stop used to be reported as low
    coverage, which sent the 2026-09-23 diagnosis the wrong way."""
    with _lock(station_id, date_str, cfg):
        state = _load_for_write(station_id, date_str, cfg)
        if state is None:
            return False
        state['timelapse_deferred'] = True
        saved = save(state, station_id, date_str, cfg)
    if saved:
        logger.warning('[%s/%s] Timelapse deferred (%s)', station_id, date_str, reason)
    return saved


def is_timelapse_deferred(station_id: str, date_str: str, cfg: dict) -> bool:
    """Return True if timelapse was skipped due to insufficient stack coverage."""
    state = peek(station_id, date_str, cfg)
    return bool(state.get('timelapse_deferred', False))


# ---------------------------------------------------------------------------
# Station-level: why this morning's full-night re-encode was stopped
# ---------------------------------------------------------------------------

# Beside config.json, so it survives bundle updates (the updater applies the
# bundle without --delete) and is not tied to any one night's capture dir.
FULL_NIGHT_MARKER_PATH = Path.home() / 'rovimen_scripts' / 'full_night_reencode_off.json'


def read_full_night_marker() -> dict | None:
    """Why the latest morning's full-night re-encode was stopped, or None.

    Informational only (station_api shows it): dawn_process measures the pass
    again every morning and clears this after a pass that runs to completion.
    """
    path = FULL_NIGHT_MARKER_PATH
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {'reason': 'unreadable'}
    return data if isinstance(data, dict) else {'reason': 'unreadable'}


def write_full_night_marker(decision: dict) -> None:
    """Atomically record why the full-night re-encode was stopped.

    Logged, not raised: the record is informational.
    """
    path = FULL_NIGHT_MARKER_PATH
    tmp = path.with_name(path.name + '.tmp')
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(decision, indent=2))
        os.replace(tmp, path)
    except OSError as exc:
        logger.error('Could not record the full-night re-encode decision at %s: %s',
                     path, exc)


def clear_full_night_marker() -> None:
    """Forget the last stop once a full-night pass has run to completion."""
    try:
        FULL_NIGHT_MARKER_PATH.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning('Could not clear %s: %s', FULL_NIGHT_MARKER_PATH, exc)
