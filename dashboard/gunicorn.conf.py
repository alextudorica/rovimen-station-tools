"""Gunicorn config for the ROVIMEN dashboard (prod + dev tiers).

Lives in ``dashboard/`` so it is rsynced flat to ``/opt/rovimen[-dev]/`` by the
deploy workflow alongside the app code, and is loaded via
``gunicorn --config /opt/rovimen[-dev]/gunicorn.conf.py``. Everything here is
overridable by env so the same file serves both tiers and the worker count can
be raised without editing the systemd unit.

Multi-worker scaling (see docs/reversed_http_push_design.md §9):
  * ``GUNICORN_WORKERS``  — number of worker processes. Default **1**, which
    preserves the current single-worker behaviour exactly. Raising this REQUIRES
    ``ROVIMEN_REDIS_URL`` to be set, otherwise the pushed status/vitals cache and
    the public-API rate limiter are per-process and DESYNC across workers. When
    workers > 1 and no Redis URL is configured, ``on_starting`` logs a loud
    warning (the app still boots — degraded, not dead).

    MEMORY: each worker is a full process with its OWN in-heap image/prefetch/
    proxy caches (Redis shares only status/vitals/rate-limit/live-thumb). Those
    per-process caches are now LRU-bounded in ``create_app`` (see the
    ``ROVIMEN_*_CACHE_MAX`` knobs) so a worker's RSS is bounded regardless of
    uptime — >1 worker no longer walks the box into the OOM killer. The systemd
    unit also sets a ``MemoryMax`` safety net so a runaway is cgroup-killed and
    restarted instead of taking the whole box down.
  * ``GUNICORN_THREADS``  — threads per worker (gthread class). Default 32 (prod)
    is expressed via env; the units set 16 for dev.
  * ``GUNICORN_BIND``     — bind address. Default 127.0.0.1:17777.
  * ``GUNICORN_TIMEOUT`` / ``GUNICORN_GRACEFUL_TIMEOUT`` — request + shutdown
    deadlines.
"""

import logging
import os
import threading

logger = logging.getLogger("gunicorn.error")

# ── Worker model ──────────────────────────────────────────────────────────
worker_class = "gthread"
workers = int(os.environ.get("GUNICORN_WORKERS", "1"))
threads = int(os.environ.get("GUNICORN_THREADS", "32"))

# ── Networking / lifecycle ─────────────────────────────────────────────────
bind = os.environ.get("GUNICORN_BIND", "127.0.0.1:17777")
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "120"))
graceful_timeout = int(os.environ.get("GUNICORN_GRACEFUL_TIMEOUT", "30"))
accesslog = os.environ.get("GUNICORN_ACCESSLOG", "-")

# ── Worker recycling ────────────────────────────────────────────────────────
# Reclaim worker RSS on a request cadence. The per-process caches are count-
# bounded but hold large blobs, and SSHFS reader threads that time out are
# abandoned (they keep their stack + buffers until the box unblocks) — so a
# long-lived worker ratchets its RSS upward until the cgroup OOM-kills it.
# Recycling after a bounded number of requests hands that memory back. This is
# the piece that was missing when >1 worker walked the box into the OOM killer.
# 20,000 requests is ~8 h of fleet push traffic per worker (2026-09: ~7,600
# req/h across 3 workers). Recycling is a backstop again, not the heartbeat of
# the service: at 500 (the 2026-09-04 ops mitigation for the per-worker GMN
# and archive-index memory) every worker rebooted every ~4 min and each boot
# re-parsed ~3 GB of GMN text, re-fetched the shower catalogue and re-scanned
# SSH host keys, so the dashboard lived permanently in its own boot storm.
# The boot-time work is now persisted and shared (gmn_data month index, MDC
# disk cache, keyscan negative cache) and the memory that motivated the low
# value is gone with it.
max_requests = int(os.environ.get("GUNICORN_MAX_REQUESTS", "20000"))
max_requests_jitter = int(os.environ.get("GUNICORN_MAX_REQUESTS_JITTER", "2000"))

# Recycling only reclaims memory if the worker can actually EXIT. Interpreter
# shutdown joins all non-daemon executor threads, and an SSHFS-blocked scan
# makes that join outlast the heartbeat ``timeout`` — recycle becomes WORKER
# TIMEOUT -> SIGKILL -> a 2-minute single-worker blackout (2026-07-06 night).
# So the gunicorn tiers opt in to the fast-exit hook (worker_exit.py): flush
# the activity log + logging, then _exit(0) instead of waiting on the box.
# ``setdefault`` lets a unit env file force "0" to opt out.
os.environ.setdefault("ROVIMEN_FAST_WORKER_EXIT", "1")


def post_worker_init(worker) -> None:  # noqa: ANN001 -- gunicorn worker arg
    """Start the dashboard's background workers AFTER the worker is serving.

    gunicorn calls this once per worker, after the worker has loaded the WSGI
    app and is about to enter its request loop — i.e. strictly after the
    fork/exec and after ``create_app()`` has returned. Starting the pollers,
    prefetch loop and archive-index build here (rather than inline in
    ``create_app``) keeps the fork→ready window free of thread-spawns and lock
    contention, which is what probabilistically wedged the worker on
    ``systemctl restart`` (fork-after-threads deadlock).

    The app also arms a short ``threading.Timer`` fallback in ``create_app`` for
    launch paths without gunicorn hooks; ``_start_background_workers()`` is
    idempotent, so triggering it from both places is harmless.
    """
    try:
        wsgi = worker.app.wsgi()
        start = getattr(wsgi, "_start_background_workers", None)
        if callable(start):
            # Spawn OFF the worker boot path. post_worker_init runs before the
            # worker enters its request loop, and _start_background_workers does
            # synchronous disk rehydration + serial tunnel/poller setup that can
            # exceed the deploy restart poll under disk/lock contention -> the
            # worker never binds /login and the dashboard 504s on restart
            # (probabilistic wedge). Threading it lets the worker serve
            # immediately; background startup (and any slowness in it) runs async.
            threading.Thread(target=start, name="bg-startup", daemon=True).start()
    except Exception:
        # Never let a background-startup hiccup take down a worker that is
        # otherwise ready to serve; the create_app timer fallback still fires.
        worker.log.exception("post_worker_init: background startup failed")


def on_starting(server) -> None:  # noqa: ANN001 -- gunicorn server arg
    """Guard: multi-worker without a shared Redis backend desyncs caches.

    The pushed StationCache (status/vitals) and the public-API rate limiter are
    per-process unless ``ROVIMEN_REDIS_URL`` is set. With more than one worker
    and no Redis, a read served by one worker won't see what another ingested,
    and rate limits multiply by the worker count. We refuse to fail closed
    (the dashboard must stay up), but we make the misconfiguration impossible
    to miss in the logs.
    """
    if workers > 1 and not os.environ.get("ROVIMEN_REDIS_URL"):
        msg = (
            "GUNICORN_WORKERS=%d but ROVIMEN_REDIS_URL is unset — the pushed "
            "station cache and rate limiter are PER-WORKER and will desync. "
            "Set ROVIMEN_REDIS_URL to a shared Redis before running >1 worker."
        )
        # Log through gunicorn's error logger AND stderr so it surfaces
        # regardless of how logging is configured at boot.
        try:
            server.log.warning(msg, workers)
        except Exception:
            logger.warning(msg, workers)
        print("WARNING: " + (msg % workers), flush=True)


# ── Control socket (gunicorn >= 25.1) ──────────────────────────────────────
# gunicorn 25.1 starts an asyncio control-socket server in a background THREAD
# of the master (gunicorn/ctl/server.py). That thread logs "Control socket
# listening at ..." concurrently with the very first spawn_workers() fork, so a
# child forked while the thread holds the stderr/logging lock deadlocks inside
# its own "Booting worker" log call — before it ever heartbeats. Verified on
# prod 2026-09-06 with py-spy: 1 of 3 workers sat at PyThread_acquire_lock <-
# logging.emit <- arbiter.spawn_worker for 35+ min with one thread and 0 CPU,
# i.e. a third of the fleet's serving capacity was dead. Nothing here uses
# ``gunicornc``; disable the server so the master stays single-threaded (the
# only other master threads are jemalloc's, which have proper atfork hooks).
# Unknown settings are ignored by older gunicorn, so this is safe on every tier.
control_socket_disable = True


def pre_fork(server, worker) -> None:  # noqa: ANN001 -- gunicorn hook args
    """Stamp a monotonic heartbeat BEFORE fork so a never-booted child is reaped.

    gunicorn's ``WorkerTmp`` creates its heartbeat file with ``mkstemp`` (mtime
    = wall clock) but ``notify()`` writes ``time.monotonic()``, and the arbiter
    reaps with ``monotonic() - last_update() <= timeout`` (gunicorn 25.1.0
    ``arbiter.py:581``). For a child that never reaches its first ``notify()``
    that difference is monotonic - wallclock, roughly -1.7e9 s: always below
    ``timeout``, so the arbiter never kills it and the wedge above persists
    until the next ``systemctl restart``. Writing one monotonic stamp in the
    master, right before the fork, gives every child a sane baseline: a worker
    that deadlocks at fork is now SIGABRT/SIGKILLed after ``timeout`` seconds
    and replaced, instead of sitting as dead capacity for hours.
    """
    try:
        worker.tmp.notify()
    except Exception:
        # Best effort: never let the safety net itself abort a spawn.
        pass
