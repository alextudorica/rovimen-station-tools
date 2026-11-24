"""Fast worker exit for gunicorn recycles (ROVIMEN_FAST_WORKER_EXIT=1).

Why this exists: at interpreter shutdown CPython joins every
``ThreadPoolExecutor`` thread — they are non-daemon on 3.9+, and
``concurrent.futures`` registers the join via ``threading._register_atexit``.
Several of this app's background executors scan the Storage Box over SSHFS
(archive index ``_scan_night``, thumb prefetch, dome renders); a single
``os.scandir`` against a slow mount blocks that join in an uninterruptible
syscall. When a routine ``max_requests`` recycle lands at such a moment, the
worker cannot finish exiting, the gunicorn master waits out its heartbeat
``timeout`` (120 s) and SIGKILLs it — a multi-minute single-worker blackout.
On the night of 2026-07-06 this produced four ~2-minute outages and ~1,500
browser 504s, all recycle-shaped (root cause proven by py-spy: MainThread in
``concurrent/futures/thread.py:_python_exit -> Thread.join`` behind an SSHFS
``_scan_night``).

The fix: register our own ``threading._register_atexit`` callback. Those
callbacks run LIFO before the executor join, so once the request loop has
drained we flush the two things that actually matter — the activity log and
the logging handlers — and ``os._exit(0)``. The abandoned threads are pure
cache-refresh work that the replacement worker redoes from disk anyway; the
SIGKILL path we are replacing abandoned them too, minus the flush and plus
two minutes of downtime.

Gating: only ``gunicorn.conf.py`` sets ``ROVIMEN_FAST_WORKER_EXIT=1`` (via
``setdefault``, so a unit env file can force ``0`` to opt out). The Flask dev
server, pytest and the E2E launcher keep normal interpreter-shutdown
semantics.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable

logger = logging.getLogger(__name__)

_installed = False
# Local-disk state savers run on a graceful worker exit (e.g. the archive index,
# so the replacement worker boots from the freshest copy). Each must touch only
# local disk and memory -- never the sshfs mount -- because nothing here may
# block the exit this module exists to guarantee.
_exit_flushes: list[tuple[str, Callable[[], None]]] = []


def register_exit_flush(name: str, fn: Callable[[], None]) -> None:
    """Run ``fn`` once on a graceful (fast-exit) worker shutdown."""
    _exit_flushes.append((name, fn))


def run_exit_flushes() -> None:
    """Run every registered saver, isolating failures from each other."""
    for name, fn in list(_exit_flushes):
        try:
            fn()
        except Exception:
            logger.exception("exit flush %s failed", name)


def fast_exit_enabled() -> bool:
    """True when the gunicorn tier opted in via ROVIMEN_FAST_WORKER_EXIT=1."""
    return os.environ.get("ROVIMEN_FAST_WORKER_EXIT", "0") == "1"


def _fast_exit() -> None:
    """threading atexit callback: flush what matters, then exit immediately.

    Runs after the worker's request loop has drained (gunicorn logs "Worker
    exiting" before interpreter shutdown begins) and before
    ``concurrent.futures`` joins executor threads. Every step is best-effort:
    nothing here may block the exit it exists to guarantee.
    """
    run_exit_flushes()
    try:
        # Idempotent, hard 2 s ceiling (see security.flush_activity_log_on_exit).
        from security import flush_activity_log_on_exit

        flush_activity_log_on_exit()
    except Exception:
        pass
    try:
        logging.shutdown()
    except Exception:
        pass
    os._exit(0)


def install_fast_worker_exit() -> bool:
    """Arm the fast-exit hook. Idempotent; returns True when armed.

    No-op (returns False) unless ``ROVIMEN_FAST_WORKER_EXIT=1``, or if this
    Python no longer exposes ``threading._register_atexit`` (private API,
    stable 3.9-3.13 — degrade to normal shutdown rather than crash).
    """
    global _installed
    if _installed:
        return True
    if not fast_exit_enabled():
        return False
    register = getattr(threading, "_register_atexit", None)
    if register is None:
        logger.warning(
            "threading._register_atexit unavailable on this Python; "
            "fast worker exit disabled — recycles may wedge on SSHFS joins"
        )
        return False
    register(_fast_exit)
    _installed = True
    logger.info("fast worker exit armed (ROVIMEN_FAST_WORKER_EXIT=1)")
    return True
