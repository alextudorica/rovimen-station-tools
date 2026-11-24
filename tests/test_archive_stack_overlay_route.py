"""The archive stack download route serves branded per-detection stacks only.

``/api/archive/file/<cam>/<date>/{meteors,stacks}/<stem>_stack.webp`` is the
archive/VDB "Download stack" surface. A per-detection stack served here must
carry the ROVIMEN/astromania branding+data strip: an already-branded stack (the
common case, produced by the station stacker) passes through, a legacy
un-branded stack is rendered on demand, and if the overlay cannot be produced
the route refuses (503) rather than handing out the un-branded original.

Drives a real ``create_app`` instance with ARCHIVE_PATH pointed at a tmp dir and
a fake station bundle wired into the stack-overlay gate.
"""

from __future__ import annotations

import importlib
import os
import shutil
import tempfile
from pathlib import Path

import pytest
import yaml

from PIL import Image

import overlay


REPO_ROOT = Path(__file__).resolve().parent.parent
FONT = REPO_ROOT / "rovimen-scripts" / "fonts" / "VCR_OSD_MONO_1.001.ttf"
LOGO = REPO_ROOT / "rovimen-scripts" / "assets" / "astromania_text.png"

CAM = "RO0017"
DATE = "20260730"
STACK = f"{CAM}_{DATE}_214442_stack.webp"


def _blank_stack(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (1280, 720), (5, 8, 20)).save(path, "WEBP", quality=95)
    return path


def _overlay_cfg() -> dict:
    return {
        "enabled": True,
        "font": str(FONT),
        "logo": str(LOGO),
        "font_size": 20,
        "network": "ROVIMEN",
        "style": "cinema",
        "show_logo": True,
        "show_network": True,
        "show_station": True,
        "show_timestamp": True,
        "coords": "45.0N 25.0E",
    }


@pytest.fixture(scope="module")
def app_env():
    tmp = Path(tempfile.mkdtemp(prefix="rovimen_stackroute_"))

    users_path = tmp / "users.yaml"
    users_path.write_text(yaml.safe_dump({
        "admin": {
            "display_name": "Admin",
            "password_hash": "",
            "role": "admin",
            "require_totp": False,
        },
    }))

    config_path = tmp / "dashboard_config.yaml"
    config_path.write_text(yaml.safe_dump({
        "correlation_window_s": 1,
        "station_api_port": 7779,
        "stations": {
            "teststation": {
                "ip": "127.0.0.1",
                "label": "Test Station",
                "ssh_user": "test",
                "public": True,
                "lat": 44.4,
                "lon": 26.1,
                "cameras": [
                    {"code": CAM, "cam_ip": "127.0.0.1", "label": "N", "rotate": False},
                ],
            },
        },
    }))

    os.environ["ROVIMEN_USERS_PATH"] = str(users_path)
    os.environ["ROVIMEN_SECRET_KEY"] = "stackroute-test-secret"
    os.environ["ROVIMEN_COOKIE_SECURE"] = "0"
    os.environ["RATELIMIT_ENABLED"] = "0"
    os.environ["THUMB_CACHE_DIR"] = str(tmp / "thumb_cache")
    os.environ["ROVIMEN_CACHE_PATH"] = str(tmp / "cache")
    os.environ["ROVIMEN_COMPILATIONS_OUT_PATH"] = str(tmp / "compilations")
    os.environ["ROVIMEN_KNOWN_HOSTS"] = str(tmp / "known_hosts")
    os.environ["ROVIMEN_AUDIT_LOG_PATH"] = str(tmp / "audit.log")
    os.environ["ROVIMEN_ACTIVITY_LOG_PATH"] = str(tmp / "activity.log")
    os.environ["ROVIMEN_STATION_STATE_DB"] = str(tmp / "station_state.db")
    os.environ["ROVIMEN_DETECTIONS_DB"] = str(tmp / "detections.db")
    os.environ["ROVIMEN_SHOWER_COUNTS_PATH"] = str(tmp / "shower_counts.json")

    import security

    _saved_threshold = security._LOGIN_THRESHOLD
    _saved_lockout = security._LOGIN_LOCKOUT_SECONDS
    _saved_init_limiter = security.init_limiter
    security._LOGIN_THRESHOLD = 10_000
    security._LOGIN_LOCKOUT_SECONDS = 0

    def _noop_limiter(app):
        lim = _saved_init_limiter(app)
        lim.enabled = False
        return lim

    security.init_limiter = _noop_limiter

    from rovimen_dashboard import load_config, create_app

    config = load_config(config_path)
    app = create_app(config, config_path)
    app.config["TESTING"] = True
    try:
        yield app
    finally:
        security._LOGIN_THRESHOLD = _saved_threshold
        security._LOGIN_LOCKOUT_SECONDS = _saved_lockout
        security.init_limiter = _saved_init_limiter
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def admin(app_env):
    c = app_env.test_client()
    with c.session_transaction() as sess:
        sess["user"] = "admin"
        sess["role"] = "admin"
        sess["stations"] = []
        sess["mfa"] = "totp"
    return c


@pytest.fixture
def archive_root(tmp_path, monkeypatch):
    import routes.archive as archive_routes

    root = tmp_path / "archive"
    root.mkdir()
    monkeypatch.setattr(archive_routes, "ARCHIVE_PATH", root)
    return root


@pytest.fixture
def bundle_gate(monkeypatch):
    """Point the stack-overlay gate at a fake bundle so it can render on demand.

    The station is unreachable (127.0.0.1), so the gate falls back to the
    fleet-default overlay; the bundle only needs overlay.py + font + logo.
    """
    bundle_root = Path(tempfile.mkdtemp(prefix="rovimen_bundle_route_"))
    bdir = bundle_root / "station-bundle"
    (bdir / "fonts").mkdir(parents=True)
    (bdir / "assets").mkdir(parents=True)
    shutil.copy(REPO_ROOT / "rovimen-scripts" / "overlay.py", bdir / "overlay.py")
    shutil.copy(FONT, bdir / "fonts" / FONT.name)
    shutil.copy(LOGO, bdir / "assets" / LOGO.name)
    monkeypatch.setenv("ROVIMEN_BUNDLE_DIR", str(bundle_root))

    # stack_overlay caches the compiled bundle at import; reload it against the
    # fake bundle and rebind the reference the archive route module holds.
    import stack_overlay
    import routes.archive as archive_routes

    reloaded = importlib.reload(stack_overlay)
    monkeypatch.setattr(archive_routes, "stack_overlay", reloaded)
    # Also refresh the per-route branded-stack cache to point at the fresh cache.
    monkeypatch.setattr(
        archive_routes,
        "_branded_stack_cache",
        reloaded.OverlaidStackCache(Path(tempfile.mkdtemp(prefix="branded_"))),
    )
    try:
        yield reloaded
    finally:
        shutil.rmtree(bundle_root, ignore_errors=True)
        importlib.reload(stack_overlay)


def _url(subdir: str, download: bool = False) -> str:
    q = "?download=1" if download else ""
    return f"/api/archive/file/{CAM}/{DATE}/{subdir}/{STACK}{q}"


class TestArchiveStackDownloadIsBranded:
    def test_legacy_unbranded_stack_is_branded_on_download(
        self, admin, archive_root, bundle_gate
    ):
        _blank_stack(archive_root / CAM / DATE / "meteors" / STACK)
        r = admin.get(_url("meteors", download=True))
        assert r.status_code == 200
        assert r.mimetype == "image/webp"
        assert 'attachment; filename="' in r.headers.get("Content-Disposition", "")
        # The served bytes carry the branding marker even though the archived
        # file did not -- rendered on demand, never served raw.
        out = archive_root / "served.webp"
        out.write_bytes(r.data)
        assert overlay.still_is_overlaid(out) is True

    def test_inline_display_is_also_branded(self, admin, archive_root, bundle_gate):
        _blank_stack(archive_root / CAM / DATE / "stacks" / STACK)
        r = admin.get(_url("stacks", download=False))
        assert r.status_code == 200
        out = archive_root / "served2.webp"
        out.write_bytes(r.data)
        assert overlay.still_is_overlaid(out) is True

    def test_already_branded_stack_served_unchanged(
        self, admin, archive_root, bundle_gate
    ):
        src = _blank_stack(archive_root / CAM / DATE / "meteors" / STACK)
        overlay.annotate_still(
            src, src, _overlay_cfg(), CAM, {"az": 180.0, "alt": 45.0}, 1_800_000_000
        )
        branded_bytes = src.read_bytes()
        r = admin.get(_url("meteors", download=True))
        assert r.status_code == 200
        # No double stamping: the pre-branded archive bytes are handed back as-is.
        assert r.data == branded_bytes

    def test_refuses_when_overlay_cannot_be_produced(self, admin, archive_root, monkeypatch):
        """With no bundle to render from, the route returns 503 rather than
        serving the un-branded archived stack."""
        import routes.archive as archive_routes

        _blank_stack(archive_root / CAM / DATE / "meteors" / STACK)

        # Fresh branded-stack cache so a prior test's branded copy can't satisfy
        # this request from cache and mask the refuse path.
        monkeypatch.setattr(
            archive_routes,
            "_branded_stack_cache",
            archive_routes.stack_overlay.OverlaidStackCache(
                Path(tempfile.mkdtemp(prefix="branded_refuse_"))
            ),
        )

        def _refuse(*a, **k):
            return None

        def _refuse_with_flag(*a, **k):
            return None, False

        # The archive route calls the gate via its module reference, so patch
        # there. ``routes.archive`` binds ``stack_overlay`` at import.
        monkeypatch.setattr(
            archive_routes.stack_overlay, "ensure_overlaid_stack_bytes", _refuse
        )
        monkeypatch.setattr(
            archive_routes.stack_overlay, "overlaid_stack_bytes", _refuse_with_flag
        )
        r = admin.get(_url("meteors", download=True))
        assert r.status_code == 503


class TestBrandedStackCache:
    """Only a stack the gate rendered is worth a local copy: an already-branded
    stack (every stack since the station overlay, August 2026) is the archive
    file itself, and copying it made the cache grow with every stack served."""

    def test_rendered_stack_is_cached(self, admin, archive_root, bundle_gate):
        import routes.archive as archive_routes

        _blank_stack(archive_root / CAM / DATE / "meteors" / STACK)
        assert admin.get(_url("meteors")).status_code == 200
        assert archive_routes._branded_stack_cache.get(CAM, DATE, STACK) is not None

    def test_already_branded_stack_is_not_copied(self, admin, archive_root, bundle_gate):
        import routes.archive as archive_routes

        src = _blank_stack(archive_root / CAM / DATE / "meteors" / STACK)
        overlay.annotate_still(
            src, src, _overlay_cfg(), CAM, {"az": 180.0, "alt": 45.0}, 1_800_000_000
        )
        r = admin.get(_url("meteors"))
        assert r.status_code == 200
        assert r.data == src.read_bytes()
        assert archive_routes._branded_stack_cache.get(CAM, DATE, STACK) is None
