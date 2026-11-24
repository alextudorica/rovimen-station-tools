"""Hiding a detection as a false positive (bird, satellite, plane, insect).

A hide is a property of one detection, ``(cam, ff_file, meteor_no)``, shared
by every user, stored in its own SQLite file (``hidden_detections``) so it
survives a rebuild of the detections table. Any signed-in account that may
see the camera can hide and unhide, except a magic-link session; browsing
lists leave hidden detections out unless a signed-in caller asks
``show_hidden=1``; the public API never returns them.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

import detection_db
import hidden_detections as hd
import highlight_events

CAM = "RO0017"
CAM2 = "RO000M"
DATE = "20261105"
FF1 = "FF_RO0017_20261105_212000_123_0012345.fits"
FF2 = "FF_RO0017_20261105_223000_456_0023456.fits"
FF3 = "FF_RO000M_20261105_212000_500_0012399.fits"
T1 = "2026-11-05T21:20:05.400"
T2 = "2026-11-05T22:30:10.100"
T3 = "2026-11-05T21:20:05.900"
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}


def _row(cam: str, ff: str, n: int, t: str, mag: float, date: str = DATE) -> dict:
    row = {c: None for c in detection_db.DETECTION_COLS}
    row.update(cam=cam, date=date, ff_file=ff, meteor_no=n, time_utc=t,
               mag_apparent=mag, shower="SPO",
               chunk_file=f"{cam}_{date}_{t[11:13]}{t[14:16]}00_color.mkv")
    return row


ROWS = [
    _row(CAM, FF1, 1, T1, -3.5),     # the bird: brightest
    _row(CAM, FF2, 1, T2, 1.2),      # a real meteor
    _row(CAM2, FF3, 1, T3, -1.0),    # the other station's view of T1
]


def _make_detections_db(path: Path) -> None:
    detection_db.open_db(path).close()
    detection_db.upsert_detections([dict(r) for r in ROWS], source="station", path=path)


# ── The store ────────────────────────────────────────────────────────────────


def test_hide_unhide_history_and_idempotence(tmp_path):
    db = tmp_path / "hidden.db"
    det = ROWS[0]
    state, changed = hd.hide(det, "operator", reason="bird", page="detections", path=db)
    assert changed and state["hidden"] and state["hidden_by"] == "operator"
    assert state["hidden_reason"] == "bird"
    # A second hide (double click, another user) changes nothing.
    state2, changed2 = hd.hide(det, "maintainer2", reason="plane", path=db)
    assert not changed2 and state2["hidden_by"] == "operator" and state2["hidden_reason"] == "bird"
    snap = hd.snapshot(db)
    assert len(snap) == 1
    assert snap.lookup(CAM, FF1, 1)["hidden_by"] == "operator"
    assert snap.lookup(CAM.lower(), time_utc=T1.replace("T", " "))["hidden_reason"] == "bird"
    # A key naming another meteor of the same FF never matches by time.
    assert snap.lookup(CAM, FF1, 2, time_utc=T1) is None

    state3, changed3 = hd.unhide(CAM, FF1, 1, "maintainer2", page="video", path=db)
    assert changed3 and state3["hidden"] is False
    assert not hd.unhide(CAM, FF1, 1, "maintainer2", path=db)[1]
    assert len(hd.snapshot(db)) == 0
    hist = hd.history(CAM, FF1, 1, path=db)
    assert [(h["action"], h["user"], h["reason"], h["page"]) for h in hist] == [
        ("hide", "operator", "bird", "detections"), ("unhide", "maintainer2", None, "video")]


def test_snapshot_without_store_is_empty(tmp_path):
    snap = hd.snapshot(tmp_path / "absent.db")
    assert not snap and snap.match({"cam": CAM, "meteor_time": T1}) is None


# A separate Python process, as a gunicorn worker is (fork() from a test
# process that runs threads can deadlock the child).
_CHILD = """
import sys
from pathlib import Path
sys.path.insert(0, {dash!r})
import hidden_detections as hd
db = Path({db!r})
for i in range({n}):
    hd.hide({{"cam": "RO0017", "ff_file": "FF_RO0017_20261105_2{w}0000_%03d.fits" % i,
             "meteor_no": 1, "date": "20261105", "time_utc": None}}, "user{w}", path=db)
    hd.hide({{"cam": "RO0017", "ff_file": {ff!r}, "meteor_no": 1, "date": "20261105",
             "time_utc": {t!r}}}, "user{w}", path=db)
"""


def _spawn_hiders(db: Path, workers: int, n: int) -> list[subprocess.Popen]:
    dash = str(Path(__file__).resolve().parent.parent / "dashboard")
    return [subprocess.Popen([sys.executable, "-c", _CHILD.format(
        dash=dash, db=str(db), n=n, w=w, ff=FF1, t=T1)]) for w in range(workers)]


def test_concurrent_writers_from_separate_processes(tmp_path):
    db = tmp_path / "hidden.db"
    procs = _spawn_hiders(db, workers=4, n=20)
    assert [p.wait(60) for p in procs] == [0, 0, 0, 0]
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM hidden").fetchone()[0] == 81
        # One hide row in the history for the detection all four hid.
        assert con.execute(
            "SELECT COUNT(*) FROM history WHERE ff_file=? AND action='hide'", (FF1,)
        ).fetchone()[0] == 1


def test_snapshot_sees_a_write_by_another_process_at_once(tmp_path):
    """The three gunicorn workers share the file: a worker's snapshot must
    pick up another worker's commit on its next read, even inside one mtime
    tick (the SQLite file change counter is part of the signature)."""
    db = tmp_path / "hidden.db"
    hd.hide(ROWS[1], "operator", path=db)
    assert len(hd.snapshot(db)) == 1
    (proc,) = _spawn_hiders(db, workers=1, n=1)
    assert proc.wait(60) == 0
    snap = hd.snapshot(db)
    assert len(snap) == 3 and snap.lookup(CAM, FF1, 1)["hidden_by"] == "user0"


def test_hide_survives_a_rebuild_of_the_detections_table(tmp_path):
    det_db = tmp_path / "detections.db"
    _make_detections_db(det_db)
    found = detection_db.find_detection(CAM, time_utc=T1, path=det_db)
    hd.hide(found, "operator", reason="bird")
    # The detections table is rebuilt from station data into a fresh file
    # (a new path: sqlite_writer keeps this process's write connection to
    # the old one open).
    rebuilt = tmp_path / "rebuilt" / "detections.db"
    rebuilt.parent.mkdir()
    _make_detections_db(rebuilt)
    again = detection_db.find_detection(CAM, FF1, 1, path=rebuilt)
    assert hd.snapshot().match(again)["hidden_by"] == "operator"
    assert hd.snapshot().match(detection_db.find_detection(CAM, FF2, 1, path=rebuilt)) is None


def test_find_detection_by_key_and_by_time(tmp_path):
    det_db = tmp_path / "detections.db"
    _make_detections_db(det_db)
    assert detection_db.find_detection(CAM, FF1, 1, path=det_db)["time_utc"] == T1
    assert detection_db.find_detection(CAM, FF1, 2, path=det_db) is None
    assert detection_db.find_detection(CAM, time_utc="2026-11-05 21:20:05", path=det_db)["ff_file"] == FF1
    assert detection_db.find_detection(CAM, time_utc="2026-11-05T21:20:06", path=det_db) is None
    assert detection_db.find_detection("RO9999", FF1, 1, path=det_db) is None


def test_aggregate_stats_picks_skip_hidden_but_counts_keep_them(tmp_path):
    det_db = tmp_path / "detections.db"
    _make_detections_db(det_db)
    hd.hide(ROWS[0], "operator")
    stats = detection_db.aggregate_stats([DATE], path=det_db, skip=hd.snapshot().match)
    assert stats["total"] == 3
    assert stats["brightest"]["ff_file"] == FF3
    assert detection_db.aggregate_stats([DATE], path=det_db)["brightest"]["ff_file"] == FF1


# ── The event rule and the payload filter ────────────────────────────────────


def _payload() -> dict:
    def det(hk, cam, t, fname, mag):
        return {"host_key": hk, "station_label": hk, "cam": cam, "filename": fname,
                "meteor_time": t, "detection_offset_s": 3.0, "stack": None,
                "time": t[11:19], "lock_type": "detection",
                "rms": {"mag_apparent": mag, "shower": "SPO"}}
    a = det("teststation", CAM, T1, f"{CAM}_{DATE}_212000_color.mkv", -3.5)
    b = det("teststation", CAM, T2, f"{CAM}_{DATE}_223000_color.mkv", 1.2)
    c = det("otherstation", CAM2, T3, f"{CAM2}_{DATE}_212000_color.mkv", -1.0)
    return {
        "date": DATE, "correlation_window_s": 1,
        "by_station": {
            "teststation": {"label": "Test", "online": True, "cameras": {CAM: [a, b]}},
            "otherstation": {"label": "Other", "online": True, "cameras": {CAM2: [c]}},
        },
        "events": [{"event_time": T1, "witness_count": 2, "station_count": 2,
                    "witnesses": [a, c], "geometry": "time_only"}],
        "detections": [a, c, b],
    }


def test_event_with_some_witnesses_hidden_stays_without_them():
    hd.hide(ROWS[0], "operator", reason="bird")
    snap = hd.snapshot()
    original = _payload()
    out = hd.apply_payload(original, snap, False)
    assert [d["cam"] for d in out["detections"]] == [CAM2, CAM]
    assert out["hidden_count"] == 1
    assert [c["meteor_time"] for c in out["by_station"]["teststation"]["cameras"][CAM]] == [T2]
    (ev,) = out["events"]
    assert [w["cam"] for w in ev["witnesses"]] == [CAM2]
    assert ev["witness_count"] == 1 and ev["hidden_witnesses"] == 1
    # The cached payload itself is never touched.
    assert len(original["events"][0]["witnesses"]) == 2
    assert "hidden" not in original["detections"][0]


def test_event_with_every_witness_hidden_is_hidden():
    hd.hide(ROWS[0], "operator")
    hd.hide(ROWS[2], "operator")
    snap = hd.snapshot()
    assert hd.apply_payload(_payload(), snap, False)["events"] == []
    (ev,) = hd.apply_payload(_payload(), snap, True)["events"]
    assert ev["hidden"] is True and all(w["hidden"] for w in ev["witnesses"])


def test_show_hidden_marks_instead_of_dropping():
    hd.hide(ROWS[0], "operator", reason="satellite")
    out = hd.apply_payload(_payload(), hd.snapshot(), True)
    first = out["by_station"]["teststation"]["cameras"][CAM][0]
    assert first["hidden"] is True and first["hidden_by"] == "operator"
    assert first["hidden_reason"] == "satellite" and first["hidden_at"]
    (ev,) = out["events"]
    assert ev["hidden"] is False and len(ev["witnesses"]) == 2


def test_highlights_events_leave_hidden_witnesses_out():
    hd.hide(ROWS[0], "operator")
    marked = hd.apply_payload(_payload(), hd.snapshot(), True)
    date_iso = "2026-11-05"
    shown = highlight_events.night_events(marked["detections"], [], marked["events"], date_iso)
    (ev,) = shown
    assert [w["camera"] for w in ev["witnesses"]] == [CAM2]
    assert ev["peak_magnitude"] == -1.0
    with_hidden = highlight_events.night_events(
        marked["detections"], [], marked["events"], date_iso, show_hidden=True)
    assert {w["camera"] for w in with_hidden[0]["witnesses"]} == {CAM, CAM2}
    assert with_hidden[0]["peak_magnitude"] == -3.5
    hd.hide(ROWS[2], "operator")
    marked = hd.apply_payload(_payload(), hd.snapshot(), True)
    assert highlight_events.night_events(marked["detections"], [], marked["events"], date_iso) == []


def test_nightly_telegram_brightest_skips_hidden(tmp_path, monkeypatch):
    import nightly_telegram

    det_db = tmp_path / "detections.db"
    _make_detections_db(det_db)
    monkeypatch.setattr(detection_db, "DB_PATH", det_db)
    before = nightly_telegram.collect_night(DATE, {CAM, CAM2}, fireball_mag=-3.0)
    assert before["brightest"]["ff_file"] == FF1 and len(before["fireballs"]) == 1
    hd.hide(ROWS[0], "operator")
    after = nightly_telegram.collect_night(DATE, {CAM, CAM2}, fireball_mag=-3.0)
    assert after["brightest"]["ff_file"] == FF3 and after["fireballs"] == []
    assert after["total"] == 3  # a hidden detection is still a detection


# ── The routes ───────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def app_env():
    tmp = Path(tempfile.mkdtemp(prefix="rovimen_hide_"))
    users = {
        "admin": {"display_name": "Admin", "password_hash": "", "role": "admin",
                  "require_totp": False},
    }
    for role in ("host", "visitor", "press"):
        users[role + "1"] = {"display_name": role, "password_hash": "", "role": role,
                             "require_totp": False, "stations": ["teststation"]}
    (tmp / "users.yaml").write_text(yaml.safe_dump(users))
    config_path = tmp / "dashboard_config.yaml"

    def station(cam: str, lat: float) -> dict:
        return {"ip": "127.0.0.1", "label": "Station " + cam, "ssh_user": "test",
                "public": True, "lat": lat, "lon": 26.1,
                "cameras": [{"code": cam, "cam_ip": "127.0.0.1", "label": "N",
                             "rotate": False}]}
    config_path.write_text(yaml.safe_dump({
        "correlation_window_s": 1, "station_api_port": 7779,
        "stations": {"teststation": station(CAM, 44.4), "otherstation": station(CAM2, 45.0)},
    }))
    keys = {
        "ROVIMEN_USERS_PATH": tmp / "users.yaml",
        "ROVIMEN_SECRET_KEY": "hide-test-secret",
        "ROVIMEN_COOKIE_SECURE": "0",
        "RATELIMIT_ENABLED": "0",
        "THUMB_CACHE_DIR": tmp / "thumb_cache",
        "ROVIMEN_CACHE_PATH": tmp / "cache",
        "ROVIMEN_COMPILATIONS_OUT_PATH": tmp / "compilations",
        "ROVIMEN_KNOWN_HOSTS": tmp / "known_hosts",
        "ROVIMEN_AUDIT_LOG_PATH": tmp / "audit.log",
        "ROVIMEN_ACTIVITY_LOG_PATH": tmp / "activity.log",
        "ROVIMEN_STATION_STATE_DB": tmp / "station_state.db",
        "ROVIMEN_DETECTIONS_DB": tmp / "detections.db",
        "ROVIMEN_SHOWER_COUNTS_PATH": tmp / "shower_counts.json",
        "ROVIMEN_GMN_CACHE_DIR": tmp / "gmn_cache",
        "ROVIMEN_ARCHIVE_PATH": tmp / "archive",
    }
    saved_env = {k: os.environ.get(k) for k in keys}
    for k, v in keys.items():
        os.environ[k] = str(v)

    import security

    saved = (security._LOGIN_THRESHOLD, security._LOGIN_LOCKOUT_SECONDS, security.init_limiter)
    security._LOGIN_THRESHOLD = 10_000
    security._LOGIN_LOCKOUT_SECONDS = 0

    def _noop_limiter(app):
        lim = saved[2](app)
        lim.enabled = False
        return lim

    security.init_limiter = _noop_limiter
    # detection_db binds its path at import; keep create_app off /opt/rovimen.
    saved_db_path = detection_db.DB_PATH
    detection_db.DB_PATH = tmp / "detections.db"
    from rovimen_dashboard import create_app, load_config

    app = create_app(load_config(config_path), config_path)
    app.config["TESTING"] = True
    try:
        yield app, tmp
    finally:
        detection_db.DB_PATH = saved_db_path
        (security._LOGIN_THRESHOLD, security._LOGIN_LOCKOUT_SECONDS,
         security.init_limiter) = saved
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@pytest.fixture
def env(app_env, tmp_path, monkeypatch):
    """Detections in a tmp DB, the night's payload seeded in the per-night
    cache, and audit records captured."""
    import routes.detections as det_routes
    import security
    from http_caching import _json_cache_clear

    app, _ = app_env
    det_db = tmp_path / "detections.db"
    _make_detections_db(det_db)
    monkeypatch.setattr(detection_db, "DB_PATH", det_db)
    cache = det_routes._deps().detections_cache
    expiry = time.monotonic() + 3600
    for allow_live in (True, False):
        cache[(DATE, allow_live)] = (expiry, _payload())
    audits: list[tuple[str, dict]] = []
    monkeypatch.setattr(security, "audit", lambda event, **f: audits.append((event, f)))
    _json_cache_clear()
    yield app, audits
    for allow_live in (True, False):
        cache.pop((DATE, allow_live), None)
    _json_cache_clear()


def _client(app, user: str | None, role: str | None = None, magic: bool = False):
    c = app.test_client()
    if user:
        with c.session_transaction() as sess:
            sess["user"] = user
            sess["role"] = role
            sess["stations"] = ["teststation"]
            sess["mfa"] = "totp"
            if magic:
                sess["magic"] = True
                sess["magic_exp"] = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    return c


def _hide(client, **body):
    body = {"cam": CAM, "ff_file": FF1, "meteor_no": 1, **body}
    return client.post("/api/detections/hide", json=body, headers=SAME_ORIGIN)


@pytest.mark.parametrize("user,role", [
    ("admin", "admin"), ("host1", "host"), ("visitor1", "visitor"), ("press1", "press")])
def test_every_role_may_hide_and_unhide(env, user, role):
    app, audits = env
    c = _client(app, user, role)
    r = _hide(c, reason="bird", page="detections")
    assert r.status_code == 200, r.data
    body = r.get_json()
    assert body["hidden"] is True and body["hidden_by"] == user and body["changed"] is True
    assert body["ff_file"] == FF1 and body["meteor_no"] == 1
    r = c.post("/api/detections/unhide", json={"cam": CAM, "time_utc": T1},
               headers=SAME_ORIGIN)
    assert r.status_code == 200 and r.get_json()["hidden"] is False
    assert [(e, f["user"], f["cam"], f["ff_file"]) for e, f in audits] == [
        ("detection_hide", user, CAM, FF1), ("detection_unhide", user, CAM, FF1)]
    assert audits[0][1]["reason"] == "bird"


def test_anonymous_cannot_hide(env):
    app, audits = env
    assert _hide(_client(app, None)).status_code == 401
    assert len(hd.snapshot()) == 0 and audits == []


def test_magic_link_session_cannot_hide_or_unhide(env):
    app, audits = env
    hd.hide(ROWS[0], "operator")
    c = _client(app, "host1", "visitor", magic=True)
    assert _hide(c, ff_file=FF2).status_code == 403
    assert c.post("/api/detections/unhide", json={"cam": CAM, "ff_file": FF1, "meteor_no": 1},
                  headers=SAME_ORIGIN).status_code == 403
    assert len(hd.snapshot()) == 1 and audits == []
    status = c.get("/api/auth/status").get_json()
    assert status["can_hide"] is False
    assert _client(app, "host1", "host").get("/api/auth/status").get_json()["can_hide"] is True


def test_unknown_role_and_unseen_camera_are_refused(env, monkeypatch):
    app, _ = env
    assert _hide(_client(app, "ghost", "guest")).status_code == 403
    import routes.hidden as hidden_routes
    monkeypatch.setattr(hidden_routes, "_has_camera_access", lambda config, cam: False)
    assert _hide(_client(app, "host1", "host")).status_code == 403
    assert len(hd.snapshot()) == 0


def test_api_key_is_not_a_session(env, monkeypatch):
    app, _ = env
    monkeypatch.setenv("ROVIMEN_API_KEYS_REQUIRED", "1")
    r = app.test_client().post("/api/detections/hide",
                               json={"cam": CAM, "ff_file": FF1, "meteor_no": 1},
                               headers={**SAME_ORIGIN, "X-API-Key": "rk_live_whatever"})
    assert r.status_code == 401


@pytest.mark.parametrize("body,status", [
    ({"cam": CAM, "ff_file": FF1, "meteor_no": 9}, 404),           # no such meteor
    ({"cam": "RO9999", "ff_file": FF1, "meteor_no": 1}, 404),      # no station owns it
    ({"cam": CAM, "time_utc": "2026-11-05T23:59:59"}, 404),
    ({"cam": CAM, "ff_file": FF1, "meteor_no": 1, "reason": "ufo"}, 400),
    ({"cam": CAM, "ff_file": "../x", "meteor_no": 1}, 400),
    ({"cam": CAM}, 400),
])
def test_bad_requests(env, body, status):
    app, _ = env
    r = _client(app, "admin", "admin").post("/api/detections/hide", json=body,
                                            headers=SAME_ORIGIN)
    assert r.status_code == status


def test_hide_by_time_resolves_the_detection_row(env):
    app, _ = env
    r = _hide(_client(app, "host1", "host"), ff_file=None, meteor_no=None, time_utc=T1[:19])
    assert r.status_code == 200 and r.get_json()["ff_file"] == FF1
    assert hd.snapshot().lookup(CAM, FF1, 1) is not None


def test_hide_is_rate_limited_per_account(env):
    import routes.hidden as hidden_routes

    app, _ = env
    limiter = app._limiter
    limiter.enabled = True
    limiter.reset()
    try:
        c = _client(app, "host1", "host")
        per_minute = int(hidden_routes.HIDE_RATE_LIMITS[0].split()[0])
        codes = [c.post("/api/detections/unhide",
                        json={"cam": CAM, "ff_file": FF1, "meteor_no": 1},
                        headers=SAME_ORIGIN).status_code for _ in range(per_minute + 1)]
        assert codes[:per_minute] == [200] * per_minute and codes[-1] == 429
        # Another account has its own budget.
        assert _hide(_client(app, "press1", "press")).status_code == 200
    finally:
        limiter.reset()
        limiter.enabled = False


def test_history_route(env):
    app, _ = env
    c = _client(app, "admin", "admin")
    _hide(c, reason="insect")
    r = c.get(f"/api/detections/hidden/history?cam={CAM}&ff_file={FF1}&meteor_no=1")
    assert [h["action"] for h in r.get_json()["history"]] == ["hide"]
    assert app.test_client().get(
        f"/api/detections/hidden/history?cam={CAM}&ff_file={FF1}&meteor_no=1").status_code == 401


# ── Listings with and without show_hidden ─────────────────────────────────────


def test_night_listing_leaves_hidden_out_and_shows_them_on_request(env):
    app, _ = env
    hd.hide(ROWS[0], "operator", reason="bird")
    c = _client(app, "visitor1", "visitor")
    body = c.get(f"/api/detections/{DATE}").get_json()
    assert T1 not in [d["meteor_time"] for d in body["detections"]]
    assert body["hidden_count"] == 1
    assert [w["cam"] for w in body["events"][0]["witnesses"]] == [CAM2]
    body = c.get(f"/api/detections/{DATE}?show_hidden=1").get_json()
    marked = [d for d in body["detections"] if d.get("hidden")]
    assert [(d["meteor_time"], d["hidden_by"], d["hidden_reason"]) for d in marked] == [
        (T1, "operator", "bird")]
    # A hide by anyone shows on the next request (no stale serialized body).
    hd.unhide(CAM, FF1, 1, "operator")
    body = c.get(f"/api/detections/{DATE}").get_json()
    assert T1 in [d["meteor_time"] for d in body["detections"]]
    assert body["hidden_count"] == 0


def test_anonymous_never_gets_hidden_detections(env):
    app, _ = env
    hd.hide(ROWS[0], "operator")
    body = app.test_client().get(f"/api/detections/{DATE}?show_hidden=1").get_json()
    assert all(not d.get("hidden") for d in body["detections"])
    assert T1 not in [d["meteor_time"] for d in body["detections"]]


def test_signed_in_listing_is_revalidated_by_the_browser(env):
    app, _ = env
    r = _client(app, "visitor1", "visitor").get(f"/api/detections/{DATE}")
    assert r.headers["Cache-Control"] == "private, max-age=0"
    r = app.test_client().get(f"/api/detections/{DATE}")
    assert r.headers["Cache-Control"] == "private, max-age=3600"


def test_grid_and_range(env):
    app, _ = env
    hd.hide(ROWS[0], "operator")
    c = _client(app, "host1", "host")

    def grid_times(qs=""):
        rows = c.get(f"/api/detections/grid/{DATE}?bucket=60{qs}").get_json()["rows"]
        return sorted(d["meteor_time"] for row in rows for cell in row["cells"].values()
                      for d in cell)
    assert T1 not in grid_times()
    assert T1 in grid_times("&show_hidden=1")
    r = c.get(f"/api/detections/range?from={DATE}&to={DATE}").get_json()
    assert r["count"] == 2 and r["hidden_count"] == 1
    r = c.get(f"/api/detections/range?from={DATE}&to={DATE}&show_hidden=1").get_json()
    assert r["count"] == 3 and [d["meteor_time"] for d in r["detections"] if d.get("hidden")] == [T1]


def test_highlights_route(env, monkeypatch):
    import gmn_data

    app, _ = env
    # No GMN trajectories: the events are the ROVIMEN coincidences.
    monkeypatch.setattr(gmn_data, "events_for_date", lambda date_iso: {"events": []})
    hd.hide(ROWS[0], "operator")
    url = "/api/highlights/data?start=2026-11-05&end=2026-11-05"

    def cams(client, qs=""):
        evs = client.get(url + qs).get_json()["events"]
        return [sorted(w["camera"] for w in ev["witnesses"]) for ev in evs]
    c = _client(app, "visitor1", "visitor")
    assert cams(c) == [[CAM2]]
    assert cams(c, "&show_hidden=1") == [sorted([CAM, CAM2])]
    assert cams(app.test_client(), "&show_hidden=1") == [[CAM2]]
    # The counts above the events are statistics and keep hidden detections.
    assert c.get(url).get_json()["total_detections"] == 3


def test_detection_tab_listings(env, monkeypatch):
    """The station Detection tab: the camera-night's RMS detections and its
    locked clips (``locked_only=1``). The footage list (Video DB) is never
    filtered: it keeps every chunk and marks a hidden detection chunk, so its
    card shows Unhide."""
    import chunk_listings

    app, _ = env
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    t_bird = f"{today[:4]}-{today[4:6]}-{today[6:]}T01:02:03.500"
    bird = _row(CAM, f"FF_{CAM}_{today}_010200_000_0000001.fits", 1, t_bird, -4.0, date=today)
    real = _row(CAM, f"FF_{CAM}_{today}_020000_000_0000002.fits", 1,
                t_bird.replace("01:02", "02:00"), 2.0, date=today)
    monkeypatch.setattr(detection_db, "query_detections", lambda *a, **k: [bird, real])
    chunks = [
        {"filename": f"{CAM}_{today}_010000_color.mkv", "locked": True,
         "lock_type": "detection", "meteor_time": t_bird[:-4]},
        {"filename": f"{CAM}_{today}_020000_color.mkv", "locked": True,
         "lock_type": "detection", "meteor_time": real["time_utc"]},
        {"filename": f"{CAM}_{today}_030000_color.mkv", "locked": False},
    ]
    monkeypatch.setattr(chunk_listings, "get", lambda cam, date: {"chunks": chunks})
    hd.hide(bird, "operator", reason="bird")
    c = _client(app, "press1", "press")

    dets = c.get(f"/api/videodb/rms-detections/teststation/{CAM}/{today}").get_json()
    assert [d["ff_file"] for d in dets["detections"]] == [real["ff_file"]]
    assert dets["hidden_count"] == 1
    dets = c.get(f"/api/videodb/rms-detections/teststation/{CAM}/{today}?show_hidden=1").get_json()
    assert [d.get("hidden", False) for d in dets["detections"]] == [True, False]

    locked = c.get(f"/api/videodb/chunks/teststation/{CAM}/{today}?locked_only=1").get_json()
    assert [x["filename"] for x in locked] == [f"{CAM}_{today}_020000_color.mkv"]
    locked = c.get(
        f"/api/videodb/chunks/teststation/{CAM}/{today}?locked_only=1&show_hidden=1").get_json()
    assert [x.get("hidden", False) for x in locked] == [True, False]
    footage = c.get(f"/api/videodb/chunks/teststation/{CAM}/{today}").get_json()
    assert [x["filename"] for x in footage] == [ch["filename"] for ch in chunks]
    assert [x.get("hidden", False) for x in footage] == [True, False, False]
    assert footage[0]["hidden_by"] == "operator"
    # An unhide clears the mark on the next request.
    hd.unhide(CAM, bird["ff_file"], 1, "operator")
    footage = c.get(f"/api/videodb/chunks/teststation/{CAM}/{today}?locked_only=0").get_json()
    assert not any(x.get("hidden") for x in footage)
    # The chunk listing the station pushed is never mutated by the marking.
    assert "hidden" not in chunks[0]


def _archive_nights() -> list[dict]:
    """A camera's archive nights as ``_build_nights_full`` returns them."""
    def clip(hhmmss: str, meteor_time: str | None) -> dict:
        return {"filename": f"{CAM}_{DATE}_{hhmmss}_color.mkv", "stack": None,
                "stack_subdir": None, "detection_offset_s": 5.0, "meteor_time": meteor_time}
    return [
        {"date": DATE, "timelapse": None, "timelapse_stack": None, "timelapse_size_mb": None,
         "meteors": [clip("212000", T1[:19]), clip("223000", T2), clip("230000", None)]},
        {"date": "20261104", "timelapse": None, "timelapse_stack": None,
         "timelapse_size_mb": None, "meteors": [clip("200000", "2026-11-04T20:00:01")]},
    ]


def test_archive_tab_leaves_hidden_clips_out_and_counts_them(env):
    """The Archive tab's nights_full: a hidden meteor clip leaves the list
    (matched by camera and meteor time, after the cache), a signed-in caller
    gets it back marked with show_hidden=1, the night carries hidden_count
    for the toolbar's "Show hidden (N)", and the cached nights are never
    mutated."""
    import routes.archive as archive_routes

    app, _ = env
    cache = archive_routes._deps().nights_full_cache
    nights = _archive_nights()
    cache[CAM] = (time.monotonic(), nights)
    url = f"/api/archive/nights_full/{CAM}"
    try:
        c = _client(app, "press1", "press")
        names = lambda body: [[m["filename"][-17:-10] for m in n["meteors"]] for n in body]  # noqa: E731
        # Nothing hidden: the cached list as it is, no counts.
        body = c.get(url).get_json()
        assert names(body) == [["_212000", "_223000", "_230000"], ["_200000"]]
        assert all("hidden_count" not in n for n in body)

        hd.hide(ROWS[0], "operator", reason="bird")    # the bird at T1
        r = c.get(url)
        body = r.get_json()
        assert names(body) == [["_223000", "_230000"], ["_200000"]]
        assert body[0]["hidden_count"] == 1 and "hidden_count" not in body[1]
        # A signed-in browser revalidates, so another user's hide shows at once.
        assert r.headers["Cache-Control"] == "private, max-age=0"

        body = c.get(url + "?show_hidden=1").get_json()
        assert names(body)[0] == ["_212000", "_223000", "_230000"]
        assert [m.get("hidden", False) for m in body[0]["meteors"]] == [True, False, False]
        assert body[0]["meteors"][0]["hidden_by"] == "operator"
        assert body[0]["hidden_count"] == 1

        # The Archive tab is signed-in only.
        assert app.test_client().get(url + "?show_hidden=1").status_code == 401

        # The cache still holds every clip, unmarked.
        assert [m.get("hidden") for m in cache[CAM][1][0]["meteors"]] == [None, None, None]
        assert "hidden_count" not in cache[CAM][1][0]
    finally:
        cache.pop(CAM, None)


def test_public_api_never_returns_a_hidden_detection(env, monkeypatch):
    app, _ = env
    monkeypatch.setenv("ROVIMEN_API_KEYS_REQUIRED", "0")
    hd.hide(ROWS[0], "operator")
    c = app.test_client()
    for qs in ("", "&show_hidden=1"):
        body = c.get(f"/api/public/v1/detections?date=2026-11-05&order=mag{qs}").get_json()
        assert sorted(d["time_utc"] for d in body["detections"]) == [T3, T2]
    # A signed-in session gets nothing more from the public surface either.
    body = _client(app, "admin", "admin").get(
        "/api/public/v1/detections?date=2026-11-05&show_hidden=1").get_json()
    assert body["total"] == 2
    bird_id = f"2026-11-05_teststation_{CAM}_{CAM}_{DATE}_212000_color.mkv"
    assert c.get(f"/api/public/v1/detections/{bird_id}").status_code == 404


def test_hall_of_fame_and_locks_are_untouched(env, tmp_path, monkeypatch):
    """Hiding writes only its own store: no clip, lock, kept clip or Hall of
    Fame entry is touched."""
    import hall_of_fame_store
    import kept_clips

    app, _ = env
    kept = tmp_path / "kept.jsonl"
    monkeypatch.setattr(kept_clips, "KEPT_CLIPS_PATH", kept)
    kept.write_text('{"cam":"RO0017","date":"20261105","chunk":"x_color.mkv","user":"a"}\n')
    before = kept.read_bytes()
    hof_before = hall_of_fame_store.load_all()
    assert _hide(_client(app, "admin", "admin")).status_code == 200
    assert kept.read_bytes() == before
    assert hall_of_fame_store.load_all() == hof_before
