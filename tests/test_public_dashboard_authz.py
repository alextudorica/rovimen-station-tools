"""Auth-model inversion tests: explicit-allow public exposure.

Exercises the real ``security.install_auth_gate`` (now explicit-allow /
deny-by-default) plus the per-station ``public`` flag enforcement, driven
through a real ``create_app`` Flask instance and the Flask test client — so
these tests cover the genuine gate + decorator path, not a re-implementation.

Model under test:
* Anonymous requests reach ONLY routes tagged ``@public_route`` (plus the
  static / auth / public-API / media path exceptions). Everything else →
  302-to-login (browser) or 401/403 (API).
* Tagged read/media routes additionally honour the per-station ``public``
  flag for anonymous callers: a ``public: false`` station is 404 / filtered
  out of the anonymous surface.
* Logged-in users keep full fleet-wide read access, including the
  non-public station and every sensitive (admin/config/station-ops) route
  their role permits.
* Anonymous users can never reach a state-changing route.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
import yaml


# ── App fixture: real create_app with a public + a non-public station ─────


@pytest.fixture(scope="module")
def app_env():
    """Build a real dashboard app in test mode.

    One ``public: true`` station (PUB) and one ``public: false`` station
    (PRIV) so the per-station flag is exercised. All writable paths point at
    a throwaway temp dir so the suite never touches ``/opt/rovimen``.
    """
    tmp = Path(tempfile.mkdtemp(prefix="rovimen_authz_"))

    users_path = tmp / "users.yaml"
    users_path.write_text(
        yaml.safe_dump(
            {
                "admin": {
                    "display_name": "Admin",
                    # werkzeug hash of "adminpw" is irrelevant — tests set the
                    # session directly rather than logging in through the form.
                    "password_hash": "",
                    "role": "admin",
                    "require_totp": False,
                },
                "viewer": {
                    "display_name": "Viewer",
                    "password_hash": "",
                    "role": "visitor",
                    "require_totp": False,
                },
            }
        )
    )

    config_path = tmp / "dashboard_config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "correlation_window_s": 1,
                "station_api_port": 7779,
                "stations": {
                    "pub_station": {
                        "ip": "127.0.0.1",
                        "label": "Public Station",
                        "ssh_user": "test",
                        "public": True,
                        "lat": 52.5,
                        "lon": 13.4,
                        "cameras": [
                            {"code": "PUB001", "cam_ip": "127.0.0.1",
                             "label": "N", "rotate": False},
                        ],
                    },
                    "priv_station": {
                        "ip": "127.0.0.1",
                        "label": "Private Station",
                        "ssh_user": "test",
                        "public": False,
                        "lat": 44.4,
                        "lon": 26.1,
                        "cameras": [
                            {"code": "PRV001", "cam_ip": "127.0.0.1",
                             "label": "S", "rotate": False},
                        ],
                    },
                    "comm_station": {
                        "ip": "127.0.0.1",
                        "label": "Commissioning Station",
                        "ssh_user": "test",
                        "public": True,
                        "status": "commissioning",
                        "lat": 45.1,
                        "lon": 25.1,
                        "cameras": [
                            {"code": "COM001", "cam_ip": "127.0.0.1",
                             "label": "E", "rotate": False},
                        ],
                    },
                },
            }
        )
    )

    os.environ["ROVIMEN_USERS_PATH"] = str(users_path)
    os.environ["ROVIMEN_SECRET_KEY"] = "authz-test-secret"
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

    # These are process-global knobs on the shared ``security`` module.
    # Snapshot and restore them on teardown so this module-scoped fixture
    # never leaks lowered lockout thresholds into e.g. test_security.py.
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


@pytest.fixture
def anon(app_env):
    """Test client with no session (anonymous public visitor)."""
    return app_env.test_client()


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
def viewer(app_env):
    """Logged-in read-only account (fleet-wide read, no writes)."""
    c = app_env.test_client()
    with c.session_transaction() as sess:
        sess["user"] = "viewer"
        sess["role"] = "visitor"
        sess["stations"] = []
        sess["mfa"] = "totp"
    return c


# A single dummy media filename that matches every extension regex; the
# handler enforces the public flag BEFORE it ever tries to fetch the file,
# so anon 404s for private stations regardless of whether the file exists.
_STACK = "PUB001_20260101_010101_stack.webp"
_MKV = "PUB001_20260101_010101.mkv"
_MP4 = "PUB001_20260101_010101.mp4"
_DATE = "20260101"


# ── 1. Public read routes: anon reaches them, public station → not 302/401 ─


@pytest.mark.parametrize(
    "path", ["/", "/network", "/events", "/showers", "/highlights"]
)
def test_anon_can_load_public_page_shells(anon, path):
    resp = anon.get(path)
    assert resp.status_code == 200, f"{path} should be anon-public"


def test_anon_overview_and_stations_return_200(anon):
    assert anon.get("/api/overview").status_code == 200
    assert anon.get("/api/stations").status_code == 200


def test_anon_shower_reference_is_public(anon):
    assert anon.get("/api/showers").status_code == 200
    profiles = anon.get("/api/shower-profiles")
    assert profiles.status_code == 200 and "PER" in profiles.get_json()["showers"]
    assert anon.get("/api/shower-year-counts").status_code == 200


# ── 2. Per-station public flag on the overview/list surface ───────────────


def test_anon_overview_hides_non_public_station(anon):
    data = anon.get("/api/overview").get_json()
    assert "pub_station" in data
    assert "priv_station" not in data, "private station must not leak to anon"


def test_anon_stations_hides_non_public_station(anon):
    data = anon.get("/api/stations").get_json()
    assert "pub_station" in data
    assert "priv_station" not in data


def test_logged_in_overview_shows_all_stations(viewer):
    data = viewer.get("/api/overview").get_json()
    assert "pub_station" in data and "priv_station" in data


def test_logged_in_stations_shows_all_stations(admin):
    data = admin.get("/api/stations").get_json()
    assert "pub_station" in data and "priv_station" in data


def test_anon_station_page_public_ok_private_404(anon):
    assert anon.get("/station/pub_station").status_code == 200
    assert anon.get("/station/priv_station").status_code == 404


def test_logged_in_station_page_reaches_private(viewer):
    assert viewer.get("/station/pub_station").status_code == 200
    assert viewer.get("/station/priv_station").status_code == 200


# ── 3. Media handlers enforce the public flag for anon, not for logged-in ─


_MEDIA_ROUTES = [
    ("/timelapse/{h}/{c}/{d}/" + _MP4),
    ("/stack/{h}/{c}/{d}/" + _STACK),
    ("/thumbnail/{h}/{c}/{d}/" + _STACK),
    ("/video/{h}/{c}/{d}/" + _MKV),
    ("/download/{h}/{c}/{d}/" + _MKV),
    ("/fullstack/{h}/{c}/{d}/" + _STACK),
    ("/night_stack/{h}/{c}/{d}/" + _STACK),
    ("/timelapse_download/{h}/{c}/{d}/" + _MP4),
]


@pytest.mark.parametrize("tmpl", _MEDIA_ROUTES)
def test_anon_media_private_station_is_404(anon, tmpl):
    """Every public-tagged media handler must 404 a private station for anon,
    regardless of whether the underlying file exists."""
    path = tmpl.format(h="priv_station", c="PRV001", d=_DATE)
    resp = anon.get(path)
    assert resp.status_code == 404, f"{path} must 404 for anon (private station)"


@pytest.mark.parametrize("tmpl", _MEDIA_ROUTES)
def test_anon_media_public_station_not_gated_out(anon, tmpl):
    """For a public station, anon must NOT be blocked by the auth gate or the
    public-flag check: the response is whatever the media layer produces
    (typically 404/502 because no real file/station exists in the test), but
    never a 302-to-login and never the private-station 404-before-fetch. We
    assert it is not a redirect and not a 401/403 (i.e. the gate let it in)."""
    path = tmpl.format(h="pub_station", c="PUB001", d=_DATE)
    resp = anon.get(path)
    assert resp.status_code not in (301, 302, 401, 403), (
        f"{path} should pass the gate for a public station (got {resp.status_code})"
    )


@pytest.mark.parametrize("tmpl", _MEDIA_ROUTES)
def test_logged_in_media_reaches_private_station(admin, tmpl):
    """Logged-in users are never blocked by the public flag — the private
    station must not 404-before-fetch for them (it proceeds into the media
    layer, which may 404/502 on the missing file, but not the flag 404)."""
    path = tmpl.format(h="priv_station", c="PRV001", d=_DATE)
    resp = admin.get(path)
    assert resp.status_code not in (301, 302, 401, 403)


# The public rule applies to the camera served, not only to the host in the
# URL: the media layer reads the archive by camera code, so a public host_key
# with a private station's camera code used to serve that camera's media. A
# missing file answers the shared "not uploaded yet" JSON 404 from the media
# layer, which tells "the gate let it through" apart from the gate's own 404.


def _reached_media_layer(resp) -> bool:
    body = resp.get_json(silent=True) or {}
    return resp.status_code == 404 and body.get("error") == "not_uploaded_yet"


@pytest.mark.parametrize("tmpl", _MEDIA_ROUTES)
def test_anon_media_public_host_private_camera_is_404(anon, tmpl):
    path = tmpl.format(h="pub_station", c="PRV001", d=_DATE)
    resp = anon.get(path)
    assert resp.status_code == 404, path
    assert not _reached_media_layer(resp), (
        f"{path}: a private station's camera must 404 at the gate for anon")


@pytest.mark.parametrize("tmpl", _MEDIA_ROUTES)
def test_anon_media_public_host_retired_camera_passes_gate(anon, tmpl):
    """A camera no current station owns (retired, moved) keeps working on the
    public pages under a public host."""
    path = tmpl.format(h="pub_station", c="OLD001", d=_DATE)
    assert _reached_media_layer(anon.get(path)), path


@pytest.mark.parametrize("tmpl", _MEDIA_ROUTES)
def test_logged_in_media_public_host_private_camera_passes_gate(admin, tmpl):
    path = tmpl.format(h="pub_station", c="PRV001", d=_DATE)
    assert _reached_media_layer(admin.get(path)), path


@pytest.mark.parametrize("who", ["anon", "admin"])
def test_fullstack_archive_miss_never_contacts_station(request, who, monkeypatch):
    """An uploaded stack is served from the archive, never pulled from the
    station: an archive miss falls through to the thumbnail tiers, whose
    miss is the shared "not uploaded yet" 404."""
    import routes.media as media_routes

    contacted: list[str] = []

    def _record(url):
        contacted.append(url)
        raise RuntimeError("station contacted")
    monkeypatch.setattr(media_routes, "_session_for_url", _record)
    client = request.getfixturevalue(who)
    resp = client.get(f"/fullstack/pub_station/PUB001/{_DATE}/{_STACK}")
    assert contacted == []
    assert _reached_media_layer(resp)


def test_anon_cached_video_public_host_private_camera_404(anon):
    resp = anon.get(f"/api/cached-video/pub_station/PRV001/{_DATE}/{_MKV}")
    assert resp.status_code == 404


def test_anon_cached_video_public_host_retired_camera_redirects(anon):
    resp = anon.get(f"/api/cached-video/pub_station/OLD001/{_DATE}/{_MKV}")
    assert resp.status_code == 302
    assert "/media/v1/clip/OLD001/2026-01-01/" in resp.headers.get("Location", "")


_CAMERA_JSON_ROUTES = [
    "/api/rms/plots/{h}/{c}/" + _DATE,
    "/api/rms/plots-latest/{h}/{c}",
    "/api/rms/color-stack-latest/{h}/{c}",
]


@pytest.mark.parametrize("tmpl", _CAMERA_JSON_ROUTES)
def test_anon_rms_json_public_host_private_camera_404(anon, tmpl):
    assert anon.get(tmpl.format(h="pub_station", c="PRV001")).status_code == 404


@pytest.mark.parametrize("tmpl", _CAMERA_JSON_ROUTES)
def test_anon_rms_json_public_host_retired_camera_200(anon, tmpl):
    assert anon.get(tmpl.format(h="pub_station", c="OLD001")).status_code == 200


@pytest.mark.parametrize("tmpl", _CAMERA_JSON_ROUTES)
def test_logged_in_rms_json_public_host_private_camera_200(admin, tmpl):
    assert admin.get(tmpl.format(h="pub_station", c="PRV001")).status_code == 200


def test_anon_plots_latest_batch_drops_private_camera(anon, admin):
    cams = "pub_station/PUB001,pub_station/PRV001,pub_station/OLD001"
    data = anon.get(f"/api/rms/plots-latest?cams={cams}").get_json()
    assert set(data) == {"pub_station/PUB001", "pub_station/OLD001"}
    data = admin.get(f"/api/rms/plots-latest?cams={cams}").get_json()
    assert "pub_station/PRV001" in data


def test_anon_rms_plot_image_public_host_private_camera_404(anon, admin, monkeypatch, tmp_path):
    """The plot image is read from the archive by camera: with the file
    present, anon gets 404 for a private station's camera under a public
    host, while a logged-in caller gets the image."""
    import routes.station_ops as station_ops

    plot = tmp_path / "PRV001" / _DATE / "rms" / _PRIV_PLOT_JPG
    plot.parent.mkdir(parents=True)
    plot.write_bytes(b"\xff\xd8\xff\xe0 not a real jpeg")
    monkeypatch.setattr(station_ops, "ARCHIVE_PATH", tmp_path)
    monkeypatch.setattr(station_ops, "THUMB_CACHE_DIR", tmp_path / "thumb_cache")

    path = f"/api/rms/plot_image/pub_station/PRV001/{_DATE}/{_PRIV_PLOT_JPG}"
    assert anon.get(path).status_code == 404
    assert admin.get(path).status_code == 200


# ── 4. Sensitive surfaces stay login-gated for anon ───────────────────────


_ANON_GATED_GET = [
    "/admin",
    "/config",
    "/social",
    # /api/live-feed moved to the anon-public set: it now carries
    # page="live" (the /live PAGE was already anon-eligible but its data
    # route stayed gated, leaving anonymous visitors a fetch error) and
    # applies the per-station public filter — see test_hall_of_fame.py
    # TestLiveFeedAnon for the filtering + toggle coverage.
    "/api/status/pub_station",
    "/api/vitals/pub_station",
    "/api/admin/usage-stats",
    "/api/admin/traffic",
    "/api/admin/users",
    "/api/config/all",
    "/api/settings/pub_station",
    "/api/station_night/pub_station/20261021",
    # Fleet-map health data is gated unless the "overview" page is enabled;
    # in this default-all fixture it IS enabled, so /api/overview is reachable
    # — but the fleet-map PAGE is served at "/" only for logged-in users.
]


@pytest.mark.parametrize("path", _ANON_GATED_GET)
def test_anon_sensitive_get_is_gated(anon, path):
    """Anon → login-redirect (browser page) or 401/403 (API). Never 200."""
    resp = anon.get(path)
    assert resp.status_code in (302, 401, 403), (
        f"{path} must be gated for anon, got {resp.status_code}"
    )


def test_traffic_report_is_admin_only(viewer, admin):
    """/api/admin/traffic: 403 for non-admin sessions, JSON report for admin
    (same guard pattern as /api/admin/usage-stats)."""
    assert viewer.get("/api/admin/traffic").status_code == 403
    resp = admin.get("/api/admin/traffic?days=7")
    assert resp.status_code == 200
    body = resp.get_json()
    assert "totals" in body and "countries" in body and "daily" in body


# ── 4b. Detection data + fleet map are anon-public under their toggles ─────
#
# The Detections page (/events, page="events") pulls its data from the
# detection-feed routes below; the fleet map (/map, page="overview") pulls
# from /api/overview + /api/stations. With the default-all fixture both
# toggles are on, so anon must reach every one of these — and each anon
# payload must carry ONLY public stations and NO ip / cam_ip admin fields.

_ANON_PUBLIC_DETECTION_GET = [
    "/api/detections/nights",
    "/api/detections/20260101",
    "/api/detections/range?from=20260101&to=20260102",
    "/api/platepar/pub_station",
    "/api/twilight/pub_station/20260101",
]


@pytest.mark.parametrize("path", _ANON_PUBLIC_DETECTION_GET)
def test_anon_detection_feed_reachable_under_events_toggle(anon, path):
    """Every data endpoint the public Detections page fetches must pass the
    gate for anon (never 302/401/403) when "events" is enabled. The response
    body may be empty (no live station in the test), but the gate must let it
    through."""
    resp = anon.get(path)
    assert resp.status_code not in (301, 302, 401, 403), (
        f"{path} should be anon-public under the events toggle, got {resp.status_code}"
    )


def test_anon_map_page_reachable_under_overview_toggle(anon):
    """With "overview" on, the map is on the Network overview ("/network");
    the old "/map" and "/stations" redirect there with their query string,
    and "/" is the home page, without the map."""
    for path in ("/map", "/stations"):
        resp = anon.get(path + "?station=pub_station")
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/network?station=pub_station")
    assert "leaflet" in anon.get("/network").get_data(as_text=True).lower()
    assert "leaflet" not in anon.get("/").get_data(as_text=True).lower()


def test_anon_root_renders_hero_plots_and_nav(anon):
    """The anon landing at "/" carries the hero/intro copy, the latest plots,
    and the top-nav links to the other public pages."""
    root = anon.get("/").get_data(as_text=True)
    # Hero intro copy (unique phrases from the required block).
    assert "ROVIMEN, Romanian Video Meteor Network" in root
    assert "Our video cameras watch the night sky" in root
    assert "Explore the public data below" in root
    # The latest plots, not the map (that is on the Network overview), and
    # the card pointing there.
    assert 'class="ov-cover-card" href="/network"' in root
    assert 'id="ov-stacks"' in root and 'id="map"' not in root
    # Top-nav links to the sibling public pages.
    for href in ('href="/network"', 'href="/events"', 'href="/highlights"', 'href="/showers"'):
        assert href in root, f"nav link {href} must be present on the map landing"
    # "About" opens the in-page panel.
    assert "openAppPanel('about')" in root


def test_logged_in_root_shows_the_hero(viewer):
    """Logged-in users see the same branded hero (logo, credits) on the home page."""
    root = viewer.get("/").get_data(as_text=True)
    assert "ROVIMEN, Romanian Video Meteor Network" in root


def test_anon_detections_payload_hides_private_station_and_ips(anon):
    """The per-date detections payload for anon must contain only public
    stations and must never carry ip / cam_ip anywhere in the body."""
    import json as _json

    resp = anon.get("/api/detections/20260101")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "priv_station" not in (data.get("by_station") or {}), (
        "private station must not appear in anon detections"
    )
    for det in data.get("detections") or []:
        assert det.get("host_key") != "priv_station"
    raw = _json.dumps(data)
    for banned in ("cam_ip", '"ip"', "127.0.0.1", "priv_station", "PRV001"):
        assert banned not in raw, f"anon detections payload leaked {banned}"


def test_anon_detections_range_hides_private_and_ips(anon):
    import json as _json

    resp = anon.get("/api/detections/range?from=20260101&to=20260102")
    assert resp.status_code == 200
    raw = _json.dumps(resp.get_json())
    for banned in ("cam_ip", '"ip"', "127.0.0.1", "priv_station", "PRV001"):
        assert banned not in raw, f"anon detections range leaked {banned}"


def test_detections_range_caps_the_window_at_92_days(anon):
    """The range route is public and computes one payload per night, so a
    window over 92 nights is refused with a JSON error the Sort & Filter tab
    shows; 92 nights inclusive still answers."""
    too_long = anon.get("/api/detections/range?from=20260101&to=20260403")
    assert too_long.status_code == 400
    assert "92 days" in too_long.get_json()["error"]
    ok = anon.get("/api/detections/range?from=20260101&to=20260402")
    assert ok.status_code == 200


def test_anon_overview_map_apis_hide_private_and_ips(anon):
    """The map's data APIs (/api/overview, /api/stations) show only public
    stations to anon and redact ip / cam_ip."""
    import json as _json

    for path in ("/api/overview", "/api/stations"):
        data = anon.get(path).get_json()
        assert "pub_station" in data and "priv_station" not in data, path
        raw = _json.dumps(data)
        for banned in ("cam_ip", '"ip"', "127.0.0.1"):
            assert banned not in raw, f"{path} leaked {banned}"


def test_anon_platepar_private_station_404(anon):
    """A private/commissioning station's platepar must 404 for anon (its
    existence undisclosed), while a public station passes the gate."""
    assert anon.get("/api/platepar/priv_station").status_code == 404
    assert anon.get("/api/platepar/pub_station").status_code not in (301, 302, 401, 403)


def test_platepar_never_contacts_a_commissioning_station(viewer, monkeypatch):
    """The Events page asks for every station's platepar on each load. A
    commissioning station must not be contacted (``Station.should_contact``):
    its IP is a placeholder or an absent peer, so the call hung ~5 s and 503'd
    for gmnro21 (Vârteșcoiu) and gmnro22 (Ferigile)."""
    import routes.station_proxy as station_proxy

    def _no_contact(*a, **k):
        raise AssertionError("commissioning station was contacted")

    monkeypatch.setattr(station_proxy, "station_get_raw", _no_contact)
    assert viewer.get("/api/platepar/comm_station").status_code == 404


def test_anon_platepar_never_contacts_station(anon, monkeypatch):
    """Platepars are pushed (#1248). Without a pushed copy an anonymous
    request answers "not uploaded yet" and never makes the VPS call the
    station."""
    import pushed_calibration
    import routes.station_proxy as station_proxy

    contacted: list[str] = []

    def _record(*a, **k):
        contacted.append(a[2] if len(a) > 2 else "")
        raise RuntimeError("station contacted")
    monkeypatch.setattr(station_proxy, "station_get_raw", _record)
    monkeypatch.setattr(pushed_calibration, "get_platepar", lambda hk: None)
    resp = anon.get("/api/platepar/pub_station")
    assert contacted == []
    assert resp.status_code == 404
    assert resp.get_json()["error"] == "not_uploaded_yet"


def test_anon_platepar_serves_the_pushed_copy(anon, monkeypatch):
    import pushed_calibration

    pushed = {"PUB001": {"az_centre": 123.0}}
    monkeypatch.setattr(pushed_calibration, "get_platepar",
                        lambda hk: pushed if hk == "pub_station" else None)
    resp = anon.get("/api/platepar/pub_station")
    assert resp.status_code == 200
    assert resp.get_json() == pushed


def test_anon_twilight_private_station_404(anon):
    assert anon.get("/api/twilight/priv_station/20260101").status_code == 404
    assert anon.get("/api/twilight/pub_station/20260101").status_code == 200


def test_twilight_network_window_respects_public_flag(anon, viewer):
    """The fleet RMS window counts public stations only for anon callers;
    a logged-in caller gets the whole fleet. priv_station (Bucharest) is the
    easternmost, so it starts capture first."""
    a = anon.get("/api/twilight/network/20261021")
    assert a.status_code == 200
    assert "priv_station" not in (a.get_json()["earliest_station"], a.get_json()["latest_station"])
    v = viewer.get("/api/twilight/network/20261021").get_json()
    assert v["earliest_station"] == "priv_station"
    assert v["station_count"] > a.get_json()["station_count"]


def test_logged_in_detections_reaches_full_fleet(viewer):
    """A logged-in account keeps the full fleet in the detections payload —
    the private station is present (fleet-wide read rule)."""
    data = viewer.get("/api/detections/20260101").get_json()
    by_station = data.get("by_station") or {}
    assert "pub_station" in by_station and "priv_station" in by_station


def test_logged_in_platepar_and_twilight_reach_private(viewer):
    """The public-flag 404 never applies to a logged-in caller."""
    assert viewer.get("/api/platepar/priv_station").status_code not in (401, 403, 404)
    assert viewer.get("/api/twilight/priv_station/20260101").status_code == 200


# ── 4c. Public-experience polish: newly anon-public data endpoints ─────────
#
# The fleet map + Detections grid pull four more endpoints that used to be
# login-gated. They now carry @public_route(page=...) and must (a) pass the
# gate for anon, (b) show ONLY public stations to anon, (c) carry no ip /
# cam_ip anywhere, and (d) keep the full fleet for a logged-in caller.

_ANON_PUBLIC_POLISH_GET = [
    "/api/status/all",
    "/api/overview/stacks",
    "/api/footage/grid/20260101",
    "/api/detections/grid/20260101",
]


@pytest.mark.parametrize("path", _ANON_PUBLIC_POLISH_GET)
def test_anon_polish_endpoints_reachable(anon, path):
    """Each newly-public endpoint must return 200 for anon under the default-all
    toggle set — never a 302/401/403."""
    resp = anon.get(path)
    assert resp.status_code == 200, f"{path} got {resp.status_code}"


@pytest.mark.parametrize("path", _ANON_PUBLIC_POLISH_GET)
def test_anon_polish_payloads_hide_private_and_ips(anon, path):
    """No newly-public endpoint may leak a non-public station or any ip/cam_ip
    to an anonymous caller."""
    import json as _json

    raw = _json.dumps(anon.get(path).get_json())
    for banned in ("cam_ip", '"ip"', "127.0.0.1", "priv_station", "PRV001"):
        assert banned not in raw, f"{path} leaked {banned} to anon"


def test_anon_status_all_only_public_station_minimal_shape(anon):
    """/api/status/all for anon: only the public station, and only the safe
    online/label/coords projection — never the raw status dict internals."""
    data = anon.get("/api/status/all").get_json()
    assert "pub_station" in data and "priv_station" not in data
    entry = data["pub_station"]
    assert set(entry.keys()) <= {"online", "label", "lat", "lon", "show_on_map"}
    assert "services" not in entry and "disk" not in entry


def test_logged_in_status_all_full_fleet(viewer):
    """A logged-in account keeps the full fleet in /api/status/all (both the
    public and the private station key are present)."""
    data = viewer.get("/api/status/all").get_json()
    assert "pub_station" in data and "priv_station" in data


def test_logged_in_footage_and_detection_grid_full_fleet(viewer):
    """The grids keep every fleet camera as a column for a logged-in caller —
    the private station's camera is present."""
    for path in ("/api/footage/grid/20260101", "/api/detections/grid/20260101"):
        cols = viewer.get(path).get_json().get("columns") or []
        hosts = {c.get("host_key") for c in cols}
        assert "priv_station" in hosts, f"{path} must keep full fleet for operator"


def test_anon_grid_columns_only_public(anon):
    """Grid columns for anon contain only public-station cameras — the private
    station's camera never appears as a column or leaks its host_key."""
    for path in ("/api/footage/grid/20260101", "/api/detections/grid/20260101"):
        cols = anon.get(path).get_json().get("columns") or []
        hosts = {c.get("host_key") for c in cols}
        assert "priv_station" not in hosts, f"{path} leaked private column to anon"


# ── 4d. Anonymous video clips: redirect to keyless media, private → 404 ────


def test_anon_cached_video_public_redirects_to_media_surface(anon):
    """For a public station's clip, anon /api/cached-video 302-redirects to the
    keyless /media/v1/clip surface (preserving ?format=mp4), rather than the
    gated station proxy."""
    resp = anon.get(
        f"/api/cached-video/pub_station/PUB001/{_DATE}/{_MKV}?format=mp4"
    )
    assert resp.status_code == 302, resp.status_code
    loc = resp.headers.get("Location", "")
    assert "/media/v1/clip/PUB001/2026-01-01/" + _MKV in loc, loc
    assert "format=mp4" in loc


def test_anon_cached_video_private_station_404(anon):
    """A private/commissioning station's clip is 404 for anon (existence
    undisclosed) — never redirected to the media surface."""
    resp = anon.get(f"/api/cached-video/priv_station/PRV001/{_DATE}/{_MKV}")
    assert resp.status_code == 404


def test_logged_in_cached_video_uses_station_proxy_not_redirect(admin):
    """A logged-in operator keeps the station-proxy path — NOT the anon media
    redirect. With no real station in the test it fails downstream (404/502/
    400), but it must never be the 302-to-media the anon branch emits."""
    resp = admin.get(
        f"/api/cached-video/pub_station/PUB001/{_DATE}/{_MKV}?format=mp4"
    )
    assert resp.status_code != 302, "operator must not hit the anon media redirect"


# ── 4e. RMS plot list + images: anon-public for public stations only ───────
#
# The overview "Latest Plots and Data" grid lists each camera's RMS plot
# images (/api/rms/plots/...) and renders each one (/api/rms/plot_image/...).
# Both now carry @public_route(page="overview") and must (a) pass the gate for
# a public station, (b) 404 a private station BEFORE any station round-trip /
# file read, (c) never leak admin fields or host paths in the plot list, and
# (d) keep the full fleet for a logged-in caller.

_PLOT_JPG = "PUB001_20260101_010101_captured_stack.jpg"
_PRIV_PLOT_JPG = "PRV001_20260101_010101_captured_stack.jpg"


def test_anon_rms_plots_public_station_passes_gate(anon):
    """A public station's plot list passes the gate for anon (never
    302/401/403). The body is an empty list, since nothing is archived in the
    test, but the gate and public-flag check let it through rather than
    404-before-lookup."""
    resp = anon.get(f"/api/rms/plots/pub_station/PUB001/{_DATE}")
    assert resp.status_code not in (301, 302, 401, 403, 404), (
        f"public-station plot list must pass the gate for anon, got {resp.status_code}"
    )


def test_anon_rms_plots_private_station_404(anon):
    """A private/commissioning station's plot list must 404 for anon before any
    archive lookup (its plots must never be served)."""
    resp = anon.get(f"/api/rms/plots/priv_station/PRV001/{_DATE}")
    assert resp.status_code == 404


def test_anon_rms_plot_image_public_station_passes_gate(anon):
    """A public station's plot image passes the gate for anon. With nothing
    archived the image route 404s, but never a 302-to-login."""
    resp = anon.get(
        f"/api/rms/plot_image/pub_station/PUB001/{_DATE}/{_PLOT_JPG}"
    )
    assert resp.status_code not in (301, 302, 401, 403), (
        f"public-station plot image must pass the gate for anon, got {resp.status_code}"
    )


def test_anon_rms_plot_image_private_station_404(anon):
    """A private/commissioning station's plot image must 404 for anon before
    any file read — non-public plots are never served."""
    resp = anon.get(
        f"/api/rms/plot_image/priv_station/PRV001/{_DATE}/{_PRIV_PLOT_JPG}"
    )
    assert resp.status_code == 404


def test_anon_rms_plots_payload_has_no_admin_fields_or_paths(anon, monkeypatch):
    """The anon plot-list payload must carry ONLY the per-plot filename / label
    / order projection — never ip / cam_ip / host paths / host_key. The list
    is built from the archive index, so we give the index a night with two
    plots, then assert the shape and that no admin field or path leaks into
    the serialised body."""
    import json as _json

    import rovimen_dashboard as rd
    from cache_store import _NightIndex

    night = _NightIndex(
        meteor_files=(), stack_files=frozenset(), timelapse_files=(),
        rms_files=frozenset({
            _PLOT_JPG, "PUB001_20260101_010101_photometry_variation.png",
        }),
    )
    monkeypatch.setattr(
        rd._archive_idx, "night_files",
        lambda cam, date: night if cam == "PUB001" else None,
    )

    resp = anon.get(f"/api/rms/plots/pub_station/PUB001/{_DATE}")
    assert resp.status_code == 200, resp.status_code
    data = resp.get_json()
    assert isinstance(data, list) and data, "expected a non-empty plot list"
    for entry in data:
        assert set(entry.keys()) <= {"filename", "label", "order"}, (
            f"plot entry leaked an unexpected field: {entry.keys()}"
        )
    raw = _json.dumps(data)
    for banned in ("cam_ip", '"ip"', "127.0.0.1", "host_key", "ssh_user",
                   "/home/", "priv_station", "PRV001"):
        assert banned not in raw, f"anon plot-list payload leaked {banned}"


def test_logged_in_rms_plots_reaches_private_station(admin):
    """A logged-in operator keeps full-fleet access: the private station's plot
    list must NOT 404-before-lookup (it proceeds to the archive lookup, which
    answers an empty list here, but never the flag 404)."""
    resp = admin.get(f"/api/rms/plots/priv_station/PRV001/{_DATE}")
    assert resp.status_code not in (301, 302, 401, 403, 404)


def test_logged_in_rms_plot_image_reaches_private_station(admin):
    """The public-flag 404 never applies to a logged-in caller for plot images
    either — the private station proceeds into the media layer."""
    resp = admin.get(
        f"/api/rms/plot_image/priv_station/PRV001/{_DATE}/{_PRIV_PLOT_JPG}"
    )
    assert resp.status_code not in (301, 302, 401, 403)


# ── 5. Anon can never reach a state-changing route ────────────────────────


_ANON_MUTATING = [
    ("POST", "/api/admin/users"),
    ("POST", "/api/reboot/pub_station"),
    ("POST", "/api/rovimen/restart/pub_station"),
    ("PATCH", "/api/settings/pub_station"),
    ("POST", "/api/probe/pub_station"),
    ("POST", "/api/compilation"),
]


@pytest.mark.parametrize("method,path", _ANON_MUTATING)
def test_anon_mutations_blocked(anon, method, path):
    resp = anon.open(path, method=method, json={})
    assert not (200 <= resp.status_code < 300), (
        f"{method} {path} must never succeed (2xx) for anon, got {resp.status_code}"
    )
    assert resp.status_code in (302, 401, 403, 404, 405), (
        f"{method} {path} unexpected status for anon: {resp.status_code}"
    )


# ── 6. The gate is fail-closed: an un-tagged route stays gated ────────────


def test_untagged_route_is_login_gated_for_anon():
    """A route WITHOUT @public_route is denied to anon by install_auth_gate,
    while a sibling tagged route is allowed — proving the default is deny.

    Uses a standalone minimal app so we drive the real gate directly."""
    from flask import Flask, jsonify

    import security

    app = Flask(__name__)
    app.secret_key = "x"
    security.install_auth_gate(app)

    @app.route("/tagged")
    @security.public_route
    def _tagged():  # pragma: no cover - only status matters
        return jsonify(ok=True)

    @app.route("/api/untagged")
    def _untagged():  # pragma: no cover - only status matters
        return jsonify(ok=True)

    client = app.test_client()
    assert client.get("/tagged").status_code == 200
    # API path with no session and no tag → 401 (fail closed).
    assert client.get("/api/untagged").status_code == 401


def test_public_route_marker_is_visible_on_view():
    """The @public_route decorator tags the view function so install_auth_gate
    can find it. Unit-level guard for the mechanism itself."""
    import security

    @security.public_route
    def _v():
        return "ok"

    assert getattr(_v, security._PUBLIC_ROUTE_ATTR, False) is True


# ── Camera-keyed public rules, pinned per route ───────────────────────────
#
# The camera-keyed surfaces below map a camera code to its owning station(s)
# through the shared helpers in ``auth`` (camera_owner, camera_owner_visible,
# public_camera_codes, public_host_keys). Their rules differ from
# ``camera_is_public_for_request`` on purpose: a camera no station owns
# (OLD001) is refused to anonymous visitors here, while the host-keyed media
# routes let it through. PUB001 belongs to the public station, PRV001 to the
# private one.

_HL_MEDIA = [
    "/api/highlights/clip/{c}/2026-01-01/{c}_20260101_010101_color.mp4",
    "/api/highlights/stack/{c}/2026-01-01/{c}_20260101_010101_stack.webp",
    "/api/highlights/shortclip/{c}/2026-01-01/{c}_20260101_010101_color.mp4",
]


@pytest.mark.parametrize("tmpl", _HL_MEDIA)
@pytest.mark.parametrize("cam,passes", [("PUB001", True), ("PRV001", False), ("OLD001", False)])
def test_anon_highlights_media_gate_per_camera_owner(anon, monkeypatch, tmpl, cam, passes):
    import public_api

    looked_up: list[str] = []

    def _resolve(camera, date_iso, filename, subdir):
        looked_up.append(camera)
        return None

    monkeypatch.setattr(public_api, "_resolve_media_path", _resolve)
    resp = anon.get(tmpl.format(c=cam))
    assert resp.status_code == 404
    assert bool(looked_up) is passes, f"{tmpl} {cam}: gate let through={bool(looked_up)}"


@pytest.mark.parametrize("cam", ["PUB001", "PRV001", "OLD001"])
def test_logged_in_highlights_media_gate_passes_every_camera(admin, monkeypatch, cam):
    import public_api

    looked_up: list[str] = []
    monkeypatch.setattr(public_api, "_resolve_media_path",
                        lambda camera, *a: looked_up.append(camera))
    admin.get(_HL_MEDIA[0].format(c=cam))
    assert looked_up == [cam]


def _report(cameras: list[str]):
    import fireball_reports as fr

    return fr.Report.model_validate({
        "event_id": "20260101_010101", "title": "Test fireball",
        "time_utc": "2026-01-01 01:01:01", "analysed": "2026-01-05",
        "status": "final", "authors": "A", "summary": ["Bright."],
        "cameras": cameras, "facts": [],
        "clips": [{"camera": c, "date": "2026-01-01",
                   "filename": f"{c}_20260101_010100_color.mkv"} for c in cameras],
    })


@pytest.fixture
def one_report(monkeypatch):
    import fireball_reports as fr

    def _install(cameras: list[str]):
        rep = _report(cameras)
        monkeypatch.setattr(fr, "all_reports", lambda reports_dir=None: {rep.event_id: rep})
        return rep
    return _install


@pytest.mark.parametrize("cam,status", [("PUB001", 200), ("PRV001", 404), ("OLD001", 404)])
def test_anon_fireball_report_page_per_camera_owner(anon, one_report, cam, status):
    one_report([cam])
    assert anon.get("/fireball/20260101_010101").status_code == status


@pytest.mark.parametrize("cam,status", [("PUB001", 200), ("PRV001", 200), ("OLD001", 404)])
def test_logged_in_fireball_report_page_per_camera_owner(admin, one_report, cam, status):
    one_report([cam])
    assert admin.get("/fireball/20260101_010101").status_code == status


def test_highlights_reports_list_cameras_per_owner(anon, admin, one_report):
    one_report(["PUB001", "PRV001", "OLD001"])
    (rep,) = anon.get("/api/highlights/reports").get_json()["reports"]
    assert rep["cameras"] == ["PUB001"]
    assert [(w["camera"], w["host_key"]) for w in rep["witnesses"]] == [("PUB001", "pub_station")]
    assert rep["places"] == ["Public Station"]
    (rep,) = admin.get("/api/highlights/reports").get_json()["reports"]
    assert rep["cameras"] == ["PUB001", "PRV001"]
    assert [(w["camera"], w["host_key"]) for w in rep["witnesses"]] == [
        ("PUB001", "pub_station"), ("PRV001", "priv_station")]


@pytest.mark.parametrize("cam", ["PRV001", "OLD001"])
def test_anon_highlights_reports_hide_a_report_with_no_public_camera(anon, one_report, cam):
    one_report([cam])
    assert anon.get("/api/highlights/reports").get_json()["reports"] == []


def test_hall_of_fame_list_per_camera_owner(anon, admin, monkeypatch, tmp_path):
    import hall_of_fame_store

    monkeypatch.setattr(hall_of_fame_store, "_PATH", tmp_path / "hall_of_fame.json")
    monkeypatch.setattr(hall_of_fame_store, "_LOCK_PATH", tmp_path / "hof.lock")
    for i, cam in enumerate(("PUB001", "PRV001", "OLD001")):
        hall_of_fame_store.add({
            "id": f"hof_{i:012d}", "camera": cam, "date": "2026-01-01",
            "filename": f"{cam}_20260101_01010{i}_color.mkv", "order": i,
            "added_at": f"2026-01-0{i + 1}T00:00:00+00:00", "added_by": "admin",
        })
    anon_entries = anon.get("/api/hall_of_fame").get_json()["entries"]
    assert [(e["camera"], e["host_key"]) for e in anon_entries] == [("PUB001", "pub_station")]
    admin_entries = admin.get("/api/hall_of_fame").get_json()["entries"]
    assert sorted((e["camera"], e["host_key"]) for e in admin_entries) == [
        ("OLD001", None), ("PRV001", "priv_station"), ("PUB001", "pub_station")]


def test_camera_lookup_helpers_first_owner_vs_any_owner():
    """A camera listed under two stations (registry drift): the report rules
    follow the FIRST owner, the set-based anon filters (highlights data,
    detections, the hall of fame) pass it when ANY owner is public, and
    ``camera_is_public_for_request`` would need ALL owners public."""
    from auth import (
        camera_owner, camera_owner_visible, public_camera_codes, public_host_keys,
    )
    from models import DashboardConfig

    def _st(public: bool, *codes: str) -> dict:
        return {"ip": "127.0.0.1", "label": "x", "ssh_user": "t", "public": public,
                "cameras": [{"code": c, "cam_ip": "127.0.0.1"} for c in codes]}

    cfg = DashboardConfig.model_validate({"stations": {
        "priv": _st(False, "DUP001", "PRV001"),
        "pub": _st(True, "DUP001", "PUB001"),
    }})
    assert camera_owner(cfg, "dup001")[0] == "priv"
    assert camera_owner(cfg, "OLD001") == (None, None)
    assert camera_owner_visible(cfg, "DUP001", anonymous=True) is False
    assert camera_owner_visible(cfg, "DUP001", anonymous=False) is True
    assert camera_owner_visible(cfg, "OLD001", anonymous=False) is False
    assert public_camera_codes(cfg) == {"DUP001", "PUB001"}
    assert public_host_keys(cfg) == {"pub"}
