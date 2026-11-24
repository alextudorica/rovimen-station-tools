"""Tests for dashboard/blob_spool.py — the local-disk spool + background flusher
that decouples pushed-blob ingest from the sshfs Storage Box.

Drives flush_once() directly (no thread) for determinism.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import blob_spool


def _spool(spool_root: Path, rel: str, data: bytes) -> Path:
    p = spool_root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _failing_guard(fn, timeout=None, default=None):
    """Simulate a stalled Storage Box: the archive write never succeeds."""
    return default


def test_flush_moves_to_archive_mirroring_the_tree(tmp_path: Path) -> None:
    spool, archive = tmp_path / "spool", tmp_path / "archive"
    bs = blob_spool.BlobSpool(archive, spool_root=spool)  # guard None -> direct
    _spool(spool, "RO000M/20260709/meteors/x.mp4", b"clip-bytes")

    assert bs.flush_once() == 1
    dest = archive / "RO000M" / "20260709" / "meteors" / "x.mp4"
    assert dest.read_bytes() == b"clip-bytes"
    # Spooled copy is removed only after the archive write is durable.
    assert not (spool / "RO000M" / "20260709" / "meteors" / "x.mp4").exists()


def test_root_level_blob_flushes(tmp_path: Path) -> None:
    spool, archive = tmp_path / "spool", tmp_path / "archive"
    bs = blob_spool.BlobSpool(archive, spool_root=spool)
    _spool(spool, "RO000M/20260709/state.json", b"{}")
    assert bs.flush_once() == 1
    assert (archive / "RO000M" / "20260709" / "state.json").read_bytes() == b"{}"


def test_flush_failure_returns_file_to_spool_for_retry(tmp_path: Path) -> None:
    spool, archive = tmp_path / "spool", tmp_path / "archive"
    bs = blob_spool.BlobSpool(archive, spool_root=spool, sshfs_guard=_failing_guard)
    src = _spool(spool, "RO000M/20260709/state.json", b"{}")

    assert bs.flush_once() == 0
    # A stalled box must never lose the blob: it stays spooled, un-claimed, and
    # no archive file was written.
    assert src.exists() and src.read_bytes() == b"{}"
    assert not list(spool.rglob(f"*{blob_spool._CLAIM_SUFFIX}*"))
    assert not (archive / "RO000M").exists()

    # When the box recovers (guard now succeeds), the next pass flushes it.
    bs._guard = None
    assert bs.flush_once() == 1
    assert (archive / "RO000M" / "20260709" / "state.json").read_bytes() == b"{}"


def test_orphaned_claim_is_reclaimed_after_crash(tmp_path: Path) -> None:
    spool, archive = tmp_path / "spool", tmp_path / "archive"
    bs = blob_spool.BlobSpool(archive, spool_root=spool)
    # Simulate a crash mid-flush: a file left in the *.flushing-<pid> state.
    d = spool / "RO000M" / "20260709"
    d.mkdir(parents=True)
    # A PID that is certainly dead: a hardcoded one (9999) is live on some hosts.
    import subprocess
    import sys
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    orphan = d / f"x.mp4{blob_spool._CLAIM_SUFFIX}{proc.pid}-1"
    orphan.write_bytes(b"clip-bytes")

    bs._reclaim_orphans()
    assert (d / "x.mp4").exists() and not orphan.exists()
    assert bs.flush_once() == 1
    assert (archive / "RO000M" / "20260709" / "x.mp4").read_bytes() == b"clip-bytes"


def test_in_flight_partial_writes_are_ignored(tmp_path: Path) -> None:
    spool, archive = tmp_path / "spool", tmp_path / "archive"
    bs = blob_spool.BlobSpool(archive, spool_root=spool)
    d = spool / "RO000M" / "20260709"
    d.mkdir(parents=True)
    (d / ".x.mp4.part-123").write_bytes(b"still-being-written")

    assert bs.flush_once() == 0
    assert not (archive / "RO000M").exists()


def test_is_priority_classifies_meteor_clips_only() -> None:
    p = blob_spool._is_priority
    # Meteor clips (either container) are priority, with or without a routing prefix.
    assert p(Path("RO000M/20260709/meteors/RO000M_20260709_x_color.mkv"))
    assert p(Path("RO000M/20260709/meteors/RO000M_20260709_x_color.mp4"))
    assert p(Path("gmnro10/RO000E/20260803/meteors/clip_color.mkv"))
    # Bulk blobs are NOT priority: stack thumbnails, timelapses, night root files.
    assert not p(Path("RO000M/20260709/stacks/RO000M_20260709_x_stack.webp"))
    assert not p(Path("RO000M/20260709/timelapse/x_timelapse.mp4"))
    assert not p(Path("RO000M/20260709/state.json"))
    # A _color.mkv NOT under meteors/ (defensive: never treat a stray as a clip).
    assert not p(Path("RO000M/20260709/stacks/weird_color.mkv"))


def test_meteor_clips_flush_before_bulk_stack_backlog(tmp_path: Path) -> None:
    """The regression this fix targets: a clip pushed behind a large stack
    backlog must be flushed FIRST, not last. Records the archive-write order and
    asserts every priority clip precedes every bulk blob."""
    spool, archive = tmp_path / "spool", tmp_path / "archive"
    order: list[str] = []

    def _published() -> set[Path]:
        # Only count final published blobs, not the transient .part-flush-* temp
        # a copy writes before os.replace()s it into place.
        return {
            q for q in archive.rglob("*")
            if q.is_file() and ".part-flush-" not in q.name
        }

    def recording_guard(fn, timeout=None, default=None):
        # fn() copies to a temp then os.replace()s onto the archive; capture the
        # order of successful destination publishes.
        before = _published()
        ok = fn()
        for new in sorted(_published() - before):
            order.append(new.name)
        return ok

    bs = blob_spool.BlobSpool(
        archive, spool_root=spool, sshfs_guard=recording_guard, lanes=1
    )
    # Simulate the observed backlog shape: thousands of stack thumbnails plus a
    # couple of fresh meteor clips, spooled in bulk-then-clip order (rglob would
    # otherwise flush the stacks first).
    for i in range(50):
        _spool(spool, f"RO000E/20260803/stacks/s_{i:03d}_stack.webp", b"thumb")
    _spool(spool, "RO000E/20260803/meteors/late_a_color.mkv", b"clip-a")
    _spool(spool, "RO000E/20260803/meteors/late_b_color.mp4", b"clip-b")

    assert bs.flush_once() == 52
    clips = {n for n in order if n.endswith(("_color.mkv", "_color.mp4"))}
    assert clips == {"late_a_color.mkv", "late_b_color.mp4"}
    # Both clips are flushed before ANY stack thumbnail (tier priority; intra-tier
    # order is unspecified / rglob-dependent).
    first_stack = next(i for i, n in enumerate(order) if n.endswith("_stack.webp"))
    last_clip = max(i for i, n in enumerate(order) if n.endswith(("_color.mkv", "_color.mp4")))
    assert last_clip < first_stack


def test_multi_lane_flushes_all_without_double_flush(tmp_path: Path) -> None:
    """With several lanes every blob still flushes exactly once (atomic claim);
    no file is copied twice and none is dropped."""
    spool, archive = tmp_path / "spool", tmp_path / "archive"
    copies: list[str] = []
    lock = __import__("threading").Lock()

    def counting_guard(fn, timeout=None, default=None):
        ok = fn()
        # Record which dest was published on this call (post-replace). Exclude the
        # transient .part-flush-* temp another lane may be mid-copy on.
        with lock:
            for q in archive.rglob("*"):
                if q.is_file() and ".part-flush-" not in q.name and q.name not in copies:
                    copies.append(q.name)
        return ok

    bs = blob_spool.BlobSpool(
        archive, spool_root=spool, sshfs_guard=counting_guard, lanes=4
    )
    for i in range(20):
        _spool(spool, f"RO000E/20260803/stacks/s_{i:02d}_stack.webp", f"t{i}".encode())

    assert bs.flush_once() == 20
    # Every source flushed, archive has exactly 20 files, none left in spool.
    assert len(list((archive).rglob("*_stack.webp"))) == 20
    assert not [q for q in spool.rglob("*") if q.is_file()]
    # No file appeared twice in the archive (unique names, unique claims).
    assert len(copies) == 20 == len(set(copies))


def test_lanes_env_override_defaults() -> None:
    # The module default lane count is env-driven and always >= 1.
    assert blob_spool._FLUSH_LANES >= 1


def test_spool_dir_env_override(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ROVIMEN_BLOB_SPOOL_DIR", str(tmp_path / "custom"))
    assert blob_spool.spool_dir() == tmp_path / "custom"


def test_spool_dir_defaults_beside_cache_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("ROVIMEN_BLOB_SPOOL_DIR", raising=False)
    monkeypatch.setenv("ROVIMEN_CACHE_PATH", str(tmp_path / "opt" / "cache"))
    # Tier-correct: a sibling of the cache dir, so the dev tier lands under
    # /opt/rovimen-dev automatically.
    assert blob_spool.spool_dir() == tmp_path / "opt" / "blob_spool"


# ── concurrent flushers (multi-worker gunicorn) ─────────────────────────────
#
# Every gunicorn worker runs its own flusher and workers recycle on a request
# cadence, so a fresh worker's startup reclaim scan runs while sibling workers
# are mid-flush. These cover that overlap: the scan must not steal a live
# claim, and a flusher that loses its claim anyway must stay quiet.


def _live_sibling_pid() -> int:
    """A PID that is certainly alive but is not this process. PID 1 always
    exists; ``os.kill(1, 0)`` either succeeds (root) or raises PermissionError,
    both of which mean "alive"."""
    return 1


def test_reclaim_scan_leaves_a_live_siblings_claim_alone(tmp_path: Path) -> None:
    spool, archive = tmp_path / "spool", tmp_path / "archive"
    bs = blob_spool.BlobSpool(archive, spool_root=spool)
    d = spool / "RO000M" / "20260709"
    d.mkdir(parents=True)
    claim = d / f"x.mp4{blob_spool._CLAIM_SUFFIX}{_live_sibling_pid()}-7"
    claim.write_bytes(b"clip-bytes")

    bs._reclaim_orphans()

    # Still claimed: another worker is flushing it right now. Stealing it would
    # double-flush the blob and break the owner's un-claim.
    assert claim.exists()
    assert not (d / "x.mp4").exists()
    assert bs.flush_once() == 0


def test_reclaim_scan_takes_a_live_claim_once_it_is_stale(tmp_path: Path, monkeypatch) -> None:
    spool, archive = tmp_path / "spool", tmp_path / "archive"
    bs = blob_spool.BlobSpool(archive, spool_root=spool)
    d = spool / "RO000M" / "20260709"
    d.mkdir(parents=True)
    claim = d / f"x.mp4{blob_spool._CLAIM_SUFFIX}{_live_sibling_pid()}-7"
    claim.write_bytes(b"clip-bytes")
    # A flusher wedged on a stalled box must not strand the blob forever.
    monkeypatch.setattr(blob_spool, "_CLAIM_STALE_S", -1.0)

    bs._reclaim_orphans()
    assert (d / "x.mp4").exists() and not claim.exists()
    assert bs.flush_once() == 1


def test_reclaim_scan_leaves_a_fresh_claim_of_an_old_blob_alone(tmp_path: Path) -> None:
    """A claim's age must run from the claim, not from the blob's arrival.

    The claim rename keeps the file's mtime, so a blob that waited an hour in
    the spool (Storage Box down) looked like an hour-old claim the moment a
    worker claimed it, and a sibling worker booting during the copy un-claimed
    it: a double flush, and the owner's un-claim failing.
    """
    spool, archive = tmp_path / "spool", tmp_path / "archive"
    src = _spool(spool, "RO000M/20260709/x.mp4", b"clip-bytes")
    arrived = time.time() - 3600
    os.utime(src, (arrived, arrived))
    seen: dict[str, bool] = {}

    def _sibling_worker_boots(_archive_rel: Path, claimed: Path) -> None:
        # A real second process, so this flusher is a live sibling to it.
        code = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(Path(blob_spool.__file__).parent)!r})
            from pathlib import Path
            import blob_spool
            blob_spool.BlobSpool(Path({str(archive)!r}),
                                 spool_root=Path({str(spool)!r}))._reclaim_orphans()
        """)
        subprocess.run([sys.executable, "-c", code], check=True, timeout=60)
        seen["claim_kept"] = claimed.exists()

    bs = blob_spool.BlobSpool(archive, spool_root=spool, before_flush=_sibling_worker_boots)
    assert bs.flush_once() == 1
    assert seen["claim_kept"], "a sibling's startup scan stole a claim seconds old"
    assert (archive / "RO000M" / "20260709" / "x.mp4").read_bytes() == b"clip-bytes"
    assert not list(spool.rglob("x.mp4*"))


def test_reclaim_scan_still_recovers_a_dead_flushers_claim(tmp_path: Path) -> None:
    spool, archive = tmp_path / "spool", tmp_path / "archive"
    bs = blob_spool.BlobSpool(archive, spool_root=spool)
    d = spool / "RO000M" / "20260709"
    d.mkdir(parents=True)
    # PID 0 never owns a process, so this claim's flusher is unambiguously gone.
    orphan = d / f"x.mp4{blob_spool._CLAIM_SUFFIX}0-1"
    orphan.write_bytes(b"clip-bytes")

    bs._reclaim_orphans()
    assert (d / "x.mp4").exists() and not orphan.exists()


def test_claim_owner_pid_parses_and_tolerates_junk() -> None:
    assert blob_spool._claim_owner_pid(f"x.mp4{blob_spool._CLAIM_SUFFIX}1234-99") == 1234
    assert blob_spool._claim_owner_pid(f"x.mp4{blob_spool._CLAIM_SUFFIX}nope-99") is None
    assert blob_spool._claim_owner_pid("x.mp4") is None


def test_losing_the_claim_mid_flush_is_debug_not_error(tmp_path: Path, caplog) -> None:
    """The prod symptom: a sibling worker reclaims our claim while our archive
    write is in flight, so the un-claim rename hits FileNotFoundError. Nothing
    is lost, so it must not be logged as an ERROR."""
    import logging

    spool, archive = tmp_path / "spool", tmp_path / "archive"
    src = _spool(spool, "RO000M/20260709/state.json", b"{}")

    def _guard_that_loses_the_claim(fn, timeout=None, default=None):
        # Simulate the sibling's startup scan un-claiming the file under us.
        for claimed in spool.rglob(f"*{blob_spool._CLAIM_SUFFIX}*"):
            claimed.rename(claimed.with_name(claimed.name.split(blob_spool._CLAIM_SUFFIX, 1)[0]))
        return default  # and the archive write failed too

    bs = blob_spool.BlobSpool(archive, spool_root=spool, sshfs_guard=_guard_that_loses_the_claim)
    with caplog.at_level(logging.DEBUG, logger="blob_spool"):
        assert bs.flush_once() == 0

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not errors, [r.getMessage() for r in errors]
    assert any("already reclaimed" in r.getMessage() for r in caplog.records)
    # The blob itself survived -- the sibling now owns it.
    assert src.exists() and src.read_bytes() == b"{}"


def test_two_racing_flushers_flush_each_blob_exactly_once(tmp_path: Path, caplog) -> None:
    """Two processes' flushers overlapping on one spool: every blob lands in the
    archive exactly once and neither flusher logs an error."""
    import logging
    import threading as _threading

    spool, archive = tmp_path / "spool", tmp_path / "archive"
    for i in range(25):
        _spool(spool, f"RO000M/20260709/meteors/c{i}_color.mp4", f"clip-{i}".encode())

    copies: list[str] = []
    copies_lock = _threading.Lock()
    real_flush_file = blob_spool.BlobSpool._flush_file

    def _counting_flush_file(self, path):
        ok = real_flush_file(self, path)
        if ok:
            with copies_lock:
                copies.append(path.name)
        return ok

    bs_a = blob_spool.BlobSpool(archive, spool_root=spool, lanes=4)
    bs_b = blob_spool.BlobSpool(archive, spool_root=spool, lanes=4)
    bs_a._flush_file = _counting_flush_file.__get__(bs_a)
    bs_b._flush_file = _counting_flush_file.__get__(bs_b)

    start = _threading.Barrier(2)
    results: dict[str, int] = {}

    def _drain(name, bs):
        start.wait()
        results[name] = bs.flush_once()

    with caplog.at_level(logging.DEBUG, logger="blob_spool"):
        threads = [
            _threading.Thread(target=_drain, args=("a", bs_a)),
            _threading.Thread(target=_drain, args=("b", bs_b)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    # Every blob flushed, none twice, none lost.
    assert sorted(copies) == sorted(f"c{i}_color.mp4" for i in range(25))
    assert results["a"] + results["b"] == 25
    for i in range(25):
        dest = archive / "RO000M" / "20260709" / "meteors" / f"c{i}_color.mp4"
        assert dest.read_bytes() == f"clip-{i}".encode()
    assert not list(spool.rglob(f"*{blob_spool._CLAIM_SUFFIX}*"))
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not errors, [r.getMessage() for r in errors]


# ── empty-directory pruning ────────────────────────────────────────────────


def test_flush_prunes_the_directories_it_empties(tmp_path: Path, monkeypatch) -> None:
    spool, archive = tmp_path / "spool", tmp_path / "archive"
    monkeypatch.setattr(blob_spool, "_EMPTY_DIR_GRACE_S", -1.0)
    bs = blob_spool.BlobSpool(archive, spool_root=spool)
    _spool(spool, "RO000M/20260709/meteors/x_color.mp4", b"clip")

    assert bs.flush_once() == 1
    # The whole drained tree is gone; the spool root itself stays.
    assert not (spool / "RO000M").exists()
    assert spool.is_dir()


def test_pruning_never_removes_a_directory_holding_a_blob(tmp_path: Path, monkeypatch) -> None:
    spool, archive = tmp_path / "spool", tmp_path / "archive"
    monkeypatch.setattr(blob_spool, "_EMPTY_DIR_GRACE_S", -1.0)
    bs = blob_spool.BlobSpool(archive, spool_root=spool, sshfs_guard=_failing_guard)
    src = _spool(spool, "RO000M/20260709/state.json", b"{}")
    # An unrelated empty tree from an earlier drained night.
    (spool / "RO000N" / "20260708" / "stacks").mkdir(parents=True)
    # A directory holding only an in-flight .part write must survive too.
    part_dir = spool / "RO000P" / "20260709"
    part_dir.mkdir(parents=True)
    part = part_dir / ".x.mp4.part-1-2"
    part.write_bytes(b"half")

    assert bs.flush_once() == 0

    assert src.exists() and src.read_bytes() == b"{}"
    assert part.exists()
    assert not (spool / "RO000N").exists()


def test_pruning_spares_directories_a_push_may_be_writing_into(tmp_path: Path) -> None:
    """Ingest mkdirs the target directory and only then opens its .part temp, so
    a just-created empty directory must not be pruned out from under it."""
    spool, archive = tmp_path / "spool", tmp_path / "archive"
    bs = blob_spool.BlobSpool(archive, spool_root=spool)
    fresh = spool / "RO000M" / "20260709" / "meteors"
    fresh.mkdir(parents=True)

    assert bs.flush_once() == 0
    assert fresh.is_dir()  # inside the grace window -> left alone


def _part(dirpath: Path, name: str, pid: int, age_s: float) -> Path:
    import os
    import time
    dirpath.mkdir(parents=True, exist_ok=True)
    p = dirpath / f".{name}.part-{pid}-140000000"
    p.write_bytes(b"partial")
    t = time.time() - age_s
    os.utime(p, (t, t))
    return p


def _dead_pid() -> int:
    import subprocess
    import sys
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_abandoned_part_of_a_dead_writer_is_reaped(tmp_path: Path) -> None:
    spool = tmp_path / "spool"
    night = spool / "gmnro02" / "RO000M" / "20260906" / "meteors"
    old = _part(night, "RO000M_20260906_224250_color.mp4", _dead_pid(), 7200)
    bs = blob_spool.BlobSpool(tmp_path / "archive", spool_root=spool)
    assert bs._reap_abandoned_parts() == 1
    assert not old.exists()


def test_part_of_a_live_writer_or_a_recent_part_is_kept(tmp_path: Path) -> None:
    import os
    spool = tmp_path / "spool"
    night = spool / "gmnro02" / "RO000M" / "20260906" / "meteors"
    live = _part(night, "a_color.mp4", os.getpid(), 7200)   # writer alive
    fresh = _part(night, "b_color.mp4", _dead_pid(), 60)    # dead but recent
    committed = night / "c_color.mp4"
    committed.write_bytes(b"done")
    bs = blob_spool.BlobSpool(tmp_path / "archive", spool_root=spool)
    assert bs._reap_abandoned_parts() == 0
    assert live.exists() and fresh.exists() and committed.exists()


# -- abandoned-copy journal (sweep only what was actually abandoned) --------


def _abandoning_guard(release: threading.Event, started: threading.Event, calls: dict):
    """A guard whose FIRST call behaves like ``_with_sshfs_timeout`` on a
    stalled box: it runs the copy in a thread, gives up after a moment and
    returns ``default`` while that thread keeps going. Later calls run inline."""

    def guard(fn, timeout=None, default=None):
        calls["n"] += 1
        if calls["n"] > 1:
            return fn()
        t = threading.Thread(target=lambda: _swallow(fn), daemon=True)
        calls["thread"] = t
        t.start()
        started.wait(5)
        return default

    return guard


def _swallow(fn) -> None:
    try:
        fn()
    except Exception:
        pass


def _stalling_copyfile(real, release: threading.Event, started: threading.Event, fail: bool):
    """copyfile that writes a partial temp, then blocks until released (the
    stalled sshfs write) and either fails or finishes."""
    state = {"first": True}

    def copy(src, dst):
        if not state["first"]:
            return real(src, dst)
        state["first"] = False
        Path(dst).write_bytes(b"PARTIAL")
        started.set()
        release.wait(5)
        if fail:
            raise OSError("sshfs: connection reset")
        return real(src, dst)

    return copy


def _journals(spool: Path) -> list[Path]:
    return sorted(spool.glob(".abandoned-copies-*"))


def test_retry_after_abandoned_copy_publishes_full_blob_and_cleans_partial(
    tmp_path: Path, monkeypatch
) -> None:
    """A timed-out copy is abandoned mid-write, leaving a partial temp on the
    archive; the retry must publish the FULL blob (unique temp per attempt), and
    the abandoned copy's partial must be cleaned up once that copy gives up.
    Regression for the silent-corruption race (station already 200'd at spool
    time)."""
    spool, archive = tmp_path / "spool", tmp_path / "archive"
    release, started, calls = threading.Event(), threading.Event(), {"n": 0}
    monkeypatch.setattr(
        blob_spool.shutil, "copyfile",
        _stalling_copyfile(blob_spool.shutil.copyfile, release, started, fail=True),
    )
    bs = blob_spool.BlobSpool(
        archive, spool_root=spool, sshfs_guard=_abandoning_guard(release, started, calls)
    )
    full = b"full-clip-bytes" * 1000
    _spool(spool, "RO000M/20260709/meteors/x.mp4", full)

    assert bs.flush_once() == 0  # first pass "times out"
    assert (spool / "RO000M" / "20260709" / "meteors" / "x.mp4").exists()
    # The abandoned attempt is journaled on LOCAL disk so a restart can't forget it.
    (journal,) = _journals(spool)
    assert ".part-flush-" in journal.read_text()

    assert bs.flush_once() == 1  # retry succeeds on its own unique temp
    dest = archive / "RO000M" / "20260709" / "meteors" / "x.mp4"
    assert dest.read_bytes() == full

    release.set()  # the stalled copy finally errors out and removes its temp
    calls["thread"].join(5)
    assert dest.read_bytes() == full  # FULL bytes, never the partial
    assert not list(dest.parent.glob(".*.part-flush-*"))
    assert not _journals(spool)


def test_abandoned_copy_that_leaves_its_temp_is_unlinked_by_the_next_pass(
    tmp_path: Path, monkeypatch
) -> None:
    """If the abandoned copy dies without removing its temp, the journal names
    the exact file and the next pass unlinks it -- no directory listing."""
    spool, archive = tmp_path / "spool", tmp_path / "archive"
    release, started, calls = threading.Event(), threading.Event(), {"n": 0}
    monkeypatch.setattr(
        blob_spool.shutil, "copyfile",
        _stalling_copyfile(blob_spool.shutil.copyfile, release, started, fail=True),
    )
    monkeypatch.setattr(blob_spool, "_unlink_quietly", _unlink_fails_once(blob_spool._unlink_quietly))
    bs = blob_spool.BlobSpool(
        archive, spool_root=spool, sshfs_guard=_abandoning_guard(release, started, calls)
    )
    _spool(spool, "RO000M/20260709/stacks/s_stack.webp", b"thumb")

    assert bs.flush_once() == 0
    release.set()
    calls["thread"].join(5)
    stacks = archive / "RO000M" / "20260709" / "stacks"
    assert list(stacks.glob(".*.part-flush-*"))  # its own cleanup failed

    globbed = _count_archive_globs(monkeypatch, archive)
    assert bs.flush_once() == 1
    assert globbed["n"] == 0
    assert (stacks / "s_stack.webp").read_bytes() == b"thumb"
    assert not list(stacks.glob(".*.part-flush-*"))
    assert not _journals(spool)


def _unlink_fails_once(real):
    state = {"first": True}

    def unlink(path: Path) -> bool:
        if state["first"]:
            state["first"] = False
            return False
        return real(path)

    return unlink


def _count_archive_globs(monkeypatch, archive: Path) -> dict:
    counter = {"n": 0}
    real_glob = Path.glob

    def counting_glob(self, pattern, *args, **kwargs):
        if str(self).startswith(str(archive)):
            counter["n"] += 1
        return real_glob(self, pattern, *args, **kwargs)

    monkeypatch.setattr(Path, "glob", counting_glob)
    return counter


def test_healthy_flushes_never_list_the_archive_and_mkdir_once_per_dir(
    tmp_path: Path, monkeypatch
) -> None:
    """The hot path: a night's blobs flushing normally must not glob the
    (thousands-of-entries, sshfs) night directory, and must create each archive
    directory once per process rather than once per blob."""
    spool, archive = tmp_path / "spool", tmp_path / "archive"
    globbed = _count_archive_globs(monkeypatch, archive)
    mkdirs: list[str] = []
    real_mkdir = Path.mkdir

    def counting_mkdir(self, *args, **kwargs):
        if str(self).startswith(str(archive)):
            mkdirs.append(str(self))
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", counting_mkdir)
    bs = blob_spool.BlobSpool(archive, spool_root=spool, lanes=1)
    for i in range(10):
        _spool(spool, f"RO000M/20260709/stacks/s_{i}_stack.webp", b"t")
    assert bs.flush_once() == 10
    # One mkdir -p for the whole night (it recurses into missing parents once).
    assert len(mkdirs) <= 8
    mkdirs.clear()
    for i in range(10, 15):
        _spool(spool, f"RO000M/20260709/stacks/s_{i}_stack.webp", b"t")
    assert bs.flush_once() == 5

    assert globbed["n"] == 0
    assert mkdirs == []
    assert not _journals(spool)


def test_archive_dir_removed_after_caching_is_recreated(tmp_path: Path) -> None:
    import shutil as _shutil

    spool, archive = tmp_path / "spool", tmp_path / "archive"
    bs = blob_spool.BlobSpool(archive, spool_root=spool)
    _spool(spool, "RO000M/20260709/stacks/a_stack.webp", b"a")
    assert bs.flush_once() == 1
    _shutil.rmtree(archive / "RO000M")  # e.g. the janitor pruned the night
    _spool(spool, "RO000M/20260709/stacks/b_stack.webp", b"b")
    assert bs.flush_once() == 1
    assert (archive / "RO000M" / "20260709" / "stacks" / "b_stack.webp").read_bytes() == b"b"


def test_crash_mid_flush_sweeps_that_one_dest(tmp_path: Path) -> None:
    """A flusher that died mid-copy left a claim in the spool and a partial of
    unknown name on the archive. Reclaiming the claim journals that dest, and the
    next pass sweeps its partials once."""
    spool, archive = tmp_path / "spool", tmp_path / "archive"
    d = spool / "gmnro02" / "RO000M" / "20260709" / "meteors"
    d.mkdir(parents=True)
    (d / f"x_color.mp4{blob_spool._CLAIM_SUFFIX}{_dead_pid()}-1").write_bytes(b"clip")
    meteors = archive / "RO000M" / "20260709" / "meteors"
    meteors.mkdir(parents=True)
    (meteors / ".x_color.mp4.part-flush-deadbeef").write_bytes(b"PART")
    (meteors / ".y_color.mp4.part-flush-live").write_bytes(b"other dest")

    bs = blob_spool.BlobSpool(archive, spool_root=spool, route_prefix_parts=1)
    bs._reclaim_orphans()
    assert len(_journals(spool)) == 1
    assert bs.flush_once() == 1

    assert (meteors / "x_color.mp4").read_bytes() == b"clip"
    assert not (meteors / ".x_color.mp4.part-flush-deadbeef").exists()
    assert (meteors / ".y_color.mp4.part-flush-live").exists()  # not this dest
    assert not _journals(spool)


def test_dead_process_journal_is_adopted_and_cleaned(tmp_path: Path) -> None:
    spool, archive = tmp_path / "spool", tmp_path / "archive"
    spool.mkdir()
    stacks = archive / "RO000M" / "20260709" / "stacks"
    stacks.mkdir(parents=True)
    leftover = stacks / ".s_stack.webp.part-flush-abc"
    leftover.write_bytes(b"PART")
    import json

    dead = spool / f".abandoned-copies-{_dead_pid()}.json"
    dead.write_text(json.dumps({"tmps": [str(leftover.relative_to(archive))], "dests": []}))

    bs = blob_spool.BlobSpool(archive, spool_root=spool)
    assert bs._journal.adopt_dead(blob_spool._pid_alive) == 1
    assert not dead.exists()
    bs.flush_once()
    assert not leftover.exists()
    assert not _journals(spool)


def test_live_process_journal_is_left_alone(tmp_path: Path) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    live = spool / f".abandoned-copies-{_live_sibling_pid()}.json"
    live.write_text('{"tmps": ["a/b/.c.part-flush-1"], "dests": []}')
    bs = blob_spool.BlobSpool(tmp_path / "archive", spool_root=spool)
    assert bs._journal.adopt_dead(blob_spool._pid_alive) == 0
    assert live.exists()


def test_corrupt_journal_is_tolerated(tmp_path: Path) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    bad = spool / f".abandoned-copies-{_dead_pid()}.json"
    bad.write_text("{not json")
    bs = blob_spool.BlobSpool(tmp_path / "archive", spool_root=spool)
    assert bs._journal.adopt_dead(blob_spool._pid_alive) == 0
    assert not bs._journal
    assert not _journals(spool)


def test_shed_guard_journals_nothing(tmp_path: Path) -> None:
    """A guard that sheds the call before running it wrote nothing to the
    archive, so there is nothing to clean and no journal is written."""
    spool, archive = tmp_path / "spool", tmp_path / "archive"
    bs = blob_spool.BlobSpool(archive, spool_root=spool, sshfs_guard=_failing_guard)
    _spool(spool, "RO000M/20260709/state.json", b"{}")
    assert bs.flush_once() == 0
    assert not _journals(spool)


def test_journal_files_are_never_flushed_as_blobs(tmp_path: Path) -> None:
    spool, archive = tmp_path / "spool", tmp_path / "archive"
    spool.mkdir()
    (spool / f".abandoned-copies-{_live_sibling_pid()}.json").write_text("{}")
    bs = blob_spool.BlobSpool(archive, spool_root=spool)
    assert bs.flush_once() == 0
    assert not archive.exists()


# ── state.json merge: what the box knows and the station doesn't ────────────
# The station re-uploads its own state.json whenever it changes, so a plain
# overwrite erased the retention janitor's video_pruned marks, and the
# station's dawn sweep (lock -> null after 7 days) erased the only record that
# a clip was a detection or a manual lock.

import json  # noqa: E402


def _state(chunks: dict) -> bytes:
    return json.dumps({"chunks": chunks}).encode()


def test_state_json_upload_keeps_prune_marks_and_the_last_lock(tmp_path: Path) -> None:
    spool, archive = tmp_path / "spool", tmp_path / "archive"
    night = archive / "RO000M" / "20260906"
    night.mkdir(parents=True)
    (night / "state.json").write_bytes(_state({
        "A_color.mkv": {"lock": {"lock_type": "manual"}, "video_pruned": True,
                        "video_pruned_at": "2026-09-20T04:31:00+00:00"},
        "B_color.mkv": {"lock": {"lock_type": "detection"}},
    }))
    bs = blob_spool.BlobSpool(archive, spool_root=spool)
    # The station's copy after its dawn sweep: locks cleared, no prune marks.
    _spool(spool, "RO000M/20260906/state.json", _state({
        "A_color.mkv": {"lock": None, "uploaded": True},
        "B_color.mkv": {"lock": None},
        "C_color.mkv": {"lock": {"lock_type": "detection"}},
    }))
    assert bs.flush_once() == 1
    chunks = json.loads((night / "state.json").read_text())["chunks"]
    assert chunks["A_color.mkv"]["lock"] is None                      # station's view wins
    assert chunks["A_color.mkv"]["uploaded"] is True
    assert chunks["A_color.mkv"]["video_pruned"] is True             # janitor's mark kept
    assert chunks["A_color.mkv"]["archived_lock"] == {"lock_type": "manual"}
    assert chunks["B_color.mkv"]["archived_lock"] == {"lock_type": "detection"}
    assert chunks["C_color.mkv"]["archived_lock"] == {"lock_type": "detection"}


def test_unparseable_state_json_is_copied_unchanged(tmp_path: Path) -> None:
    spool, archive = tmp_path / "spool", tmp_path / "archive"
    bs = blob_spool.BlobSpool(archive, spool_root=spool)
    _spool(spool, "RO000M/20260906/state.json", b"{not json")
    assert bs.flush_once() == 1
    assert (archive / "RO000M" / "20260906" / "state.json").read_bytes() == b"{not json"


def test_merge_state_json_keeps_an_archived_lock_across_repeated_sweeps() -> None:
    first = blob_spool.merge_state_json(
        {"chunks": {"A": {"lock": None}}}, {"chunks": {"A": {"lock": {"lock_type": "manual"}}}})
    assert first["chunks"]["A"]["archived_lock"] == {"lock_type": "manual"}
    second = blob_spool.merge_state_json({"chunks": {"A": {"lock": None}}}, first)
    assert second["chunks"]["A"]["archived_lock"] == {"lock_type": "manual"}


def test_failed_flush_does_not_overwrite_a_newer_push(tmp_path: Path) -> None:
    """A re-upload that lands while the older copy is claimed (a recalibrated
    FTPdetectinfo) must not be replaced by the older copy when its flush fails."""
    spool, archive = tmp_path / "spool", tmp_path / "archive"
    rel = "RO000M/20260709/rms/FTPdetectinfo_x.txt"

    def guard_with_newer_push(fn, timeout=None, default=None):
        _spool(spool, rel, b"new-astrometry")   # the station re-POSTs meanwhile
        return default                          # the box stalls: flush fails

    bs = blob_spool.BlobSpool(archive, spool_root=spool, sshfs_guard=guard_with_newer_push)
    _spool(spool, rel, b"old-astrometry")
    assert bs.flush_once() == 0
    assert (spool / rel).read_bytes() == b"new-astrometry"
    assert not list(spool.rglob(f"*{blob_spool._CLAIM_SUFFIX}*"))

    bs._guard = None
    assert bs.flush_once() == 1
    assert (archive / rel).read_bytes() == b"new-astrometry"
