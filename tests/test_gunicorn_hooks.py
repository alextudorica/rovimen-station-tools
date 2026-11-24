"""gunicorn config: a single-threaded master, a heartbeat baseline for every
child, and a recycle threshold that is a backstop rather than a heartbeat.

Verified on prod 2026-09-06: the 25.1 control-socket thread deadlocked a
freshly forked worker on the logging lock, the arbiter never reaped it (its
heartbeat age computed as monotonic - wallclock, always below the timeout),
and GUNICORN_MAX_REQUESTS=500 rebooted every worker every ~4 minutes.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "dashboard"))


# ── gunicorn hooks ────────────────────────────────────────────────────────

def _load_gunicorn_conf():
    spec = importlib.util.spec_from_file_location(
        "gunicorn_conf_under_test", PROJECT / "dashboard" / "gunicorn.conf.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_gunicorn_master_disables_the_control_socket_thread():
    conf = _load_gunicorn_conf()
    assert conf.control_socket_disable is True


def test_pre_fork_stamps_a_monotonic_heartbeat_baseline():
    conf = _load_gunicorn_conf()

    class _Tmp:
        def __init__(self):
            self.notified = 0

        def notify(self):
            self.notified += 1

    class _Worker:
        tmp = _Tmp()

    w = _Worker()
    conf.pre_fork(server=None, worker=w)
    assert w.tmp.notified == 1

    class _Broken:
        def notify(self):
            raise OSError("fd closed")

    w.tmp = _Broken()
    conf.pre_fork(server=None, worker=w)   # never raises into the arbiter


def test_recycle_threshold_is_a_backstop_not_a_heartbeat(monkeypatch):
    monkeypatch.delenv("GUNICORN_MAX_REQUESTS", raising=False)
    monkeypatch.delenv("GUNICORN_MAX_REQUESTS_JITTER", raising=False)
    conf = _load_gunicorn_conf()
    assert conf.max_requests >= 10000
    assert conf.max_requests_jitter >= conf.max_requests // 20


def test_loading_the_config_does_not_arm_fast_exit_in_the_test_process():
    """gunicorn.conf.py setdefaults ROVIMEN_FAST_WORKER_EXIT=1. Armed inside
    pytest, the next create_app() ends the run with os._exit(0) and hides every
    failure; conftest.py pins the variable to 0 so that cannot happen."""
    _load_gunicorn_conf()
    import worker_exit
    assert not worker_exit.fast_exit_enabled()
