"""Global SSHFS concurrency bound in station_client._with_sshfs_timeout.

A fan-out burst (e.g. the highlights range: ~10 dates x ~15 cameras) must not
spawn ~150 threads all hammering the single storage-box mount. At most
_SSHFS_MAX_CONCURRENCY storage-box ops run at once; excess callers fail fast to
*default* (degrade) instead of piling up on the worker's thread pool.
"""

from __future__ import annotations

import threading

import station_client as sc


def test_fails_fast_to_default_when_saturated(monkeypatch):
    sem = threading.BoundedSemaphore(1)
    monkeypatch.setattr(sc, "_sshfs_semaphore", sem)
    monkeypatch.setattr(sc, "_SSHFS_ACQUIRE_TIMEOUT", 0.1)

    sem.acquire()  # the one permit is taken -> mount "saturated"
    ran: list[int] = []
    result = sc._with_sshfs_timeout(
        lambda: (ran.append(1), "value")[1], timeout=1.0, default="DEGRADED"
    )
    assert result == "DEGRADED"          # degraded, not queued
    assert ran == []                     # fn never ran (no thread spawned)
    sem.release()


def test_permit_released_after_op_completes(monkeypatch):
    sem = threading.BoundedSemaphore(1)
    monkeypatch.setattr(sc, "_sshfs_semaphore", sem)

    # Run several ops through the single permit serially — each must release so
    # the next can acquire. If release were missing, the 2nd call would degrade.
    for _ in range(4):
        assert sc._with_sshfs_timeout(lambda: "ok", timeout=1.0, default=None) == "ok"

    # Permit is back and free.
    assert sem.acquire(timeout=0.2)
    sem.release()


def test_leaked_op_keeps_its_permit_until_it_finishes(monkeypatch):
    """A timed-out (leaked) op must hold its permit until it truly finishes, so
    the bound reflects real mount load — the next caller degrades meanwhile."""
    sem = threading.BoundedSemaphore(1)
    monkeypatch.setattr(sc, "_sshfs_semaphore", sem)
    monkeypatch.setattr(sc, "_SSHFS_ACQUIRE_TIMEOUT", 0.1)

    gate = threading.Event()

    def slow():
        gate.wait(5)   # blocks past the op timeout -> leaks
        return "late"

    # First call times out (leaks) but the runner thread still holds the permit.
    assert sc._with_sshfs_timeout(slow, timeout=0.2, default="TIMEDOUT") == "TIMEDOUT"
    # Second call can't get a permit -> degrades immediately.
    assert sc._with_sshfs_timeout(lambda: "x", timeout=1.0, default="DEG") == "DEG"

    # Let the leaked op finish; it releases the permit.
    gate.set()
    freed = False
    for _ in range(50):
        if sem.acquire(timeout=0.1):
            freed = True
            sem.release()
            break
    assert freed


# ── Dedicated flush lane ─────────────────────────────────────────────────
# The blob-spool flusher runs on its own lane so a read-traffic burst can't
# starve it into a spool livelock (the gmnro02 disk-fill incident).


def test_flush_lane_runs_and_returns():
    assert sc.with_sshfs_flush_timeout(lambda: "ok", timeout=1.0, default=None) == "ok"


def test_flush_lane_independent_of_saturated_read_lane(monkeypatch):
    # Read lane fully saturated (all permits taken) ...
    read_sem = threading.BoundedSemaphore(1)
    monkeypatch.setattr(sc, "_sshfs_semaphore", read_sem)
    monkeypatch.setattr(sc, "_SSHFS_ACQUIRE_TIMEOUT", 0.1)
    read_sem.acquire()
    # ... a read degrades ...
    assert sc._with_sshfs_timeout(lambda: "R", timeout=1.0, default="DEG") == "DEG"
    # ... but the flusher, on its own lane, still runs.
    ran: list[int] = []
    assert sc.with_sshfs_flush_timeout(
        lambda: (ran.append(1), "F")[1], timeout=1.0, default=None
    ) == "F"
    assert ran == [1]
    read_sem.release()


def test_flush_lane_defers_when_its_own_lane_saturated(monkeypatch):
    flush_sem = threading.BoundedSemaphore(1)
    monkeypatch.setattr(sc, "_flush_sshfs_semaphore", flush_sem)
    monkeypatch.setattr(sc, "_FLUSH_SSHFS_ACQUIRE_TIMEOUT", 0.1)
    flush_sem.acquire()  # its one permit is taken
    ran: list[int] = []
    result = sc.with_sshfs_flush_timeout(
        lambda: (ran.append(1), "F")[1], timeout=1.0, default="DEFERRED"
    )
    assert result == "DEFERRED"   # deferred to next pass, not queued forever
    assert ran == []              # fn never ran
    flush_sem.release()


def test_shed_warning_names_the_permit_holders(monkeypatch, caplog):
    """A shed must say who holds the lane, or the log cannot route the fix."""
    sem = threading.BoundedSemaphore(1)
    monkeypatch.setattr(sc, "_sshfs_semaphore", sem)
    monkeypatch.setattr(sc, "_SSHFS_ACQUIRE_TIMEOUT", 0.1)
    monkeypatch.setattr(sc, "_sshfs_shed_last_log", float("-inf"))
    gate = threading.Event()

    def slow_holder():
        return sc._with_sshfs_timeout(lambda: gate.wait(5), timeout=5.0, default=None)

    t = threading.Thread(target=slow_holder)
    t.start()
    for _ in range(50):
        if sc._sshfs_inflight:
            break
        threading.Event().wait(0.02)

    def shed_caller():
        return sc._with_sshfs_timeout(lambda: "x", timeout=1.0, default="DEG")

    with caplog.at_level("WARNING", logger="station_client"):
        assert shed_caller() == "DEG"
        assert shed_caller() == "DEG"  # inside the 10 s window: suppressed
    gate.set()
    t.join(5)

    msgs = [r.getMessage() for r in caplog.records if "concurrency limit" in r.getMessage()]
    assert len(msgs) == 1
    assert "shed_caller@test_sshfs_concurrency.py" in msgs[0]
    assert "holders: slow_holder@test_sshfs_concurrency.py" in msgs[0]
    assert not sc._sshfs_inflight
