"""Regression guard for the create_app() restart deadlock.

Background: starting the pollers / prefetch loop / tunnel init / archive-index
build INLINE in ``create_app()`` meant those threads began contending for the
import lock, the logging lock and the SSHFS+SQLite handles WHILE the boot thread
was still finishing its own lazy imports and route registration. Across a
``systemctl restart`` (stop overlapping start) that fork-after-threads race
probabilistically wedged the gunicorn worker in ``futex_wait`` at ~20 MB RSS —
``create_app()`` never returned, so the worker never bound its socket and never
served (curl hung → 000).

The fix moves every background start off the boot-critical path into a single
idempotent ``app._start_background_workers()`` closure, triggered AFTER the
worker is serving (a short ``threading.Timer`` fallback in ``create_app`` plus
the gunicorn ``post_worker_init`` hook). These tests pin the invariants:

  1. ``create_app()`` returns promptly and the returned app is serving-ready
     (routes registered, answers a trivial request) WITHOUT the background
     workers having started.
  2. The deferred hook, when background work is enabled, is invoked exactly once
     and is idempotent (double-trigger is a no-op).
  3. The ``ROVIMEN_DISABLE_BACKGROUND`` gate still fully skips background work.

Reuses the minimal-app fixture pattern from ``test_background_worker_gate.py``.
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import yaml


def _minimal_app_env() -> Path:
    """Write a minimal users.yaml + dashboard_config.yaml and point the
    dashboard's env at throwaway paths. Returns the config path."""
    tmp = Path(tempfile.mkdtemp(prefix="rovimen_defer_"))
    (tmp / "users.yaml").write_text(yaml.safe_dump({}))
    cfg = {
        "station_api_port": 7779,
        "stations": {
            "a": {
                "ip": "100.0.0.1",
                "label": "A",
                "cameras": [{"code": "RO000A", "cam_ip": "192.168.1.10"}],
            }
        },
    }
    cfg_path = tmp / "dashboard_config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg))

    for key, val in {
        "ROVIMEN_USERS_PATH": tmp / "users.yaml",
        "THUMB_CACHE_DIR": tmp / "thumb_cache",
        "ROVIMEN_CACHE_PATH": tmp / "cache",
        "ROVIMEN_KNOWN_HOSTS": tmp / "known_hosts",
        "ROVIMEN_AUDIT_LOG_PATH": tmp / "audit.log",
        "ROVIMEN_ACTIVITY_LOG_PATH": tmp / "activity.log",
        "ROVIMEN_STATION_STATE_DB": tmp / "station_state.db",
        "ROVIMEN_DETECTIONS_DB": tmp / "detections.db",
    }.items():
        os.environ[key] = str(val)
    os.environ.setdefault("ROVIMEN_SECRET_KEY", "defer-test-secret")
    return cfg_path


def _build_app():
    cfg_path = _minimal_app_env()
    from rovimen_dashboard import create_app, load_config

    return create_app(load_config(cfg_path), cfg_path)


def test_create_app_returns_serving_ready_without_background(monkeypatch):
    """create_app() must return an app whose request surface works, and must
    NOT have started background workers on the boot path.

    The session-wide ROVIMEN_DISABLE_BACKGROUND=1 (conftest) means the timer is
    never armed and the hook is a no-op, so this proves the app is complete and
    serving-ready with zero background work — which is exactly the boot-critical
    state the worker must reach before anything else runs."""
    assert os.environ.get("ROVIMEN_DISABLE_BACKGROUND") == "1"

    started = time.monotonic()
    app = _build_app()
    elapsed = time.monotonic() - started

    # Construction must be prompt — no synchronous poll/tunnel/SSHFS on boot.
    assert elapsed < 10.0, f"create_app took {elapsed:.1f}s (boot path blocked?)"

    # The deferred hook is always installed, regardless of the gate.
    assert callable(getattr(app, "_start_background_workers", None))

    # Routes are registered → the app is serving-ready. /login is a public
    # path (no session required) so a trivial request completes without hanging.
    rules = {r.rule for r in app.url_map.iter_rules()}
    assert "/login" in rules
    client = app.test_client()
    resp = client.get("/login")
    assert resp.status_code in (200, 302, 303)


def test_deferred_hook_runs_once_and_is_idempotent(monkeypatch):
    """With background work enabled, _start_background_workers() must fan out to
    the individual start_* functions exactly once even when triggered twice
    (timer + gunicorn post_worker_init both fire in production)."""
    monkeypatch.setenv("ROVIMEN_DISABLE_BACKGROUND", "0")

    import rovimen_dashboard as rd

    calls: list[str] = []

    # Patch every start_* / spawn site the closure touches so nothing real
    # (SSH tunnels, pollers, SSHFS scans, timers) runs during the test.
    monkeypatch.setattr(rd, "start_polling", lambda *a, **k: calls.append("polling"))
    monkeypatch.setattr(rd, "_start_gmn_poller", lambda *a, **k: calls.append("gmn"))
    monkeypatch.setattr(rd, "_disk_cache_load_into", lambda *a, **k: 0)
    # Win the render-leader election so the leader-gated daemons (dome scheduler
    # + thumb prefetch) start in this worker; test_render_leader_gate_* covers
    # the non-leader path where they are skipped.
    monkeypatch.setattr(rd, "_try_become_render_leader", lambda: True)
    # Never touch the real /opt/rovimen lock dir from a test.
    monkeypatch.setattr(rd, "_try_become_duty_leader", lambda duty: False)

    class _FakeThread:
        def __init__(self, *a, **k):
            calls.append("thread:" + str(k.get("name", (k.get("target") or "").__class__.__name__)))

        def start(self):
            pass

    class _FakeTimer:
        # The create_app end-of-boot timer must NOT fire during the test; we
        # trigger the hook ourselves. Record that it was armed, then no-op.
        def __init__(self, interval, fn, *a, **k):
            self._fn = fn
            calls.append("timer-armed")

        def start(self):
            pass

    monkeypatch.setattr(rd.threading, "Thread", _FakeThread)
    monkeypatch.setattr(rd.threading, "Timer", _FakeTimer)

    # sky-dome scheduler + mdc poller are imported inside create_app / the
    # closure; patch the source module attrs so the imports resolve to stubs.
    import routes.sky_dome as sky_dome
    monkeypatch.setattr(sky_dome, "start_dome_scheduler", lambda *a, **k: calls.append("dome"))
    import mdc_poller
    monkeypatch.setattr(mdc_poller, "start_mdc_poller", lambda *a, **k: calls.append("mdc"))

    # tunnels is a real TunnelManager instance created inside create_app;
    # start_watchdog()/start_all() spawn threads in the tunnels module's own
    # namespace (unaffected by the rd.threading patch), so stub them on the
    # class to keep the test from launching real SSH machinery.
    import tunnels as tunnels_mod
    monkeypatch.setattr(
        tunnels_mod.TunnelManager, "start_watchdog",
        lambda self: calls.append("watchdog"),
    )
    monkeypatch.setattr(
        tunnels_mod.TunnelManager, "start_all",
        lambda self: calls.append("start_all"),
    )

    app = _build_app()

    # Boot path must have armed the fallback timer but NOT started any worker.
    assert "timer-armed" in calls
    assert "polling" not in calls, "background work leaked onto the boot path"

    calls.clear()
    app._start_background_workers()
    first = list(calls)
    assert "polling" in first and "gmn" in first and "mdc" in first and "dome" in first
    # Scheduled compilations are fired from here, not by an external cron.
    assert "thread:compilation-tick" in first

    # Second trigger (the other of timer / post_worker_init) is a no-op.
    calls.clear()
    app._start_background_workers()
    assert calls == [], f"deferred hook ran twice: {calls}"


def test_render_leader_gate_confines_dome_to_one_worker(monkeypatch):
    """The sky-dome scheduler + thumbnail prefetch are leader-gated so exactly
    one worker runs them. A worker that LOSES the flock election must skip both,
    while the per-worker / Redis-shared daemons (pollers, GMN, MDC) still start
    so it serves from the shared caches. This is the Nx-render fix: without the
    gate every worker ran the ~0.7 GB-per-render dome scheduler."""
    monkeypatch.setenv("ROVIMEN_DISABLE_BACKGROUND", "0")

    import rovimen_dashboard as rd

    calls: list[str] = []
    monkeypatch.setattr(rd, "start_polling", lambda *a, **k: calls.append("polling"))
    monkeypatch.setattr(rd, "_start_gmn_poller", lambda *a, **k: calls.append("gmn"))
    monkeypatch.setattr(rd, "_disk_cache_load_into", lambda *a, **k: 0)
    # This worker LOSES the election → must not start the dome/prefetch daemons.
    monkeypatch.setattr(rd, "_try_become_render_leader", lambda: False)
    monkeypatch.setattr(rd, "_try_become_duty_leader", lambda duty: False)

    class _FakeThread:
        def __init__(self, *a, **k):
            pass

        def start(self):
            pass

    class _FakeTimer:
        def __init__(self, *a, **k):
            pass

        def start(self):
            pass

    monkeypatch.setattr(rd.threading, "Thread", _FakeThread)
    monkeypatch.setattr(rd.threading, "Timer", _FakeTimer)
    import routes.sky_dome as sky_dome
    monkeypatch.setattr(sky_dome, "start_dome_scheduler", lambda *a, **k: calls.append("dome"))
    import mdc_poller
    monkeypatch.setattr(mdc_poller, "start_mdc_poller", lambda *a, **k: calls.append("mdc"))
    import tunnels as tunnels_mod
    monkeypatch.setattr(
        tunnels_mod.TunnelManager, "start_watchdog", lambda self: calls.append("watchdog")
    )
    monkeypatch.setattr(
        tunnels_mod.TunnelManager, "start_all", lambda self: calls.append("start_all")
    )

    app = _build_app()
    calls.clear()
    app._start_background_workers()

    assert "dome" not in calls, "dome scheduler must NOT run on a non-leader worker"
    assert "polling" in calls and "gmn" in calls and "mdc" in calls, (
        "per-worker / shared daemons must still start on a non-leader worker"
    )


def test_disabled_gate_skips_deferred_hook(monkeypatch):
    """ROVIMEN_DISABLE_BACKGROUND=1 must make the deferred hook a no-op even
    when called directly (tests / E2E launcher rely on this)."""
    monkeypatch.setenv("ROVIMEN_DISABLE_BACKGROUND", "1")

    import rovimen_dashboard as rd

    calls: list[str] = []
    monkeypatch.setattr(rd, "start_polling", lambda *a, **k: calls.append("polling"))
    monkeypatch.setattr(rd, "_start_gmn_poller", lambda *a, **k: calls.append("gmn"))

    app = _build_app()
    app._start_background_workers()
    assert calls == [], "background work ran despite ROVIMEN_DISABLE_BACKGROUND=1"


# ── per-host duties ──────────────────────────────────────────────────────────
#
# Jobs whose output is host-wide (the thumb cache and its cold tier, the temp
# dir, the network-stats disk seed and GMN month cache) run in the one worker
# holding a named duty lock, not in every worker.


class _RecordingThread:
    """threading.Thread stand-in that records what would have started."""

    started: list[tuple[object, tuple, str | None]] = []

    def __init__(self, *a, target=None, args=(), name=None, **k):
        self._rec = (target, tuple(args), name)

    def start(self):
        _RecordingThread.started.append(self._rec)


def _stub_background(monkeypatch, rd, calls: list[str]) -> None:
    monkeypatch.setattr(rd, "start_polling", lambda *a, **k: calls.append("polling"))
    monkeypatch.setattr(rd, "_start_gmn_poller", lambda *a, **k: calls.append("gmn"))
    monkeypatch.setattr(rd, "_disk_cache_load_into", lambda *a, **k: 0)
    monkeypatch.setattr(rd, "_try_become_render_leader", lambda: False)
    monkeypatch.setattr(rd, "_try_become_duty_leader", lambda duty: False)
    _RecordingThread.started = []
    monkeypatch.setattr(rd.threading, "Thread", _RecordingThread)
    monkeypatch.setattr(rd.threading, "Timer", _RecordingThread)
    import routes.sky_dome as sky_dome
    monkeypatch.setattr(sky_dome, "start_dome_scheduler", lambda *a, **k: calls.append("dome"))
    import mdc_poller
    monkeypatch.setattr(mdc_poller, "start_mdc_poller", lambda *a, **k: calls.append("mdc"))
    import tunnels as tunnels_mod
    monkeypatch.setattr(tunnels_mod.TunnelManager, "start_watchdog", lambda self: None)
    monkeypatch.setattr(tunnels_mod.TunnelManager, "start_all", lambda self: None)
    import network_stats
    monkeypatch.setattr(network_stats, "register_precompute",
                        lambda *a, **k: calls.append("network_precompute"))


def _thread_names() -> list[str | None]:
    return [name for _t, _a, name in _RecordingThread.started]


def test_network_precompute_is_not_started_by_create_app(monkeypatch):
    """create_app() ran the coverage precompute and the GMN orbit-count warmup
    in every worker, even under ROVIMEN_DISABLE_BACKGROUND=1 (tests, E2E)."""
    assert os.environ.get("ROVIMEN_DISABLE_BACKGROUND") == "1"
    import rovimen_dashboard as rd

    calls: list[str] = []
    _stub_background(monkeypatch, rd, calls)
    app = _build_app()
    app._start_background_workers()

    assert "network_precompute" not in calls
    assert "orbit-warmup" not in _thread_names()


def test_network_precompute_runs_in_the_duty_leader_only(monkeypatch):
    monkeypatch.setenv("ROVIMEN_DISABLE_BACKGROUND", "0")
    import rovimen_dashboard as rd

    for leader in (True, False):
        calls: list[str] = []
        _stub_background(monkeypatch, rd, calls)
        duties: list[str] = []
        monkeypatch.setattr(
            rd, "_try_become_duty_leader",
            lambda duty, leader=leader: duties.append(duty) or leader,
        )
        app = _build_app()
        calls.clear()
        _RecordingThread.started = []
        app._start_background_workers()

        assert "network_precompute" in duties
        assert ("network_precompute" in calls) is leader
        assert ("orbit-warmup" in _thread_names()) is leader
        # Per-worker daemons start either way.
        assert "polling" in calls and "gmn" in calls


def test_host_wide_sweeps_are_gated_on_a_duty(monkeypatch, tmp_path):
    """The thumb-cache prune and the temp reaper start in every worker; each
    pass must check a duty lock so only one worker sweeps the host."""
    import rovimen_dashboard as rd

    calls: list[str] = []
    _stub_background(monkeypatch, rd, calls)
    monkeypatch.setattr(rd, "THUMB_CACHE_DIR", tmp_path / "thumb_cache")
    monkeypatch.setattr(rd, "create_app", lambda config, config_path: "app")
    duties: list[str] = []
    monkeypatch.setattr(rd, "_try_become_duty_leader", lambda duty: duties.append(duty) or False)

    assert rd._init_and_create_app(_minimal_app_env()) == "app"

    gates = {t: a for t, a, _n in _RecordingThread.started
             if t in (rd._prune_thumb_cache, rd._reap_stale_tmp)}
    assert set(gates) == {rd._prune_thumb_cache, rd._reap_stale_tmp}
    assert gates[rd._prune_thumb_cache][0]() is False
    assert gates[rd._reap_stale_tmp][0]() is False
    assert duties == ["thumb_prune", "tmp_reap"]


def test_cache_cleanup_starts_with_the_background_workers_under_a_duty(monkeypatch):
    """Registering the archive routes started the hourly cache cleanup in every
    worker, outside the deferred hook. It starts from the hook now, and each
    pass checks the "cache_cleanup" duty: every worker shares CACHE_PATH."""
    monkeypatch.setenv("ROVIMEN_DISABLE_BACKGROUND", "0")
    import rovimen_dashboard as rd
    import routes.archive as archive

    calls: list[str] = []
    _stub_background(monkeypatch, rd, calls)
    duties: list[str] = []
    monkeypatch.setattr(rd, "_try_become_duty_leader",
                        lambda duty: duties.append(duty) or False)

    app = _build_app()
    at_boot = [getattr(t, "__name__", None) for t, _a, _n in _RecordingThread.started]
    assert "_cache_cleanup" not in at_boot and "cache_cleanup_loop" not in at_boot

    _RecordingThread.started = []
    app._start_background_workers()
    (args,) = [a for t, a, _n in _RecordingThread.started if t is archive.cache_cleanup_loop]
    assert args[0]() is False
    assert duties[-1] == "cache_cleanup"
