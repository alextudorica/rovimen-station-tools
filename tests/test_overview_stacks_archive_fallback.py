"""``/api/overview/stacks`` resolves every camera from the archive, never the
station, and never renders a camera as "no data" while its last plots sit
in the archive.

The grid shows one tile per camera and paints every camera MISSING from the
payload as "no data". The fan-out that fills it used to ask the STATION
first, so a station that was offline contributed nothing -- and the
"last-known-good" cache that was supposed to cover that is per-worker and
in-memory, so it was empty for every station that had been offline since the
last dashboard restart. Six of twenty stations rendered as a wall of blank
tiles on 2026-09-09 with yesterday's stacks on the storage box the whole time.

The archive is the durable copy and the only one that can be SERVED when the
station is gone, so the fan-out now reads nothing else (#1247). What the
operator sees for a quiet camera is a stale tile with its real date, coloured
by age -- which is the invariant: uploaded once, never missing again.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

PROJECT = Path(__file__).resolve().parent.parent
FIXTURES = PROJECT / "tests" / "e2e" / "fixtures"
sys.path.insert(0, str(PROJECT / "dashboard"))

LIVE_CAM = "TST001"     # only the station has it -> absent (not uploaded yet)
DARK_CAM = "TST002"     # archived plots -> archive entry
BARE_CAM = "TST003"     # nothing anywhere -> stays absent ("no data")
OFFLINE_CAM = "TST004"  # station known offline -> archive entry
NS_CAM = "TST005"       # no RMS plots at all -> night-stack fallback (rms: false cams)


def _ymd(days_ago: int) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y%m%d")


@pytest.fixture
def archive(monkeypatch):
    """A fake storage box with plots for the dark cameras only."""
    root = Path(tempfile.mkdtemp(prefix="rovimen_ovarchive_test_"))
    for cam, day in ((DARK_CAM, _ymd(1)), (OFFLINE_CAM, _ymd(1))):
        rms = root / cam / day / "rms"
        rms.mkdir(parents=True)
        for name in (
            f"{cam}_{day}_170000_000000_captured_stack.jpg",
            f"{cam}_{day}_170000_000000_radiants.png",
        ):
            (rms / name).write_bytes(b"x")

    # NS_CAM archives no rms/ at all — only the dawn pipeline's per-night
    # composite, the way a colour/spectro-only (rms: false) camera does.
    from PIL import Image
    ns_day = _ymd(1)
    tl = root / NS_CAM / ns_day / "timelapse"
    tl.mkdir(parents=True)
    Image.new("RGB", (16, 16), (200, 30, 30)).save(
        tl / f"{NS_CAM}_{ns_day}_night_stack.webp", format="WEBP",
    )

    import cache_store
    import routes.archive as ra
    import routes.station_ops as so
    monkeypatch.setattr(cache_store, "ARCHIVE_PATH", root)
    monkeypatch.setattr(ra, "ARCHIVE_PATH", root)
    # station_ops binds ARCHIVE_PATH at import; when another test module
    # imported it first (full-suite order), the constant points at that
    # module's archive and the night-stack route 404s.
    monkeypatch.setattr(so, "ARCHIVE_PATH", root)
    return root


@pytest.fixture
def app(archive):
    tmp = Path(tempfile.mkdtemp(prefix="rovimen_ovarchive_app_"))
    os.environ["ROVIMEN_USERS_PATH"] = str(FIXTURES / "users.yaml")
    os.environ["ROVIMEN_SECRET_KEY"] = "ovarchive-test-key"
    os.environ["ROVIMEN_COOKIE_SECURE"] = "0"
    os.environ["RATELIMIT_ENABLED"] = "0"
    os.environ["ROVIMEN_API_KEYS_REQUIRED"] = "0"
    os.environ["ROVIMEN_DISABLE_BACKGROUND"] = "1"
    os.environ["THUMB_CACHE_DIR"] = str(tmp / "thumb_cache")
    os.environ["ROVIMEN_CACHE_PATH"] = str(tmp / "cache")
    os.environ["ROVIMEN_ARCHIVE_PATH"] = str(archive)
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

    def _station(cams: list[str], label: str) -> dict:
        return {
            "cameras": [
                {"code": c, "label": "N", "cam_ip": "127.0.0.1",
                 "rotate": False, "az": 0.0, "alt": 45.0}
                for c in cams
            ],
            "ip": "127.0.0.1", "jump_hosts": [], "label": label,
            "lat": 45.0, "lon": 25.0, "proxy_media": False, "public": True,
            "public_tabs": [], "ssh_user": "test", "location_name": label,
            "show_on_map": True, "status": "active",
        }

    config_data = {
        "correlation_window_s": 1,
        "station_api_port": 7779,
        "stations": {
            "teststn1": _station([LIVE_CAM, DARK_CAM, BARE_CAM, NS_CAM], "Test Alpha"),
            "teststn2": _station([OFFLINE_CAM], "Test Beta"),
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
def station(monkeypatch):
    """Station stub: only LIVE_CAM has anything. Records what was polled, so
    a test can prove the fan-out never asked."""
    polled: list[tuple[str, str]] = []

    def station_get_raw(config, tunnels, host_key, path, timeout=10):
        polled.append((host_key, path))
        if path.startswith("/api/nights/"):
            cam = path.rsplit("/", 1)[-1]
            return [_ymd(0)] if cam == LIVE_CAM else []
        if path.startswith("/api/rms/plots/"):
            cam, date = path.split("/")[4], path.split("/")[5]
            return [{
                "filename": f"{cam}_{date}_180000_000000_captured_stack.jpg",
                "label": "Captured stack",
            }]
        return []

    import routes.overview as ov
    monkeypatch.setattr(ov, "station_get_raw", station_get_raw)
    return polled


@pytest.fixture
def offline_stations(monkeypatch):
    """Report teststn2 as known-offline, like the status poller would."""
    import station_client
    monkeypatch.setattr(
        station_client.StationCache, "get_status",
        lambda self, host_key: {"online": host_key != "teststn2"},
    )


@pytest.fixture
def client(app):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["user"] = "alice"
        sess["role"] = "admin"
        sess["stations"] = []
    return c


def _by_cam(client) -> dict[str, dict]:
    r = client.get("/api/overview/stacks")
    assert r.status_code == 200
    return {e["cam"]: e for e in r.get_json()}


def test_a_silent_station_still_gets_its_last_stack(app, client, station):
    """THE BUG: the station has nothing, so the tile went blank."""
    app._refresh_overview_stacks()
    entries = _by_cam(client)

    assert DARK_CAM in entries, "camera with archived plots rendered as no data"
    assert entries[DARK_CAM]["date"] == _ymd(1)
    assert entries[DARK_CAM]["filename"].endswith("_captured_stack.jpg")
    assert entries[DARK_CAM]["label"] == "Captured stack"


def test_the_fan_out_never_contacts_a_station(app, client, station):
    """Every tile comes from the archive: no station call at all, even for a
    camera whose station holds a newer stack than the archive. That camera
    shows up once its night is uploaded, not before."""
    app._refresh_overview_stacks()
    entries = _by_cam(client)
    assert station == [], f"overview fan-out contacted a station: {station}"
    assert LIVE_CAM not in entries, "station-only stack reached the grid"


def test_a_camera_with_nothing_anywhere_stays_absent(app, client, station):
    """"No data" is still the honest answer for a camera that has never
    uploaded a plot -- the fix is about not claiming it for cameras that have."""
    app._refresh_overview_stacks()
    assert BARE_CAM not in _by_cam(client)


def test_a_known_offline_station_is_resolved_without_being_polled(
    app, client, station, offline_stations,
):
    """No live call for a station the poller already knows is down (that only
    burns the station timeout), but its tiles must still fill in."""
    app._refresh_overview_stacks()
    entries = _by_cam(client)

    assert OFFLINE_CAM in entries
    assert entries[OFFLINE_CAM]["date"] == _ymd(1)
    assert not [p for p in station if p[0] == "teststn2"], (
        f"offline station was polled anyway: {station}"
    )


def test_a_card_never_moves_backwards_in_time(app, client, station, monkeypatch):
    """A refresh tick whose archive answer is older than the cached card (a
    storage-box scan that timed out and served a stale listing) must not
    regress the grid."""
    app._refresh_overview_stacks()
    assert _by_cam(client)[DARK_CAM]["date"] == _ymd(1)

    import routes.overview as ov

    def older(archive_idx, host_key, cam_code):
        return {"host": host_key, "cam": cam_code, "date": _ymd(5),
                "filename": f"{cam_code}_{_ymd(5)}_captured_stack.jpg",
                "label": "Captured stack"}

    monkeypatch.setattr(ov, "resolve_archive_cam_stack", older)
    app._refresh_overview_stacks()
    assert _by_cam(client)[DARK_CAM]["date"] == _ymd(1), (
        "card regressed to an older archived night"
    )


def _seed_index_dates(cam: str, day: str) -> None:
    """Teach the archive index this camera-night exists (prod learns it from
    the persisted catalogue / refresh walk); the file listing itself is then
    resolved by night_files()'s on-demand scan of the fake archive."""
    import rovimen_dashboard as rd
    idx = rd._archive_idx
    with idx._lock:
        idx._dates.setdefault(cam, set()).add(day)


def test_a_colour_only_camera_falls_back_to_its_night_stack(app, client, station):
    """A camera with no RMS output anywhere (rms: false, e.g. the GH662
    spectro pair) must render its dawn night composite, not "no data"."""
    _seed_index_dates(NS_CAM, _ymd(1))
    app._refresh_overview_stacks()
    entries = _by_cam(client)

    assert NS_CAM in entries, "colour-only camera rendered as no data"
    assert entries[NS_CAM]["date"] == _ymd(1)
    assert entries[NS_CAM]["filename"] == f"{NS_CAM}_{_ymd(1)}_night_stack.webp"
    assert entries[NS_CAM]["label"] == "Night stack"


def test_the_night_stack_tile_is_served_as_bw_webp(app, client, station):
    """The plot-image route renders the colour composite as a GMN-style
    black-and-white stack (single-channel source, webp out)."""
    from io import BytesIO

    from PIL import Image

    day = _ymd(1)
    r = client.get(
        f"/api/rms/plot_image/teststn1/{NS_CAM}/{day}/{NS_CAM}_{day}_night_stack.webp"
    )
    assert r.status_code == 200
    assert r.headers["Content-Type"] == "image/webp"
    # WebP decodes to RGB whatever the source mode was, so grayscale shows as
    # equal channels: the red source (200, 30, 30) must come back r == g == b.
    img = Image.open(BytesIO(r.data)).convert("RGB")
    colours = img.getcolors(maxcolors=256) or []
    assert colours, "render produced no pixels"
    for _count, (red, green, blue) in colours:
        assert abs(red - green) <= 2 and abs(green - blue) <= 2, (
            f"expected grayscale render, got pixel ({red}, {green}, {blue})"
        )
