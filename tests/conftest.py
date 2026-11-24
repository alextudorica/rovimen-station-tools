"""Shared test fixtures for the ROVIMEN test suite."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

# Add module directories to sys.path so tests can import station scripts
# and dashboard modules directly, matching how they run in production.
REPO_ROOT = Path(__file__).resolve().parent.parent
STATION_SCRIPTS = REPO_ROOT / "rovimen-scripts"
DASHBOARD = REPO_ROOT / "dashboard"
TOOLS = REPO_ROOT / "tools"

for p in (STATION_SCRIPTS, DASHBOARD, TOOLS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

# create_app() spawns a dozen forever-sleeping daemon workers (status/vitals
# pollers, tunnel watchdog, thumbnail prefetch, sky-dome scheduler, GMN/MDC and
# archive-index refreshers). Under pytest those threads are never joined, so
# every create_app-based test leaked them and they piled up across the ~1400-
# test suite — starving the 2-core CI runner until an unlucky test tripped the
# per-test pytest-timeout signal. Tests exercise the WSGI request surface, not
# the pollers, so disable the background workers for the whole session. Set
# before any dashboard import so the module-level default is read correctly.
os.environ.setdefault("ROVIMEN_DISABLE_BACKGROUND", "1")

# gunicorn.conf.py does os.environ.setdefault("ROVIMEN_FAST_WORKER_EXIT", "1"),
# and test_gunicorn_hooks executes that file inside this process. With the
# variable set, the next create_app() arms worker_exit's hook, which ends the
# interpreter with os._exit(0): pytest then exited 0 whatever the results, and
# CI's test gate passed with 4 failing tests from 2026-09-26 to 09-30. Pin it
# off for the session; test_fast_worker_exit sets it per test with monkeypatch.
os.environ["ROVIMEN_FAST_WORKER_EXIT"] = "0"

# No test may reboot or power off the machine running it. Several tests run
# rovimen_watchdog.sh, whose recovery ladder ends in `sudo /sbin/reboot`; a sudo
# stub that passes its arguments through then runs the real /sbin/reboot, and
# in a desktop session logind grants that without a password. That rebooted a
# developer machine three times on 2026-11-17 (test_watchdog_rescue_wifi). With
# the system bus pointed at a dead socket, systemctl, reboot, shutdown and
# loginctl all fail to reach logind and systemd, whatever a stub lets through.
# Subprocesses inherit it unless a test builds its env from scratch.
os.environ["DBUS_SYSTEM_BUS_ADDRESS"] = "unix:path=/nonexistent/rovimen-tests-no-system-bus"

import ff_thumbs  # noqa: E402  (needs the sys.path inserts above)
import flags_manager  # noqa: E402


@pytest.fixture
def station_patch(monkeypatch):
    """Patch a station API name wherever it is bound.

    The collectors live in station_collect and station_api re-exports them,
    so swapping load_config, capture_base, BASE, CONFIG_PATH or a render
    helper has to reach both bindings: station_api's routes read their own
    name, station_collect's functions read theirs. Returns ``patch(name,
    value)``; it fails if neither module defines the name."""
    import station_api
    import station_collect

    def patch(name: str, value, raising: bool = True) -> None:
        hit = False
        for mod in (station_api, station_collect):
            if hasattr(mod, name):
                monkeypatch.setattr(mod, name, value)
                hit = True
        assert hit or not raising, f"neither station_api nor station_collect defines {name}"

    return patch


@pytest.fixture(autouse=True)
def _isolate_full_night_marker(tmp_path, monkeypatch):
    """Keep dawn_process's sticky full-night decision out of the real home dir.

    Any test that drives ``_run_reencode_all`` past a deadline can write the
    marker, and one left in ``~/rovimen_scripts`` would switch the pass off
    for every later test (and for a developer running dawn locally).
    """
    monkeypatch.setattr(flags_manager, "FULL_NIGHT_MARKER_PATH",
                        tmp_path / "full_night_reencode_off.json")


@pytest.fixture(autouse=True)
def _isolate_capture_night_counts(tmp_path, monkeypatch):
    """Keep station_collect's per-night FF counts out of the real home dir and
    out of the next test (the counts are cached in the module)."""
    import station_collect
    monkeypatch.setattr(station_collect, "CAPTURE_COUNTS_PATH", tmp_path / "capture_nights.json")
    monkeypatch.setitem(station_collect._capture_counts, "data", None)


@pytest.fixture(autouse=True)
def _isolate_rms_import_state():
    """Undo the RMS-venv sys.path mutation ff_thumbs performs mid-test.

    ``ff_thumbs.ensure_rms_venv_on_path()`` prepends the developer's real RMS
    checkout (``~/RMS``, ``~/source/RMS``) to ``sys.path`` and caches the probe
    in ``_venv_probe_ok``, neither of which is ever undone. So one test that
    renders an FF thumbnail leaks an importable ``RMS`` package into every
    later test in the session. Tests that write small CALSTARS/FF fixtures then
    reach the real RMS readers — which skip a full-length RMS header the
    fixtures do not have — instead of the format-tolerant fallbacks they are
    written against, and fail only in a full local run, only in file order.
    CI never saw it because the runners have no RMS checkout at all.

    Restoring sys.path, the probe cache, and any imported ``RMS`` modules after
    each test keeps that leak inside the test that caused it.
    """
    saved_path = list(sys.path)
    yield
    sys.path[:] = saved_path
    ff_thumbs._venv_probe_ok = None
    for name in [m for m in sys.modules if m == "RMS" or m.startswith("RMS.")]:
        del sys.modules[name]


@pytest.fixture(autouse=True)
def _isolate_ffthumb_root(tmp_path, monkeypatch):
    """Keep the BW FF thumbnail root out of ~/rovimen_scripts/ffthumbs.

    The indexer renders and archive_upload sweeps that root whenever a config
    has no ``ffthumb_path``; without this a test writes thumbnails into the
    developer's home and a later test uploads them. A config that sets
    ``ffthumb_path`` still wins.
    """
    import rovimen_config

    def _root(cfg):
        value = cfg.get("ffthumb_path")
        return Path(value) if value else tmp_path / "ffthumbs"

    monkeypatch.setattr(rovimen_config, "ffthumb_root", _root)


@pytest.fixture(autouse=True)
def _isolate_state_sigs(tmp_path, monkeypatch):
    """Keep archive_upload's persisted state.json signatures out of ~/.

    _Uploader loads and saves them under ~/rovimen_scripts/upload_state by
    default; pointing every test at its own tmp file keeps runs independent
    and the developer's home untouched.
    """
    monkeypatch.setenv("ROVIMEN_STATE_SIGS_PATH", str(tmp_path / "state_json_sent.json"))
    # The per-night upload locks and mark journals, likewise.
    monkeypatch.setenv("ROVIMEN_UPLOAD_STATE_DIR", str(tmp_path / "upload_state"))


@pytest.fixture(autouse=True)
def _isolate_station_events(tmp_path, monkeypatch):
    """Keep the VPS station event history (dashboard/station_events.py) in
    the test's tmp dir: ingest and the stale-push demotion write it, and its
    default is /opt/rovimen/pushed_state."""
    monkeypatch.setenv("ROVIMEN_STATION_EVENTS_DB", str(tmp_path / "station_events.db"))
    mod = sys.modules.get("station_events")
    if mod is not None and hasattr(mod, "reset_memos_for_tests"):
        mod.reset_memos_for_tests()


@pytest.fixture(autouse=True)
def _isolate_hidden_detections(tmp_path, monkeypatch):
    """Keep the hidden-detections store (dashboard/hidden_detections.py) in
    the test's tmp dir; its default is /opt/rovimen/hidden_detections.db."""
    monkeypatch.setenv("ROVIMEN_HIDDEN_DETECTIONS_DB", str(tmp_path / "hidden_detections.db"))


@pytest.fixture
def tmp_capture_dir(tmp_path):
    """Create a temporary capture directory structure and return a config dict."""
    capture = tmp_path / "color_capture"
    capture.mkdir()
    cfg = {"videocapture_path": str(capture)}
    return capture, cfg


@pytest.fixture
def sample_config(tmp_path):
    """Minimal station config dict pointing at tmp_path."""
    return {
        "videocapture_path": str(tmp_path / "color_capture"),
        "segment_duration": 20,
        "compression_level": 2,
        "stations": {
            "RO000H": {
                "rms_data_path": str(tmp_path / "rms_data"),
                "rotate": False,
            }
        },
        "services": {
            "stacker": {"enabled": True},
            "reencode": {"enabled": True},
            "detection_lock": {"enabled": True},
            "archive_upload": {"enabled": True},
            "timelapse_build": {"enabled": True},
        },
        "archive": {
            "enabled": True,
            "host": "10.0.0.1",
            "port": 22,
            "user": "rovimen",
            "base_path": "/rovimen",
        },
        "overlay": {
            "enabled": True,
            "font": "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "font_size": 19,
            "text_opacity": 0.4,
            "network": "ROVIMEN",
            "coords": "45.0N 25.0E",
            "style": "standard",
        },
    }


@pytest.fixture
def make_state_json(tmp_path):
    """Factory fixture: create a state.json in the expected directory."""
    def _make(station_id: str, date_str: str, state: dict, cfg: dict):
        root = Path(cfg.get("videocapture_path", str(tmp_path / "color_capture")))
        night_dir = root / station_id / date_str
        night_dir.mkdir(parents=True, exist_ok=True)
        (night_dir / "state.json").write_text(json.dumps(state, indent=2))
        return night_dir
    return _make


@pytest.fixture
def ingest_ready(monkeypatch):
    """Make every archive_upload._Uploader built in the test ingest-ready.

    A pass skips outright (no scan) when the ingest settings are missing, so
    tests that drive cycle()/cycle_night() through a stubbed _post_one need
    the settings present, as on a configured station.
    """
    import archive_upload

    monkeypatch.setattr(archive_upload, "_resolve_base_url", lambda cfg: "https://vps.example")
    monkeypatch.setattr(archive_upload, "_resolve_ingest_key", lambda: "SECRET")
    monkeypatch.setattr(archive_upload, "_resolve_host_key", lambda cfg: "gmnro03")


@pytest.fixture(autouse=True)
def _no_internet(monkeypatch):
    """Tests never reach the internet. On 2026-11-01 a highlights route test
    downloaded a GMN monthly trajectory file (60 s read timeout) on a slow CI
    runner, timed out, and blocked the prod deploy. Loopback stays open for
    tests that run a local server; a test faking ``requests.get`` replaces
    this layer and is unaffected."""
    import ipaddress
    from urllib.parse import urlsplit

    import requests

    real = requests.sessions.Session.request

    def guarded(self, method, url, *args, **kwargs):
        host = urlsplit(str(url)).hostname or ""
        try:
            local = host == "localhost" or ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = False
        if not local:
            raise requests.ConnectionError(f"network disabled in tests: {method} {url}")
        return real(self, method, url, *args, **kwargs)

    monkeypatch.setattr(requests.sessions.Session, "request", guarded)
