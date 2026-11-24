"""Overview captured-stacks cache: atomic publish + warming is uncacheable.

The grid at ``/api/overview/stacks`` renders one tile per camera and shows
"no data" for any camera missing from the payload. Two defects made that
misfire on a cold gunicorn worker:

* the fan-out published each camera's result as it landed, so a request that
  snapshotted the cache mid-flight got a **prefix of the fleet in config
  order** — indistinguishable, to the frontend, from "these cameras have no
  stack";
* that prefix was served with ``max-age=60``, so the browser replayed it to
  every retry in the frontend's backoff ladder and a ~4 s warm-up became a
  full minute of "no data" tiles.

These tests pin both: a reader sees either nothing or the complete
generation, and a snapshot published before the first full fan-out is
``no-store`` and flagged ``X-Stacks-Warming``.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
from pathlib import Path

import pytest
import yaml

PROJECT = Path(__file__).resolve().parent.parent
FIXTURES = PROJECT / "tests" / "e2e" / "fixtures"
sys.path.insert(0, str(PROJECT / "dashboard"))

# In the past, so _drop_future_dates keeps it.
NIGHT = "20240115"
CAMS = ["TST001", "TST002", "TST003", "TST004", "TST005", "TST006"]
BLOCKED_CAM = CAMS[-1]  # last in config order -> the one a prefix would omit


@pytest.fixture
def app():
    tmp = Path(tempfile.mkdtemp(prefix="rovimen_ovstacks_test_"))
    os.environ["ROVIMEN_USERS_PATH"] = str(FIXTURES / "users.yaml")
    os.environ["ROVIMEN_SECRET_KEY"] = "ovstacks-test-key"
    os.environ["ROVIMEN_COOKIE_SECURE"] = "0"
    os.environ["RATELIMIT_ENABLED"] = "0"
    os.environ["ROVIMEN_API_KEYS_REQUIRED"] = "0"
    os.environ["ROVIMEN_DISABLE_BACKGROUND"] = "1"  # no pollers / prefetch loop
    os.environ["THUMB_CACHE_DIR"] = str(tmp / "thumb_cache")
    os.environ["ROVIMEN_CACHE_PATH"] = str(tmp / "cache")
    os.environ["ROVIMEN_ARCHIVE_PATH"] = str(tmp / "archive")
    os.environ["ROVIMEN_GMN_CACHE_DIR"] = str(tmp / "gmn_cache")
    os.environ["ROVIMEN_COMPILATIONS_OUT_PATH"] = str(tmp / "compilations")
    os.environ["ROVIMEN_KNOWN_HOSTS"] = str(tmp / "known_hosts")
    os.environ["ROVIMEN_AUDIT_LOG_PATH"] = str(tmp / "audit.log")
    os.environ["ROVIMEN_ACTIVITY_LOG_PATH"] = str(tmp / "activity.log")

    import security
    original_init_limiter = security.init_limiter

    def _noop_limiter(a):
        lim = original_init_limiter(a)
        lim.enabled = False
        return lim
    security.init_limiter = _noop_limiter

    config_data = {
        "correlation_window_s": 1,
        "station_api_port": 7779,
        "stations": {
            "teststn1": {
                "cameras": [
                    {"code": c, "label": "N", "cam_ip": "127.0.0.1",
                     "rotate": False, "az": 0.0, "alt": 45.0}
                    for c in CAMS
                ],
                "ip": "127.0.0.1", "jump_hosts": [], "label": "Test Alpha",
                "lat": 45.0, "lon": 25.0, "proxy_media": False, "public": True,
                "public_tabs": [], "ssh_user": "test",
                "location_name": "Test Alpha",
                "show_on_map": True, "status": "active",
            },
        },
    }
    cfg_path = tmp / "dashboard_config.yaml"
    cfg_path.write_text(yaml.dump(config_data))

    from rovimen_dashboard import load_config, create_app
    config = load_config(cfg_path)
    flask_app = create_app(config, cfg_path)
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture
def client(app):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["user"] = "alice"
        sess["role"] = "admin"
        sess["stations"] = []
    return c


class _Gate:
    """Archive-resolver stand-in that lets 5 of 6 cameras finish while pinning
    the 6th.

    Reproduces the exact mid-fan-out window the old incremental publish
    exposed: five results ready, one still in flight.
    """

    def __init__(self) -> None:
        self.release = threading.Event()
        self.done: set[str] = set()
        self._cv = threading.Condition()

    def resolve(self, archive_idx, host_key, cam_code):
        if cam_code == BLOCKED_CAM:
            # Hold this one open until the test releases it.
            self.release.wait(10)
        entry = {
            "host": host_key, "cam": cam_code, "date": NIGHT,
            "filename": f"{cam_code}_{NIGHT}_182228_099720_captured_stack.jpg",
            "label": "Captured stack",
        }
        with self._cv:
            self.done.add(cam_code)
            self._cv.notify_all()
        return entry

    def wait_for(self, n: int, timeout: float = 10.0) -> bool:
        with self._cv:
            return self._cv.wait_for(lambda: len(self.done) >= n, timeout=timeout)


@pytest.fixture
def gate(monkeypatch):
    import routes.overview as ov
    g = _Gate()
    monkeypatch.setattr(ov, "resolve_archive_cam_stack", g.resolve)
    return g


def test_warming_snapshot_is_flagged_and_uncacheable(client):
    """Before any fan-out completes, the snapshot must not be cached.

    A max-age on a warming snapshot is what let the browser replay an empty
    or partial payload to the whole frontend retry ladder.
    """
    r = client.get("/api/overview/stacks")
    assert r.status_code == 200
    assert r.headers.get("X-Stacks-Warming") == "1"
    assert r.headers.get("Cache-Control") == "no-store"
    assert "max-age" not in (r.headers.get("Cache-Control") or "")


def test_fanout_never_publishes_a_prefix(app, client, gate):
    """Mid-fan-out readers see the previous generation, never a prefix.

    With five of six cameras resolved and the sixth still in flight, the old
    per-future publish exposed those five; the frontend then rendered the
    sixth as a permanent "no data" tile. The publish is now atomic, so the
    payload is still empty at this point.
    """
    t = threading.Thread(target=app._refresh_overview_stacks, daemon=True)
    t.start()
    assert gate.wait_for(len(CAMS) - 1), "stub never resolved the unblocked cameras"

    r = client.get("/api/overview/stacks")
    assert r.status_code == 200
    payload = r.get_json()
    assert payload == [], f"partial snapshot leaked mid-fan-out: {payload}"
    assert r.headers.get("X-Stacks-Warming") == "1"

    gate.release.set()
    t.join(timeout=15)
    assert not t.is_alive()

    r2 = client.get("/api/overview/stacks")
    cams = sorted(e["cam"] for e in r2.get_json())
    assert cams == sorted(CAMS), "complete generation missing cameras"


def test_snapshot_is_cacheable_once_warm(app, client, gate):
    """After a full fan-out the payload is authoritative, so ETag/max-age
    come back — the warming path must not permanently disable caching on a
    heavy (~50-200 KB) endpoint."""
    gate.release.set()
    app._refresh_overview_stacks()

    r = client.get("/api/overview/stacks")
    assert r.status_code == 200
    assert r.headers.get("X-Stacks-Warming") is None
    assert "max-age=60" in r.headers.get("Cache-Control", "")
    assert r.headers.get("ETag")
    assert len(r.get_json()) == len(CAMS)


def test_failed_cameras_keep_last_known_good(app, client, gate):
    """A camera whose lookup fails must not evict its previous entry — the
    grid's whole point is surviving a bad tick."""
    gate.release.set()
    app._refresh_overview_stacks()
    assert len(client.get("/api/overview/stacks").get_json()) == len(CAMS)

    import routes.overview as ov

    def _all_fail(archive_idx, host_key, cam_code):
        raise RuntimeError("storage box unreachable")

    ov.resolve_archive_cam_stack = _all_fail  # monkeypatch fixture restores the original
    app._refresh_overview_stacks()

    still = client.get("/api/overview/stacks").get_json()
    assert sorted(e["cam"] for e in still) == sorted(CAMS)
