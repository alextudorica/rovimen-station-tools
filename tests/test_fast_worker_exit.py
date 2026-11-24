"""Tests for the gunicorn fast worker exit hook (dashboard/worker_exit.py).

The hook exists so a recycling worker never waits on SSHFS-blocked executor
threads at interpreter shutdown (2026-07-06 night: recycle -> WORKER TIMEOUT
-> SIGKILL blackouts). The subprocess test reproduces that wedge shape with a
deliberately hung executor thread and proves the armed hook exits anyway.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

import worker_exit

DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"


@pytest.fixture(autouse=True)
def _fresh_install_state(monkeypatch):
    """Each test starts with the hook not yet installed."""
    monkeypatch.setattr(worker_exit, "_installed", False)


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ROVIMEN_FAST_WORKER_EXIT", raising=False)
    registered: list = []
    monkeypatch.setattr(
        worker_exit.threading, "_register_atexit", registered.append
    )
    assert worker_exit.install_fast_worker_exit() is False
    assert registered == []


def test_installs_when_enabled_and_is_idempotent(monkeypatch):
    monkeypatch.setenv("ROVIMEN_FAST_WORKER_EXIT", "1")
    registered: list = []
    monkeypatch.setattr(
        worker_exit.threading, "_register_atexit", registered.append
    )
    assert worker_exit.install_fast_worker_exit() is True
    assert worker_exit.install_fast_worker_exit() is True
    # Second call must not double-register.
    assert registered == [worker_exit._fast_exit]


def test_degrades_gracefully_without_private_api(monkeypatch):
    """If a future Python drops threading._register_atexit, we fall back to
    normal shutdown semantics instead of crashing app startup."""
    monkeypatch.setenv("ROVIMEN_FAST_WORKER_EXIT", "1")
    monkeypatch.delattr(worker_exit.threading, "_register_atexit")
    assert worker_exit.install_fast_worker_exit() is False


def test_fast_exit_flushes_activity_log_then_logging_then_exits(monkeypatch):
    import security

    order: list = []
    monkeypatch.setattr(
        security, "flush_activity_log_on_exit", lambda: order.append("activity")
    )
    monkeypatch.setattr(logging, "shutdown", lambda: order.append("logging"))
    monkeypatch.setattr(
        worker_exit.os, "_exit", lambda code: order.append(("exit", code))
    )
    worker_exit._fast_exit()
    assert order == ["activity", "logging", ("exit", 0)]


def test_fast_exit_still_exits_when_flush_raises(monkeypatch):
    """Nothing in the exit path may block the exit it guarantees."""
    import security

    order: list = []

    def _boom() -> None:
        raise RuntimeError("disk gone")

    monkeypatch.setattr(security, "flush_activity_log_on_exit", _boom)
    monkeypatch.setattr(logging, "shutdown", lambda: order.append("logging"))
    monkeypatch.setattr(
        worker_exit.os, "_exit", lambda code: order.append(("exit", code))
    )
    worker_exit._fast_exit()
    assert order == ["logging", ("exit", 0)]


def test_fast_exit_beats_hung_executor_thread(tmp_path):
    """End-to-end proof of the LIFO ordering the fix relies on.

    A non-daemon ThreadPoolExecutor thread blocked for an hour stands in for
    the SSHFS-wedged ``_scan_night``. Without the hook, interpreter shutdown
    joins it and the process hangs (the prod WORKER TIMEOUT wedge); with the
    hook armed, the process must exit 0 within seconds.
    """
    script = tmp_path / "wedge_repro.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import os
            import sys
            import time
            from concurrent.futures import ThreadPoolExecutor

            sys.path.insert(0, {str(DASHBOARD_DIR)!r})
            os.environ["ROVIMEN_FAST_WORKER_EXIT"] = "1"

            import worker_exit

            assert worker_exit.install_fast_worker_exit()
            pool = ThreadPoolExecutor(max_workers=1)
            pool.submit(time.sleep, 3600)  # SSHFS-blocked scan stand-in
            time.sleep(0.2)  # ensure the worker thread has started
            # Fall off the end: interpreter shutdown begins here.
            """
        )
    )
    start = time.monotonic()
    proc = subprocess.run(
        [sys.executable, str(script)], timeout=20, capture_output=True
    )
    elapsed = time.monotonic() - start
    assert proc.returncode == 0, proc.stderr.decode()
    assert elapsed < 10, f"exit took {elapsed:.1f}s — hook did not fire"
