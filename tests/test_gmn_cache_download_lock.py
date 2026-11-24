"""Monthly-file cache: one download per month, whoever asks for it.

The dashboard runs three gunicorn workers and each of them warms the GMN
monthly cache. They all staged their download into the same
``<name>.tmp`` and published it with a rename, so three processes wrote
into one file concurrently and the last rename published the interleaving —
three PIDs were seen downloading the same monthly file within seconds of
each other, and their per-worker orbit tallies then disagreed.

The fix is a private staging path per writer plus an ``flock`` gate so only
one process fetches a given month; the rest wait and reuse the fresh file.
A settled month also gets a longer freshness budget, so a worker recycle no
longer re-pulls ~1 GB of history it already has on disk.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "dashboard"))

import gmn_data  # noqa: E402

BODY = b"# monthly file\n" + b"x" * 65_536


class _FakeResponse:
    """Minimal stand-in for a streaming ``requests`` response."""

    def __init__(self, body: bytes, status_code: int = 200, delay: float = 0.0):
        self._body = body
        self.status_code = status_code
        self._delay = delay

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise AssertionError(f"unexpected status {self.status_code}")

    def iter_content(self, chunk_size: int = 8192):
        for i in range(0, len(self._body), chunk_size):
            if self._delay:
                time.sleep(self._delay)
            yield self._body[i:i + chunk_size]


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    d = tmp_path / "gmn_cache"
    d.mkdir()
    monkeypatch.setattr(gmn_data, "CACHE_DIR", d)
    # Per-month thread locks are process-global; a stale one from another
    # test would silently serialise nothing that matters, but clearing keeps
    # the concurrency assertions honest.
    gmn_data._fetch_locks.clear()
    return d


# The orbit counter warms every month in a trailing 366-day window from a
# background thread that any earlier test can have started, and it reads
# CACHE_DIR at call time — so it lands in whichever cache directory is
# monkeypatched when it gets there. Fetch a month far outside that window
# and these tests are the only writer for it. Months that need the "still
# open" TTL say so by patching _ttl_for_month rather than by using the real
# current month, which is exactly the month the warmer is busy with.
_QUIET_MONTH = (2019, 3)


def _open_month() -> tuple[int, int]:
    """A month nothing else fetches, treated as still open."""
    return _QUIET_MONTH


def _treat_as_open(monkeypatch) -> None:
    """Give the quiet month the freshness budget of a month still in progress."""
    monkeypatch.setattr(
        gmn_data, "_ttl_for_month",
        lambda year, month, now=None: gmn_data.CACHE_TTL_SECONDS,
    )


def _settled_month() -> tuple[int, int]:
    """The same quiet month, taken at its real (long since settled) TTL."""
    return _QUIET_MONTH


def test_staging_path_is_private_to_the_writer(cache_dir):
    target = cache_dir / "traj_summary_monthly_202608.txt"
    tmp = gmn_data._tmp_download_path(target)
    assert tmp.parent == target.parent          # rename stays on one filesystem
    assert str(os.getpid()) in tmp.name
    assert str(threading.get_ident()) in tmp.name
    assert tmp.name.endswith(".tmp")
    assert tmp != target


def test_concurrent_threads_fetch_the_month_once(cache_dir, monkeypatch):
    year, month = _open_month()
    _treat_as_open(monkeypatch)
    calls: list[str] = []
    calls_lock = threading.Lock()

    def _fake_get(url, **kwargs):
        with calls_lock:
            calls.append(url)
        return _FakeResponse(BODY, delay=0.01)

    monkeypatch.setattr(gmn_data.requests, "get", _fake_get)

    results: list[Path] = []
    start = threading.Barrier(4)   # the four workers, so they race properly

    def _worker():
        start.wait(timeout=30)
        results.append(gmn_data.ensure_monthly_cached(year, month))

    threads = [threading.Thread(target=_worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive(), "a worker never finished"

    assert len(calls) == 1, f"downloaded {len(calls)} times, expected 1"
    assert len(results) == 4
    path = results[0]
    assert path.read_bytes() == BODY
    assert not list(cache_dir.glob("*.tmp")), "staging file left behind"


def test_a_failed_download_leaves_no_staging_file(cache_dir, monkeypatch):
    year, month = _open_month()
    _treat_as_open(monkeypatch)

    class _Exploding(_FakeResponse):
        def iter_content(self, chunk_size: int = 8192):
            yield b"partial"
            raise OSError("connection reset mid-stream")

    monkeypatch.setattr(gmn_data.requests, "get", lambda url, **kw: _Exploding(BODY))
    with pytest.raises(OSError):
        gmn_data.ensure_monthly_cached(year, month)
    assert not list(cache_dir.glob("*.tmp"))
    assert not (cache_dir / f"traj_summary_monthly_{year:04d}{month:02d}.txt").exists()


def test_a_second_process_waits_for_the_download_gate(cache_dir, monkeypatch):
    """A worker that finds the gate held reuses the file, it does not refetch.

    A helper process takes the ``flock``, signals that it holds it, then
    publishes the monthly file before releasing. Our request must block on
    the gate and come back with that file — with ``requests.get`` wired to
    blow up, so a second download can't hide behind a passing assertion.
    """
    year, month = _open_month()
    _treat_as_open(monkeypatch)
    path = cache_dir / f"traj_summary_monthly_{year:04d}{month:02d}.txt"
    lock_path = gmn_data._lock_path_for(path)
    ready = cache_dir / "holder-ready"

    holder_body = b"# published by the process holding the gate\n"
    script = textwrap.dedent(
        f"""
        import fcntl, pathlib, time
        lock = open({str(lock_path)!r}, "a+")
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        pathlib.Path({str(ready)!r}).write_text("held")
        time.sleep(1.5)
        pathlib.Path({str(path)!r}).write_bytes({holder_body!r})
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        """
    )
    holder = subprocess.Popen([sys.executable, "-c", script])
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), "helper process never took the lock"

        def _must_not_download(*a, **kw):
            raise AssertionError("downloaded a month another process was fetching")

        monkeypatch.setattr(gmn_data.requests, "get", _must_not_download)
        t0 = time.monotonic()
        got = gmn_data.ensure_monthly_cached(year, month)
        waited = time.monotonic() - t0
    finally:
        holder.wait(timeout=30)

    assert got.read_bytes() == holder_body
    assert waited > 0.2, "returned without waiting for the gate"


def test_a_settled_month_is_not_refetched_after_a_worker_recycle(cache_dir, monkeypatch):
    """The 1 GB re-download guard: an old month on disk is good for a day."""
    year, month = _settled_month()
    path = cache_dir / f"traj_summary_monthly_{year:04d}{month:02d}.txt"
    path.write_bytes(BODY)
    two_hours_ago = time.time() - 2 * 3600
    os.utime(path, (two_hours_ago, two_hours_ago))

    def _must_not_download(*a, **kw):
        raise AssertionError("re-downloaded a settled month that was on disk")

    monkeypatch.setattr(gmn_data.requests, "get", _must_not_download)
    assert gmn_data.ensure_monthly_cached(year, month) == path


def test_an_open_month_still_revalidates_hourly(cache_dir, monkeypatch):
    year, month = _open_month()
    _treat_as_open(monkeypatch)
    path = cache_dir / f"traj_summary_monthly_{year:04d}{month:02d}.txt"
    path.write_bytes(b"stale")
    two_hours_ago = time.time() - 2 * 3600
    os.utime(path, (two_hours_ago, two_hours_ago))

    seen: list[dict] = []

    def _fake_get(url, headers=None, **kw):
        seen.append(headers or {})
        return _FakeResponse(BODY)

    monkeypatch.setattr(gmn_data.requests, "get", _fake_get)
    assert gmn_data.ensure_monthly_cached(year, month).read_bytes() == BODY
    assert len(seen) == 1
    # Conditional GET so an unchanged month costs a 304, not 110 MB.
    assert "If-Modified-Since" in seen[0]


def test_ttl_is_hourly_for_open_months_and_daily_for_settled_ones():
    now = datetime(2026, 8, 25, tzinfo=timezone.utc)
    assert gmn_data._ttl_for_month(2026, 8, now) == gmn_data.CACHE_TTL_SECONDS
    # GMN is still filling in July nights on 25 Aug — the previous month is
    # every bit as open as the current one.
    assert gmn_data._ttl_for_month(2026, 7, now) == gmn_data.CACHE_TTL_SECONDS
    assert gmn_data._ttl_for_month(2026, 6, now) == gmn_data.CLOSED_MONTH_TTL_SECONDS
    assert gmn_data._ttl_for_month(2025, 12, now) == gmn_data.CLOSED_MONTH_TTL_SECONDS


def test_orphaned_staging_files_are_reclaimed_on_the_next_download(cache_dir, monkeypatch):  # noqa: E501
    """A download killed mid-stream (a deploy restart) leaves its staging file.

    Those are per-writer, so without a sweep every interrupted attempt adds
    another half-written ~110 MB file to the cache directory.
    """
    year, month = _open_month()
    _treat_as_open(monkeypatch)
    path = cache_dir / f"traj_summary_monthly_{year:04d}{month:02d}.txt"
    old = time.time() - 3600

    dead_writer = cache_dir / f"{path.name}.99999.123456.tmp"
    legacy = path.with_suffix(".tmp")          # the shared-staging era's name
    other_month = cache_dir / "traj_summary_monthly_202401.txt.99999.1.tmp"
    for f in (dead_writer, legacy, other_month):
        f.write_bytes(b"half a monthly file")
        os.utime(f, (old, old))

    fresh = cache_dir / f"{path.name}.88888.222222.tmp"
    fresh.write_bytes(b"a writer that just started")

    monkeypatch.setattr(gmn_data.requests, "get", lambda url, **kw: _FakeResponse(BODY))
    assert gmn_data.ensure_monthly_cached(year, month).read_bytes() == BODY

    assert not dead_writer.exists(), "orphaned staging file kept"
    assert not legacy.exists(), "legacy shared staging file kept"
    assert fresh.exists(), "swept a staging file that may still be in use"
    assert other_month.exists(), "swept another month's staging file"
