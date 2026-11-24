"""Local-disk spool for station-pushed archive blobs, flushed to the Storage
Box off the request path.

The HTTPS-ingest byte plane used to stream pushed blobs (clips, stacks,
timelapses, state) *synchronously* onto the FUSE sshfs Storage Box mount inside
the Flask request thread (``ingest_api.ingest_blob``). When the box stalled the
request thread blocked for the whole transfer and the station's connection
reset (the observed ``ConnectionResetError``); a burst of pushes could pin the
whole gthread pool on sshfs I/O and freeze the public site.

This module decouples the two planes:

* **Request path** writes the blob to local NVMe (this module's ``SPOOL_DIR``)
  and returns immediately -- no sshfs on the hot path.
* **A bounded background flusher** moves each spooled file onto the archive
  with the ``_with_sshfs_timeout`` guard, retrying on the next pass when the box
  is slow. The spool naturally buffers pushes while the box catches up.

Two properties keep meteor clips (the thing users actually watch) from starving
behind bulk uploads:

* **Priority ordering.** A single station stacking a night dumps thousands of
  ``stacks/*.webp`` thumbnails; at ~2.6 files/s over a stalling sshfs that is a
  multi-hour backlog. A flat ``rglob`` walk would flush those ahead of a meteor
  clip pushed seconds ago, so a whole night of clips reads "clip not available"
  until the thumbnails clear. Each pass therefore flushes **meteor clips**
  (``meteors/*_color.{mkv,mp4}``) before any bulk blob, so a fresh clip jumps the
  whole thumbnail queue.
* **Multi-lane copying.** One thread over a 60 s-per-file sshfs copy caps the
  drain at one file at a time, so a slow box serialises everything. A small
  worker pool runs several archive copies concurrently, so a stack backlog on one
  lane can't block clips on another. The atomic rename-to-claim already makes
  concurrent flushes safe (each file is flushed by exactly one lane / worker /
  process), so widening to N lanes needs no extra locking.

Crash-safety: a spooled file is removed only after its archive write is
durable, and the flusher rescans the spool on startup (and periodically), so a
restart re-flushes anything left behind -- a blob is never acknowledged-then-lost.

That guarantee holds only while nothing *else* deletes the spool. The spool sits
under the install root, which both deploy workflows sync with ``rsync --delete``,
so they must exclude ``blob_spool/`` or every deploy destroys the in-flight
blobs that stations have already been told (HTTP 200) were accepted and will
never re-send. ``tests/test_deploy_rsync_excludes.py`` guards that exclusion.

Archive-side partials: each copy writes a unique ``.part-flush-<uuid>`` temp on
the Storage Box. The hot path never lists the (sshfs, thousands-of-entries)
night directory to look for stale ones; only copies that were actually
abandoned or interrupted are journaled and cleaned (``AbandonedCopyJournal``).

The spool mirrors the archive-relative tree (``<cam>/<date>/<subdir>/<file>``),
so the destination is encoded entirely in the path and needs no sidecar. Multiple
gunicorn workers may run a flusher each; they coordinate with an atomic
rename-to-claim so exactly one worker flushes any given file.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from atomic_write import write_atomic

logger = logging.getLogger("blob_spool")

# Suffix marking a file a flusher has claimed. Encodes pid+tid so a crash leaves
# an identifiable orphan the next startup scan reclaims.
_CLAIM_SUFFIX = ".flushing-"
# How often the flusher rescans the spool even with no kick (crash-safety /
# retry-when-box-recovers backstop).
_SCAN_INTERVAL_S = float(os.environ.get("ROVIMEN_BLOB_FLUSH_INTERVAL_S", "30"))
# Per-file archive-write timeout (via the sshfs guard). Generous: a large clip
# copy over a healthy box is still well under this; a stalled box trips it and
# the file stays spooled for the next pass.
_FLUSH_TIMEOUT_S = float(os.environ.get("ROVIMEN_BLOB_FLUSH_TIMEOUT_S", "60"))
# Concurrent archive copies per drain pass. One lane over a 60 s-per-file sshfs
# serialises every blob behind the slowest copy; a small pool lets clips flush
# on a free lane while a stack backlog occupies others. Kept small so a burst of
# lanes can't itself pin the box or the gthread pool.
_FLUSH_LANES = max(1, int(os.environ.get("ROVIMEN_BLOB_FLUSH_LANES", "4")))
# How old a claim held by a STILL-LIVE process must be before the startup scan
# treats it as abandoned and un-claims it anyway. A healthy claim lives for at
# most one guarded archive write, so several multiples of that timeout is well
# clear of any in-flight flush while still rescuing a wedged (but not crashed)
# flusher's file.
_CLAIM_STALE_S = float(
    os.environ.get("ROVIMEN_BLOB_CLAIM_STALE_S", str(_FLUSH_TIMEOUT_S * 5))
)
# How long an empty spool directory must have sat untouched before the pruner
# removes it. The ingest path does mkdir(parents=True) and only then opens its
# ``.part`` temp, so a directory created for an in-flight push is momentarily
# empty; requiring it to be idle for this long keeps the pruner from deleting a
# directory a push is about to write into. Every spool mutation (mkdir, the
# .part create, the commit rename, the flusher's claim/unlink) bumps the parent
# mtime, so "empty and untouched for minutes" means genuinely abandoned.
_EMPTY_DIR_GRACE_S = float(os.environ.get("ROVIMEN_BLOB_EMPTY_DIR_GRACE_S", "600"))


# In-flight ingest writes are ``.<filename>.part-<pid>-<tid>`` (ingest_api). A
# worker killed mid-upload (recycle, deploy, OOM) leaves its .part behind, and
# nothing flushes or removes a dot-file, so they sat in prod's spool for weeks
# (three from 2026-09-03..07, 32 MB). One is reaped only when its writer PID is
# gone AND it has been idle for _PART_ABANDONED_S: the station never got a 200
# for it, so it re-sends the blob and nothing is lost.
_PART_MARKER = ".part-"
_PART_ABANDONED_S = float(os.environ.get("ROVIMEN_BLOB_PART_ABANDONED_S", "3600"))
_PART_REAP_INTERVAL_S = 600.0


# Archive-side temp a flush copy writes before os.replace()ing it onto the dest.
_FLUSH_TMP_MARKER = ".part-flush-"
# Per-process journal (spool root, local disk) of archive leftovers this process
# could not clean up. Dot-prefixed so the flusher never treats it as a blob.
_JOURNAL_RE = re.compile(r"^\.abandoned-copies-(\d+)\.json(?:\.adopting-(\d+))?$")
# Bound on the per-process "archive directory already exists" cache.
_KNOWN_DIRS_MAX = 50_000


# Per-chunk fields in a night's state.json that only the VPS side knows, so a
# station re-upload (which carries the station's own copy) must not erase them:
# the retention janitor's prune marks. ``archived_lock`` is kept alongside: the
# last non-null ``lock`` the box saw, because the station's dawn sweep clears
# ``lock`` a week after the night (``dawn_process._sweep_stale_locks``) and
# the janitor and the clip-provenance code still need to know the clip was a
# detection or a manual lock.
_STATE_VPS_FIELDS = ("video_pruned", "video_pruned_at")


def merge_state_json(incoming: dict, existing: dict | None) -> dict:
    """``incoming`` (a station's state.json) with the box copy's VPS-only
    per-chunk fields carried over and ``archived_lock`` maintained."""
    old_chunks = (existing or {}).get("chunks") or {}
    chunks = incoming.get("chunks")
    if not isinstance(chunks, dict) or not isinstance(old_chunks, dict):
        return incoming
    for name, info in chunks.items():
        if not isinstance(info, dict):
            continue
        old = old_chunks.get(name)
        old = old if isinstance(old, dict) else {}
        for field_name in _STATE_VPS_FIELDS:
            if field_name in old and field_name not in info:
                info[field_name] = old[field_name]
        lock = info.get("lock")
        if lock:
            info["archived_lock"] = lock
        else:
            prev = old.get("archived_lock") or old.get("lock")
            if prev and "archived_lock" not in info:
                info["archived_lock"] = prev
    return incoming


def _merged_state_bytes(src: Path, dest: Path) -> bytes | None:
    """The bytes to publish for an arriving state.json, or None to copy the
    file unchanged (unparseable input: never block an upload on the merge)."""
    try:
        incoming = json.loads(src.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(incoming, dict):
        return None
    try:
        existing = json.loads(dest.read_text())
    except FileNotFoundError:
        existing = None
    except (OSError, ValueError):
        existing = None
    return json.dumps(merge_state_json(incoming, existing), indent=2).encode()


def _flush_tmp_name(dest_name: str) -> str:
    """Unique archive temp name for one copy attempt onto ``dest_name``."""
    return f".{dest_name}{_FLUSH_TMP_MARKER}{uuid.uuid4().hex}"


def _unlink_quietly(path: Path) -> bool:
    """Remove ``path``; True when it is gone afterwards (already absent counts)."""
    try:
        path.unlink()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


@dataclass
class _CopyAttempt:
    """One archive copy's lifecycle, shared between the copy thread and the
    flusher that may give up on it (the sshfs guard abandons a timed-out
    thread, which keeps writing its temp)."""

    tmp_rel: str
    started: bool = False
    finished: bool = False
    clean: bool = False
    abandoned: bool = False


class AbandonedCopyJournal:
    """Archive partials this process knows it left behind, so only those are
    cleaned up.

    Every flush used to ``glob`` the destination night directory on the sshfs
    Storage Box for ``.<name>.part-flush-*`` partials, just in case an earlier
    copy had been abandoned. With ~86k blobs a day into night directories
    holding thousands of entries, that listing was the single hottest on-CPU
    stack in the dashboard (py-spy, 2026-09-24). Abandonment is rare and always
    observable, so this journal records exactly what needs cleaning instead:

    * ``tmps`` -- exact archive-relative temp paths of copies that were
      abandoned (timed out) or failed without removing their temp. Cleaned with
      one ``unlink``, no listing.
    * ``dests`` -- archive-relative destinations whose partial temp names are
      unknown because the copying process died mid-flush (seen as a reclaimed
      ``*.flushing-<pid>`` claim). Cleaned with one targeted glob.

    Persisted to a small per-process JSON file in the spool root (local disk)
    only when an entry is added or removed, so a restart forgets nothing; the
    next process adopts the journals of dead processes.
    """

    def __init__(self, spool_root: Path) -> None:
        self._spool_root = spool_root
        self._pid = os.getpid()
        self._path = spool_root / f".abandoned-copies-{self._pid}.json"
        self._lock = threading.Lock()
        self._tmps: set[str] = set()
        self._dests: set[str] = set()

    def __bool__(self) -> bool:
        with self._lock:
            return bool(self._tmps or self._dests)

    def snapshot(self) -> tuple[set[str], set[str]]:
        with self._lock:
            return set(self._tmps), set(self._dests)

    def add(self, *, tmp: str | None = None, dest: str | None = None) -> None:
        with self._lock:
            if tmp is not None:
                self._tmps.add(tmp)
            if dest is not None:
                self._dests.add(dest)
            self._persist_locked()

    def discard(self, *, tmp: str | None = None, dest: str | None = None) -> None:
        with self._lock:
            before = len(self._tmps) + len(self._dests)
            if tmp is not None:
                self._tmps.discard(tmp)
            if dest is not None:
                self._dests.discard(dest)
            if len(self._tmps) + len(self._dests) != before:
                self._persist_locked()

    def adopt_dead(self, pid_alive: Callable[[int], bool]) -> int:
        """Merge the journals of processes that no longer exist into this one.
        A rename-to-claim makes each dead journal adopted by exactly one
        process. Returns the number of entries adopted."""
        try:
            names = [p.name for p in self._spool_root.iterdir()]
        except OSError:
            return 0
        adopted = 0
        for name in names:
            m = _JOURNAL_RE.match(name)
            if m is None:
                continue
            owner = int(m.group(2) or m.group(1))
            if owner == self._pid or pid_alive(owner):
                continue
            src = self._spool_root / name
            claim = self._spool_root / f"{name.split('.adopting-', 1)[0]}.adopting-{self._pid}"
            try:
                os.rename(src, claim)
            except OSError:
                continue  # another process adopted it first
            tmps, dests = _read_journal(claim)
            with self._lock:
                self._tmps |= tmps
                self._dests |= dests
                self._persist_locked()
            adopted += len(tmps) + len(dests)
            _unlink_quietly(claim)
        if adopted:
            logger.info("adopted %d abandoned-copy journal entries", adopted)
        return adopted

    def _persist_locked(self) -> None:
        if not self._tmps and not self._dests:
            if not _unlink_quietly(self._path):
                logger.warning("could not remove abandoned-copy journal %s", self._path)
            return
        payload = json.dumps({"tmps": sorted(self._tmps), "dests": sorted(self._dests)})
        try:
            self._spool_root.mkdir(parents=True, exist_ok=True)
            write_atomic(self._path, payload,
                         tmp=self._path.with_name(f"{self._path.name}.w"))
        except OSError:
            logger.warning("could not persist abandoned-copy journal %s", self._path)


def _read_journal(path: Path) -> tuple[set[str], set[str]]:
    """Entries of a journal file; a corrupt or unreadable file yields nothing
    (worst case a partial stays on the archive, never a lost blob)."""
    try:
        data = json.loads(path.read_text())
        return (
            {str(t) for t in data.get("tmps", [])},
            {str(d) for d in data.get("dests", [])},
        )
    except (OSError, ValueError, AttributeError, TypeError):
        logger.warning("ignoring unreadable abandoned-copy journal %s", path)
        return set(), set()


def _part_owner_pid(name: str) -> int | None:
    """Writer PID in a ``.<file>.part-<pid>-<tid>`` name, or None."""
    if not name.startswith("."):
        return None
    _, sep, tail = name.rpartition(_PART_MARKER)
    if not sep:
        return None
    try:
        return int(tail.split("-", 1)[0])
    except ValueError:
        return None


def _claim_owner_pid(name: str) -> int | None:
    """PID encoded in a ``*.flushing-<pid>-<tid>`` claim name, or None if the
    name does not carry a parseable owner."""
    _, sep, tail = name.partition(_CLAIM_SUFFIX)
    if not sep:
        return None
    pid_text = tail.split("-", 1)[0]
    try:
        return int(pid_text)
    except ValueError:
        return None


def _pid_alive(pid: int) -> bool:
    """Whether a process with ``pid`` currently exists. Used to tell a crashed
    flusher's orphaned claim (safe to steal) from a sibling gunicorn worker's
    live claim (stealing it causes a double flush and an un-claim error)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user
    except OSError:
        return True  # unknown -- assume live and leave the claim alone
    return True


def _is_priority(rel: Path) -> bool:
    """A meteor clip -- ``meteors/*_color.{mkv,mp4}`` -- flushed ahead of bulk
    blobs (stack thumbnails, timelapses, state). ``rel`` is spool-relative and
    may carry a routing prefix; only the trailing ``meteors/<file>`` shape
    matters, so a match anywhere in the tail is enough."""
    name = rel.name
    if not (name.endswith("_color.mkv") or name.endswith("_color.mp4")):
        return False
    return "meteors" in rel.parts[:-1]


def spool_dir() -> Path:
    """Local NVMe spool root. Tier-correct without extra config: defaults to a
    ``blob_spool`` sibling of ``ROVIMEN_CACHE_PATH`` (so the dev tier's
    ``/opt/rovimen-dev/cache`` yields ``/opt/rovimen-dev/blob_spool``), and is
    fully overridable with ``ROVIMEN_BLOB_SPOOL_DIR``."""
    override = os.environ.get("ROVIMEN_BLOB_SPOOL_DIR")
    if override:
        return Path(override)
    cache = Path(os.environ.get("ROVIMEN_CACHE_PATH", "/opt/rovimen/cache"))
    return cache.parent / "blob_spool"


class BlobSpool:
    """Owns the spool directory and a single background flusher thread.

    One instance per process. ``enqueue`` is called from the request path after
    a blob lands in the spool; the flusher also rescans on a timer so nothing is
    stranded if a kick is missed or a restart happens mid-flush.
    """

    def __init__(
        self,
        archive_root: Path,
        *,
        spool_root: Path | None = None,
        sshfs_guard: Callable[..., object] | None = None,
        on_flushed: Callable[[Path, Path], None] | None = None,
        before_flush: Callable[[Path, Path], object] | None = None,
        route_prefix_parts: int = 0,
        lanes: int = _FLUSH_LANES,
    ) -> None:
        """``archive_root`` is the Storage Box mount (sshfs). ``sshfs_guard`` is
        ``station_client._with_sshfs_timeout`` (injected to avoid an import
        cycle and to let tests substitute it). ``on_flushed(rel, local_path)``
        runs after a successful archive write, before the local copy is removed
        -- e.g. to register a media pointer only once the blob is durably
        archived. ``before_flush(archive_rel, claimed_path)`` runs once the
        flusher owns a file, before its archive copy, and may write new files
        into the claimed file's spool directory (they flush like pushed blobs;
        see faststart_sibling); a non-None result kicks another drain pass.
        ``route_prefix_parts`` is the number of leading spool-relative
        path components that are a *routing* prefix (e.g. the pushing station)
        and are stripped when mapping the spool file onto the archive; the full
        (prefixed) relative path is still what ``on_flushed`` receives. ``lanes``
        is how many archive copies run concurrently within a drain pass (a small
        pool so a slow sshfs copy doesn't serialise every blob behind it)."""
        self.archive_root = Path(archive_root)
        self.spool_root = Path(spool_root) if spool_root is not None else spool_dir()
        self._guard = sshfs_guard
        self._on_flushed = on_flushed
        self._before_flush = before_flush
        self._route_prefix_parts = route_prefix_parts
        self._lanes = max(1, int(lanes))
        self._kick = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = threading.Lock()
        self._parts_reaped_at = float("-inf")
        self._journal = AbandonedCopyJournal(self.spool_root)
        self._attempts_lock = threading.Lock()
        self._inflight: dict[str, _CopyAttempt] = {}
        self._known_dirs: set[str] = set()

    # -- request-path API -------------------------------------------------

    def spool_path_for(self, rel: str) -> Path:
        """Absolute spool path for an archive-relative path (``cam/date/...``)."""
        return self.spool_root / rel

    def enqueue(self) -> None:
        """Signal the flusher that new work exists (best-effort; the periodic
        rescan is the guarantee)."""
        self._kick.set()

    def start(self) -> None:
        """Start the single flusher thread. Idempotent."""
        with self._started:
            if self._thread is not None:
                return
            t = threading.Thread(
                target=self._run, name="blob-flusher", daemon=True
            )
            self._thread = t
        t.start()
        logger.info(
            "blob spool flusher started (spool=%s archive=%s)",
            self.spool_root, self.archive_root,
        )

    # -- flusher ----------------------------------------------------------

    def _run(self) -> None:
        # Reclaim any files a previous process left mid-flush before the first
        # pass, then loop: kick-or-timeout -> drain.
        self._reclaim_orphans()
        self._journal.adopt_dead(_pid_alive)
        while True:
            self._kick.wait(timeout=_SCAN_INTERVAL_S)
            self._kick.clear()
            try:
                self.flush_once()
            except Exception:
                logger.exception("blob flusher pass failed")

    def _reclaim_orphans(self) -> None:
        """Un-claim ``*.flushing-<pid>-<tid>`` files left by a *crashed* flusher
        so the next pass retries them.

        Claims owned by a process that is still alive are left alone. Every
        gunicorn worker runs its own flusher and workers recycle on a request
        cadence (``max_requests``), so a fresh worker's startup scan runs while
        siblings are mid-flush. Un-claiming their files renamed the blob out
        from under the owning flusher: the owner's un-claim then failed with
        FileNotFoundError (the ERROR bursts on prod) and, worse, both processes
        could copy the same blob to the archive. Gating on liveness makes the
        scan reclaim only what it is actually for -- files whose flusher died.

        A live owner that has been sitting on a claim for longer than any
        plausible archive write (``_CLAIM_STALE_S``) is still reclaimed, so a
        wedged flusher can't strand a blob forever.
        """
        if not self.spool_root.exists():
            return
        now = time.time()
        for claimed in self.spool_root.rglob(f"*{_CLAIM_SUFFIX}*"):
            owner = _claim_owner_pid(claimed.name)
            if owner is not None and owner != os.getpid() and _pid_alive(owner):
                try:
                    age = now - claimed.stat().st_mtime
                except OSError:
                    continue  # vanished under us -- the owner is handling it
                if age < _CLAIM_STALE_S:
                    continue  # a sibling worker is flushing this right now
                logger.warning(
                    "reclaiming spool claim %s held by live pid %d for %.0fs",
                    claimed.name, owner, age,
                )
            base_name = claimed.name.split(_CLAIM_SUFFIX, 1)[0]
            try:
                os.replace(claimed, claimed.with_name(base_name))
            except OSError:
                continue
            # The owner died (or wedged) mid-copy, so it may have left a partial
            # temp on the archive whose name nobody knows: sweep that dest once.
            rel = claimed.with_name(base_name).relative_to(self.spool_root)
            self._journal.add(dest=str(self._archive_rel(rel)))

    def _candidates(self) -> tuple[list[Path], list[Path]]:
        """Partition spool files into (priority, bulk) flush tiers.

        Priority = meteor clips (see ``_is_priority``). A claimed ``*.flushing-*``
        file or a ``.``-prefixed in-flight ``.part`` write is skipped -- another
        lane/process owns it, or it isn't fully written yet."""
        priority: list[Path] = []
        bulk: list[Path] = []
        for path in self.spool_root.rglob("*"):
            if not path.is_file():
                continue
            if _CLAIM_SUFFIX in path.name or path.name.startswith("."):
                continue  # in-flight claim or a .part being written
            (priority if _is_priority(path.relative_to(self.spool_root)) else bulk).append(path)
        return priority, bulk

    def _flush_batch(self, paths: Iterable[Path]) -> int:
        """Flush a tier across up to ``self._lanes`` concurrent lanes. The atomic
        rename-to-claim makes each file the property of exactly one lane, so no
        two lanes ever copy the same blob; a lane that loses the claim (or hits a
        stalled box) just returns False and the file is retried next pass."""
        batch = list(paths)
        if not batch:
            return 0
        if self._lanes == 1 or len(batch) == 1:
            return sum(1 for p in batch if self._flush_file(p))
        with ThreadPoolExecutor(
            max_workers=min(self._lanes, len(batch)), thread_name_prefix="blob-flush"
        ) as pool:
            return sum(1 for ok in pool.map(self._flush_file, batch) if ok)

    def flush_once(self) -> int:
        """One drain pass, meteor clips first. Returns the number of blobs
        successfully flushed. Separated from the loop so tests can drive it
        deterministically.

        The priority tier is fully dispatched before the bulk tier so a clip
        never waits behind a stack backlog; within each tier the copies run
        across ``self._lanes`` lanes so a slow sshfs copy can't serialise the
        rest."""
        if not self.spool_root.exists():
            return 0
        priority, bulk = self._candidates()
        flushed = self._flush_batch(priority) + self._flush_batch(bulk)
        if self._journal:
            self._clean_abandoned()
        if time.monotonic() - self._parts_reaped_at >= _PART_REAP_INTERVAL_S:
            self._parts_reaped_at = time.monotonic()
            self._reap_abandoned_parts()
        self._prune_empty_dirs()
        return flushed

    def _reap_abandoned_parts(self) -> int:
        """Delete ``.part`` ingest writes whose writer died (see _PART_MARKER)."""
        now = time.time()
        reaped = 0
        for path in self.spool_root.rglob(f".*{_PART_MARKER}*"):
            owner = _part_owner_pid(path.name)
            if owner is None or _pid_alive(owner):
                continue
            try:
                st = path.stat()
                if now - st.st_mtime < _PART_ABANDONED_S:
                    continue
                path.unlink()
            except OSError:
                continue
            reaped += 1
            logger.info(
                "reaped abandoned spool upload %s (%d bytes, writer pid %d gone)",
                path.relative_to(self.spool_root), st.st_size, owner,
            )
        return reaped

    def _prune_empty_dirs(self) -> int:
        """Remove directories the flusher emptied, deepest first.

        The spool mirrors ``<station>/<cam>/<date>/<subdir>/`` and a flusher only
        ever removes *files*, so every drained night leaves its directory tree
        behind forever. On prod that had accumulated to 66 MB of directory
        entries under an otherwise-empty spool. Only directories are removed,
        never files: ``os.rmdir`` refuses a non-empty directory, so a blob (or a
        ``.part`` write in progress) can never be destroyed by this pass even if
        one lands mid-walk.

        The spool root itself is kept -- the request path writes into it and
        recreating it per push is pointless churn.
        """
        root = str(self.spool_root)
        now = time.time()
        pruned: set[str] = set()
        for dirpath, dirnames, filenames in os.walk(root, topdown=False):
            if dirpath == root:
                continue
            if filenames:
                continue  # holds a blob (or a .part write in progress)
            # os.walk captured dirnames before we descended, so subdirectories
            # removed lower in this same pass still appear here.
            emptied_child = False
            still_populated = False
            for name in dirnames:
                if os.path.join(dirpath, name) in pruned:
                    emptied_child = True
                else:
                    still_populated = True
            if still_populated:
                continue
            path = Path(dirpath)
            try:
                # Spare a directory a push may be about to write into: ingest
                # mkdirs it and only then opens its .part temp, so it is briefly
                # empty and freshly touched. A directory whose only content was
                # stale subdirectories we just removed is exempt -- its own mtime
                # was bumped by those removals, not by a push.
                if not emptied_child and now - path.stat().st_mtime < _EMPTY_DIR_GRACE_S:
                    continue
                path.rmdir()
                pruned.add(dirpath)
            except OSError as exc:
                # ENOTEMPTY/EEXIST: a blob landed between the walk and the rmdir
                # -- the kernel protected it for us. ENOENT: already gone.
                if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST, errno.ENOENT):
                    logger.debug("could not prune empty spool dir %s: %s", path, exc)
        if pruned:
            logger.info(
                "pruned %d empty spool director%s",
                len(pruned), "y" if len(pruned) == 1 else "ies",
            )
        return len(pruned)

    def _flush_file(self, spool_path: Path) -> bool:
        rel = spool_path.relative_to(self.spool_root)
        # Atomic claim: exactly one worker wins the rename.
        claim = spool_path.with_name(
            f"{spool_path.name}{_CLAIM_SUFFIX}{os.getpid()}-{threading.get_ident()}"
        )
        try:
            os.rename(spool_path, claim)
        except OSError:
            return False  # lost the claim (another flusher) or file vanished
        # The rename keeps the arrival mtime, but _reclaim_orphans times a live
        # sibling's claim by mtime: stamp the claim so an old blob is not
        # mistaken for a stale claim the moment it is taken.
        try:
            os.utime(claim)
        except OSError:
            pass

        archive_rel = self._archive_rel(rel)
        dest = self.archive_root / archive_rel
        if self._before_flush is not None:
            try:
                if self._before_flush(archive_rel, claim) is not None:
                    self._kick.set()
            except Exception:
                logger.exception("blob before_flush hook failed for %s", rel)
        # Unique temp PER ATTEMPT. An abandoned (timed-out) copy keeps writing
        # its own temp, so a retry must never reuse that path -- otherwise two
        # writers interleave into one temp and os.replace could publish a
        # partially-written blob (silent archive corruption: the station
        # already got its 200 at spool time and never re-uploads).
        attempt = _CopyAttempt(
            tmp_rel=str(archive_rel.with_name(_flush_tmp_name(archive_rel.name)))
        )

        def _do_archive() -> bool:
            return self._archive_copy(claim, dest, attempt)

        ok = bool(self._run_guarded(_do_archive, timeout=_FLUSH_TIMEOUT_S, default=False))
        if not ok:
            self._note_failed_attempt(attempt)
            # Return the file to the spool for the next pass, unless a newer
            # push of the same file arrived while this copy was claimed: that
            # one wins, and putting this one back would publish the older
            # content (a re-uploaded FTPdetectinfo after a recalibration).
            try:
                if spool_path.exists():
                    claim.unlink()
                    return False
                os.replace(claim, spool_path)
            except FileNotFoundError:
                # The claim is gone: another flusher's startup scan reclaimed it
                # (and may already have flushed it), or the spool was cleared.
                # Either way this blob is no longer ours and nothing is lost --
                # normal concurrent-flusher bookkeeping, not an error.
                logger.debug("spool file %s was already reclaimed; nothing to un-claim", claim)
            except OSError:
                logger.exception("could not un-claim spool file %s", claim)
            return False

        if self._on_flushed is not None:
            try:
                self._on_flushed(rel, claim)
            except Exception:
                logger.exception("blob on_flushed hook failed for %s", rel)
        try:
            os.unlink(claim)
        except OSError:
            pass
        return True

    # -- archive copy + abandoned-partial bookkeeping ---------------------

    def _archive_rel(self, rel: Path) -> Path:
        """Strip any routing prefix (e.g. the pushing station) so the blob lands
        at its archive-relative path, not under the prefix."""
        return Path(*rel.parts[self._route_prefix_parts:])

    def _ensure_archive_dir(self, directory: Path) -> None:
        """``mkdir -p`` on the archive, once per directory per process. Each
        call is an sshfs round trip, and every blob of a night lands in the
        same few directories. A directory removed behind our back (janitor) is
        handled by ``_archive_copy``'s one retry."""
        key = str(directory)
        if key in self._known_dirs:
            return
        directory.mkdir(parents=True, exist_ok=True)
        if len(self._known_dirs) >= _KNOWN_DIRS_MAX:
            self._known_dirs.clear()
        self._known_dirs.add(key)

    def _archive_copy(self, src: Path, dest: Path, attempt: _CopyAttempt) -> bool:
        """Copy ``src`` onto the archive at ``dest`` via this attempt's unique
        temp. Runs inside the sshfs guard's worker thread, which may be
        abandoned mid-copy; the attempt record lets that thread clean up after
        itself and tells the flusher whether anything was left behind."""
        tmp = self.archive_root / attempt.tmp_rel
        with self._attempts_lock:
            attempt.started = True
            self._inflight[attempt.tmp_rel] = attempt
        clean = False
        try:
            self._ensure_archive_dir(dest.parent)
            merged = _merged_state_bytes(src, dest) if dest.name == "state.json" else None
            try:
                if merged is not None:
                    tmp.write_bytes(merged)
                else:
                    shutil.copyfile(src, tmp)
            except FileNotFoundError:
                if not src.exists():
                    raise
                # The archive directory vanished after we cached it.
                self._known_dirs.discard(str(dest.parent))
                self._ensure_archive_dir(dest.parent)
                if merged is not None:
                    tmp.write_bytes(merged)
                else:
                    shutil.copyfile(src, tmp)
            os.replace(tmp, dest)  # same fs (archive) -> atomic
            clean = True
            return True
        except BaseException:
            clean = _unlink_quietly(tmp)
            raise
        finally:
            self._finish_attempt(attempt, clean=clean)

    def _finish_attempt(self, attempt: _CopyAttempt, *, clean: bool) -> None:
        with self._attempts_lock:
            self._inflight.pop(attempt.tmp_rel, None)
            attempt.finished = True
            attempt.clean = clean
            abandoned = attempt.abandoned
        if abandoned and clean:
            # A late finisher tidied its own temp; nothing left to sweep.
            self._journal.discard(tmp=attempt.tmp_rel)

    def _note_failed_attempt(self, attempt: _CopyAttempt) -> None:
        """Journal a failed attempt's temp only if it may still exist: the copy
        was abandoned while running, or it failed and could not remove it. A
        guard that shed the call before running it left nothing behind."""
        with self._attempts_lock:
            if not attempt.started or (attempt.finished and attempt.clean):
                return
            attempt.abandoned = not attempt.finished
            abandoned = attempt.abandoned
            # Journal under the lock so a finisher racing us sees `abandoned`
            # only after the entry exists, and its discard can't be lost.
            self._journal.add(tmp=attempt.tmp_rel)
        if abandoned:
            logger.warning("archive copy abandoned mid-write; journaled %s", attempt.tmp_rel)

    def _clean_abandoned(self) -> None:
        """Remove journaled archive partials, leaving the temps of copies this
        process is still running alone (they clean up after themselves)."""
        tmps, dests = self._journal.snapshot()
        with self._attempts_lock:
            running = set(self._inflight)
        for tmp_rel in tmps - running:
            path = self.archive_root / tmp_rel
            if self._run_guarded(
                lambda p=path: _unlink_quietly(p), timeout=_FLUSH_TIMEOUT_S, default=False
            ):
                self._journal.discard(tmp=tmp_rel)
        for dest_rel in dests:
            dest = self.archive_root / dest_rel
            if self._run_guarded(
                lambda d=dest: self._sweep_partials(d, running),
                timeout=_FLUSH_TIMEOUT_S, default=False,
            ):
                self._journal.discard(dest=dest_rel)

    def _sweep_partials(self, dest: Path, running: set[str]) -> bool:
        """Glob-sweep ``dest``'s flush temps (one directory listing), sparing
        copies this process is still running. True when the sweep completed."""
        try:
            stale = list(dest.parent.glob(f".{dest.name}{_FLUSH_TMP_MARKER}*"))
        except FileNotFoundError:
            return True
        ok = True
        for path in stale:
            if str(path.relative_to(self.archive_root)) in running:
                continue
            ok = _unlink_quietly(path) and ok
        return ok

    def _run_guarded(self, fn: Callable[[], object], *, timeout: float, default: object):
        """Run ``fn`` under the injected sshfs guard, or directly if none was
        provided (tests)."""
        if self._guard is None:
            try:
                return fn()
            except OSError:
                logger.exception("blob archive write failed")
                return default
        return self._guard(fn, timeout=timeout, default=default)
