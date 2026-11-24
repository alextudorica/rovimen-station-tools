"""``/api/archive/file`` resolves a gallery from one listing, not one stat each.

The Spectro tab renders a night as up to 170 pictures (gmnro01/GH662S/20260920:
82 event cards, each with a widefield frame, a grating frame and a reduced
spectrum). Every one of those ``<img>`` requests used to re-stat its own file
through the six-permit SSHFS lane in ``station_client``, whose acquire timeout
is 2 s, so opening the tab once saturated the lane and 66 of the 170 pictures
came back 503 -- a third of the gallery rendered as broken images, and clicking
a plot opened the lightbox on a broken image.

Asserted here: a directory's files are resolved by ONE guarded storagebox walk
however many of them are requested, and a shed lane after that walk no longer
turns the rest of the night into 503s. The fall-through is kept honest too -- a
name the listing does not carry still gets its own stat, so a file written
after the listing was taken is served rather than 404'd, and a genuinely
missing file is still a 404 while a stalled mount is still a 503.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest
import yaml


CAM = "GH662S"
DATE = "20260920"
SUBDIR = "spectro"
# One real night's shape, scaled down: an RMS-triggered event with a spectrum
# and a widefield-triggered one, each with the pictures the tab renders.
EVENTS = ["RO000A_20260920_172427", "WIDE_20260920_183052"]
NAMES = [f"{ev}__{f}" for ev in EVENTS
         for f in ("wide.jpg", "spectro.jpg", "spectrum.png",
                   "spectrum.csv", "event.json")]


@pytest.fixture(scope="module")
def app_env():
    tmp = Path(tempfile.mkdtemp(prefix="rovimen_dirlisting_"))

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
            "gmnro01": {
                "ip": "127.0.0.1",
                "label": "Ghirdoveni",
                "ssh_user": "gmn",
                "public": True,
                "lat": 44.8,
                "lon": 25.4,
                "cameras": [
                    {"code": CAM, "cam_ip": "127.0.0.1", "label": "Spectro",
                     "rotate": False, "rms": False, "spectro": True},
                ],
            },
        },
    }))

    os.environ["ROVIMEN_USERS_PATH"] = str(users_path)
    os.environ["ROVIMEN_SECRET_KEY"] = "dirlisting-test-secret"
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

    saved_threshold = security._LOGIN_THRESHOLD
    saved_lockout = security._LOGIN_LOCKOUT_SECONDS
    saved_init_limiter = security.init_limiter
    security._LOGIN_THRESHOLD = 10_000
    security._LOGIN_LOCKOUT_SECONDS = 0

    def _noop_limiter(app):
        lim = saved_init_limiter(app)
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
        security._LOGIN_THRESHOLD = saved_threshold
        security._LOGIN_LOCKOUT_SECONDS = saved_lockout
        security.init_limiter = saved_init_limiter
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
def night(tmp_path, monkeypatch):
    """A night of spectro artifacts on a stand-in archive root."""
    import routes.archive as archive_routes

    root = tmp_path / "archive"
    night_dir = root / CAM / DATE / SUBDIR
    night_dir.mkdir(parents=True)
    for name in NAMES:
        (night_dir / name).write_bytes(b"x" * 32)
    monkeypatch.setattr(archive_routes, "ARCHIVE_PATH", root)

    def _clear() -> None:
        # getattr rather than a direct reference: the listing cache is what
        # this file is here to assert, so its absence must read as a failing
        # assertion below, not as a fixture error that hides which behaviour
        # broke.
        for name in ("_archive_dir_cache", "_archive_dir_locks"):
            cache = getattr(archive_routes, name, None)
            if cache is not None:
                cache.clear()

    _clear()
    yield night_dir
    _clear()


class _Lane:
    """Stand-in for ``_with_sshfs_timeout`` that counts calls and can shed.

    ``capacity`` is how many calls run for real before the lane is saturated;
    every call after that returns ``default``, which is what the real guard
    does when no permit frees up within its acquire timeout.
    """

    def __init__(self, capacity: int = 10_000) -> None:
        self.capacity = capacity
        self.calls = 0

    def __call__(self, fn, timeout: float = 5.0, default=None):
        self.calls += 1
        if self.calls > self.capacity:
            return default
        try:
            return fn()
        except Exception:
            return default


def _url(name: str) -> str:
    return f"/api/archive/file/{CAM}/{DATE}/{SUBDIR}/{name}"


class TestOneListingServesTheGallery:
    def test_a_night_of_pictures_costs_one_storagebox_walk(
        self, admin, night, monkeypatch,
    ):
        """Ten files, one guarded walk -- not ten stats down the SSHFS lane."""
        import routes.archive as archive_routes

        lane = _Lane()
        monkeypatch.setattr(archive_routes, "_with_sshfs_timeout", lane)

        for name in NAMES:
            assert admin.get(_url(name)).status_code == 200, name
        assert lane.calls == 1

    def test_a_shed_lane_no_longer_breaks_the_rest_of_the_night(
        self, admin, night, monkeypatch,
    ):
        """The lane saturates right after the listing; the gallery still renders.

        This is the reported failure: with one stat per picture, every request
        past the lane's capacity answered 503 and the tab showed broken images.
        """
        import routes.archive as archive_routes

        lane = _Lane(capacity=1)
        monkeypatch.setattr(archive_routes, "_with_sshfs_timeout", lane)

        codes = [admin.get(_url(name)).status_code for name in NAMES]
        assert codes == [200] * len(NAMES)

    def test_the_plot_a_click_enlarges_is_served_from_the_listing(
        self, admin, night, monkeypatch,
    ):
        """The reduced-spectrum PNG is the picture the lightbox opens.

        Requested last, after the rest of the night has already taken the
        lane's one permit -- which is where the operator meets it, since the
        plot is the picture they click.
        """
        import routes.archive as archive_routes

        monkeypatch.setattr(archive_routes, "_with_sshfs_timeout", _Lane(capacity=1))

        plot = f"{EVENTS[0]}__spectrum.png"
        for name in NAMES:
            if name != plot:
                admin.get(_url(name))

        resp = admin.get(_url(plot))
        assert resp.status_code == 200
        assert resp.headers["Content-Type"] == "image/png"


class TestExistingSemanticsAreUnchanged:
    def test_a_file_written_after_the_listing_is_still_served(
        self, admin, night, monkeypatch,
    ):
        """A name the cached listing does not carry falls through to its stat."""
        import routes.archive as archive_routes

        lane = _Lane()
        monkeypatch.setattr(archive_routes, "_with_sshfs_timeout", lane)

        assert admin.get(_url(NAMES[0])).status_code == 200   # primes the listing
        (night / "WIDE_20260920_235813__wide.jpg").write_bytes(b"x" * 32)

        before = lane.calls
        assert admin.get(_url("WIDE_20260920_235813__wide.jpg")).status_code == 200
        assert lane.calls == before + 1

    def test_a_missing_file_is_still_a_404(self, admin, night, monkeypatch):
        import routes.archive as archive_routes

        monkeypatch.setattr(archive_routes, "_with_sshfs_timeout", _Lane())
        assert admin.get(_url("WIDE_20260920_999999__wide.jpg")).status_code == 404

    def test_a_stalled_mount_is_still_a_503(self, admin, night, monkeypatch):
        """Both the listing and the fall-through stat shed: unknown, not empty."""
        import routes.archive as archive_routes

        monkeypatch.setattr(archive_routes, "_with_sshfs_timeout", _Lane(capacity=0))
        assert admin.get(_url(NAMES[0])).status_code == 503

    def test_an_unknown_listing_is_not_cached_as_an_empty_directory(
        self, admin, night, monkeypatch,
    ):
        """A shed walk must not make the whole night 404 for the next minute."""
        import routes.archive as archive_routes

        monkeypatch.setattr(archive_routes, "_with_sshfs_timeout", _Lane(capacity=0))
        assert admin.get(_url(NAMES[0])).status_code == 503

        monkeypatch.setattr(archive_routes, "_ARCHIVE_DIR_UNKNOWN_TTL", 0.0,
                            raising=False)
        getattr(archive_routes, "_archive_dir_cache", {}).clear()
        monkeypatch.setattr(archive_routes, "_with_sshfs_timeout", _Lane())
        assert admin.get(_url(NAMES[0])).status_code == 200
