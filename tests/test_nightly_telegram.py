"""Tests for the nightly Telegram summary.

Each test here pins a behaviour that was wrong in production against real
numbers measured on the night of 2026-08-16:

  - 212 detections brighter than 0.0 mag were announced as "fireballs"; 8 were
    actually brighter than -4, and all 8 came from the one station the summary
    filtered out for being in commissioning;
  - eight cameras that had uploaded 65-136 detection clips each were listed as
    "clear / no meteors" because their FTPdetectinfo never reached the index;
  - the brightest clip matched 6 times out of 200 because the matcher looked for
    the FF block timestamp in the colour clip's filename;
  - the post was English on a Romanian channel, and named no shower on a night
    that was 36% Perseids.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import cache_store
import detection_db
import nightly_telegram as nt
import station_state


CAMS = {"RO000H", "RO001F", "RO0003", "RO000J"}


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def index(tmp_path, monkeypatch):
    """A detections.db built by the real migrations, so the schema matches prod."""
    db = tmp_path / "detections.db"
    detection_db.open_db(db).close()
    # media_pointers lives in station_state's half of the same DB.
    station_state.ensure_schema(db)
    monkeypatch.setattr(detection_db, "DB_PATH", db)
    return db


def _write_config(tmp_path, stations: dict) -> Path:
    """Write a dashboard_config.yaml that validates against DashboardConfig."""
    import yaml
    for st in stations.values():
        st.setdefault("ip", "100.0.0.1")
        st.setdefault("label", st.get("location_name", "Station"))
        for cam in st.get("cameras", []):
            cam.setdefault("cam_ip", "192.168.1.10")
    path = tmp_path / "dashboard_config.yaml"
    path.write_text(yaml.safe_dump({"stations": stations}))
    return path


def _add_detection(db, cam, date, secs, mag, shower="SPO"):
    con = sqlite3.connect(db)
    con.execute(
        "INSERT OR REPLACE INTO detections "
        "(cam, date, ff_file, meteor_no, time_utc, shower, mag_apparent) "
        "VALUES (?,?,?,?,?,?,?)",
        (cam, date, f"FF_{cam}_{date}_{secs:06d}_000_0000000.fits", 1,
         f"2026-08-16T{secs // 10000:02d}:{secs // 100 % 100:02d}:{secs % 100:02d}",
         shower, mag),
    )
    con.commit()
    con.close()


def _add_pointer(db, cam, date, kind, filename, extra=None):
    con = sqlite3.connect(db)
    con.execute(
        "INSERT OR REPLACE INTO media_pointers "
        "(host_key, cam, date, kind, filename, extra, updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        ("hk", cam, date, kind, filename,
         json.dumps(extra) if extra is not None else None, 0.0),
    )
    con.commit()
    con.close()


def _set_poll(db, host_key, last_polled, errors=0):
    con = sqlite3.connect(db)
    con.execute(
        "INSERT OR REPLACE INTO poll_state (host_key, last_polled, error_count) "
        "VALUES (?,?,?)", (host_key, last_polled, errors))
    con.commit()
    con.close()


# ── fireball threshold ───────────────────────────────────────────────────────


def test_fireball_threshold_is_minus_four_not_zero(index):
    """A -0.2 mag meteor is not a fireball."""
    for i, mag in enumerate([-0.2, -1.4, -3.9, -4.6, -6.35]):
        _add_detection(index, "RO000H", "20260816", 180000 + i, mag)

    night = nt.collect_night("20260816", {"RO000H"}, nt.DEFAULT_FIREBALL_MAG)

    assert len(night["fireballs"]) == 2
    assert night["per_cam"]["RO000H"]["fireballs"] == 2
    assert night["brightest"]["mag"] == pytest.approx(-6.35)


def test_fireball_threshold_is_configurable(index, monkeypatch):
    _add_detection(index, "RO000H", "20260816", 180000, -2.5)
    assert nt.collect_night("20260816", {"RO000H"}, -4.0)["fireballs"] == []
    assert len(nt.collect_night("20260816", {"RO000H"}, -2.0)["fireballs"]) == 1


def test_fireballs_are_ordered_brightest_first(index):
    for i, mag in enumerate([-4.5, -6.3, -5.1]):
        _add_detection(index, "RO000H", "20260816", 180000 + i, mag)
    night = nt.collect_night("20260816", {"RO000H"}, -4.0)
    assert [f["mag"] for f in night["fireballs"]] == [-6.3, -5.1, -4.5]


# ── commissioning stations are included ──────────────────────────────────────


def test_commissioning_station_cameras_are_included(tmp_path, monkeypatch):
    """RO001F recorded every real fireball of 2026-08-16 and was filtered out."""
    cfg = _write_config(tmp_path, {
        "gmnro03": {"status": "active", "location_name": "Ciocarlia",
                    "cameras": [{"code": "RO000H"}]},
        "gmnro18": {"status": "commissioning", "location_name": "Bisoca",
                    "cameras": [{"code": "RO001F"}]},
        "gmnro15": {"status": "decommissioned", "location_name": "Runcu",
                    "cameras": [{"code": "RO0099"}]},
    })
    station_cams, cam_location, commissioning = nt.load_stations(cfg)

    assert station_cams["gmnro18"] == ["RO001F"]
    assert commissioning == {"RO001F"}
    assert cam_location["RO001F"] == "Bisoca"
    # Decommissioned stays out -- that filter was correct.
    assert "gmnro15" not in station_cams


def test_commissioning_camera_is_never_reported_offline(index, monkeypatch):
    station_cams = {"gmnro18": ["RO001F"], "gmnro03": ["RO000H"]}
    # Neither station has polled recently and neither has detections.
    _set_poll(index, "gmnro18", 0.0)
    _set_poll(index, "gmnro03", 0.0)
    night = nt.collect_night("20260816", {"RO001F", "RO000H"}, -4.0)

    groups = nt.classify(night, station_cams, {"RO001F"}, stale_hours=30)

    assert "RO001F" in groups["quiet"]
    assert "RO000H" in groups["offline"]


def test_camera_prefix_filter_honours_env(tmp_path, monkeypatch):
    cfg = _write_config(tmp_path, {
        "gmnmd01": {"status": "active", "location_name": "Cahul",
                    "cameras": [{"code": "MD0001"}]},
        "gmnde01": {"status": "active", "location_name": "Berlin",
                    "cameras": [{"code": "DE0018"}]},
    })
    # Default RO,MD keeps Moldova, drops Germany from the Romanian channel.
    station_cams, _loc, _c = nt.load_stations(cfg)
    assert set(station_cams) == {"gmnmd01"}

    monkeypatch.setenv("ROVIMEN_NIGHTLY_CAM_PREFIXES", "RO")
    assert nt.load_stations(cfg)[0] == {}

    monkeypatch.setenv("ROVIMEN_NIGHTLY_CAM_PREFIXES", "RO, MD ,DE")
    assert set(nt.load_stations(cfg)[0]) == {"gmnmd01", "gmnde01"}


# ── "clear skies" vs a broken detection feed ─────────────────────────────────


def test_camera_with_clips_but_no_index_rows_is_not_called_clear(index):
    """Bistrița uploaded 136 clips and was reported as clear sky."""
    station_cams = {"gmnro05": ["RO0003"], "gmnro03": ["RO000J"]}
    _set_poll(index, "gmnro05", 1e12)   # fresh
    _set_poll(index, "gmnro03", 1e12)
    for i in range(136):
        _add_pointer(index, "RO0003", "20260816", "clip",
                     f"RO0003_20260816_{180000 + i:06d}_color.mkv",
                     {"locked": True})
    night = nt.collect_night("20260816", {"RO0003", "RO000J"}, -4.0)

    groups = nt.classify(night, station_cams, set(), stale_hours=30)

    assert groups["index_gap"] == [{"cam": "RO0003", "clips": 136}]
    # The camera with neither detections nor clips is genuinely quiet.
    assert groups["quiet"] == ["RO000J"]
    assert "RO0003" not in groups["quiet"]


def test_index_gap_never_reaches_the_public_post(index):
    station_cams = {"gmnro05": ["RO0003"]}
    _set_poll(index, "gmnro05", 1e12)
    _add_pointer(index, "RO0003", "20260816", "clip",
                 "RO0003_20260816_180000_color.mkv", {"locked": True})
    night = nt.collect_night("20260816", {"RO0003"}, -4.0)
    groups = nt.classify(night, station_cams, set(), stale_hours=30)

    public = nt.build_message(night, groups, {"RO0003": "Bistrita"}, -4.0)
    ops = nt.build_ops_appendix(groups, {"RO0003": "Bistrita"})

    assert "RO0003" not in public
    assert "RO0003" in ops
    assert "136" not in public


def test_ops_appendix_is_none_when_the_fleet_is_clean(index):
    station_cams = {"gmnro03": ["RO000H"]}
    _set_poll(index, "gmnro03", 1e12)
    _add_detection(index, "RO000H", "20260816", 180000, -1.0)
    night = nt.collect_night("20260816", {"RO000H"}, -4.0)
    groups = nt.classify(night, station_cams, set(), stale_hours=30)

    assert nt.build_ops_appendix(groups, {}) is None


# ── clip matching ────────────────────────────────────────────────────────────


def test_clip_matches_on_meteor_time_not_the_ff_filename(index, tmp_path, monkeypatch):
    """The FF stamp is the block start; the clip carries the real meteor time."""
    monkeypatch.setattr(cache_store, "ARCHIVE_PATH", tmp_path)
    meteors = tmp_path / "RO000H" / "20260816" / "meteors"
    meteors.mkdir(parents=True)
    (meteors / "RO000H_20260816_180905_color.mp4").write_bytes(b"clip")
    _add_pointer(index, "RO000H", "20260816", "clip",
                 "RO000H_20260816_180905_color.mkv",
                 {"meteor_time": "2026-08-16T18:09:21.295744", "locked": True})

    # Detection 16 s later than the clip's filename stamp -- an exact-substring
    # match on "20260816_180937" would find nothing.
    detection = {"time_utc": "2026-08-16T18:09:37", "cam": "RO000H"}
    found = nt.find_clip("RO000H", "20260816", detection, window_s=30)

    assert found == meteors / "RO000H_20260816_180905_color.mp4"


def test_clip_outside_the_window_is_not_matched(index, tmp_path, monkeypatch):
    monkeypatch.setattr(cache_store, "ARCHIVE_PATH", tmp_path)
    meteors = tmp_path / "RO000H" / "20260816" / "meteors"
    meteors.mkdir(parents=True)
    (meteors / "RO000H_20260816_180905_color.mp4").write_bytes(b"clip")
    _add_pointer(index, "RO000H", "20260816", "clip",
                 "RO000H_20260816_180905_color.mkv",
                 {"meteor_time": "2026-08-16T18:09:21"})

    detection = {"time_utc": "2026-08-16T19:30:00", "cam": "RO000H"}
    assert nt.find_clip("RO000H", "20260816", detection, window_s=30) is None


def test_clip_picks_the_nearest_of_several(index, tmp_path, monkeypatch):
    monkeypatch.setattr(cache_store, "ARCHIVE_PATH", tmp_path)
    meteors = tmp_path / "RO000H" / "20260816" / "meteors"
    meteors.mkdir(parents=True)
    for stem, mt in (
        ("RO000H_20260816_180900_color", "2026-08-16T18:09:05"),
        ("RO000H_20260816_180920_color", "2026-08-16T18:09:25"),
    ):
        (meteors / f"{stem}.mp4").write_bytes(b"clip")
        _add_pointer(index, "RO000H", "20260816", "clip", f"{stem}.mkv",
                     {"meteor_time": mt})

    detection = {"time_utc": "2026-08-16T18:09:24", "cam": "RO000H"}
    found = nt.find_clip("RO000H", "20260816", detection, window_s=30)

    assert found.name == "RO000H_20260816_180920_color.mp4"


def test_clip_without_meteor_time_is_skipped(index, tmp_path, monkeypatch):
    """Some pointers carry only {"locked": true}."""
    monkeypatch.setattr(cache_store, "ARCHIVE_PATH", tmp_path)
    meteors = tmp_path / "RO000H" / "20260816" / "meteors"
    meteors.mkdir(parents=True)
    (meteors / "RO000H_20260816_180905_color.mp4").write_bytes(b"clip")
    _add_pointer(index, "RO000H", "20260816", "clip",
                 "RO000H_20260816_180905_color.mkv", {"locked": True})

    detection = {"time_utc": "2026-08-16T18:09:21", "cam": "RO000H"}
    assert nt.find_clip("RO000H", "20260816", detection, window_s=30) is None


# ── night stacks come from the index, not a glob ─────────────────────────────


def test_night_stacks_only_returns_cameras_that_have_one(index, tmp_path, monkeypatch):
    monkeypatch.setattr(cache_store, "ARCHIVE_PATH", tmp_path)
    tl = tmp_path / "RO000H" / "20260816" / "timelapse"
    tl.mkdir(parents=True)
    (tl / "RO000H_20260816_night_stack.webp").write_bytes(b"webp")
    _add_pointer(index, "RO000H", "20260816", "nightstack",
                 "RO000H_20260816_night_stack.webp")
    # RO000P has no timelapse dir at all -- a real gap, not a path bug.
    _add_pointer(index, "RO000P", "20260816", "nightstack",
                 "RO000P_20260816_night_stack.webp")

    stacks = nt.night_stacks("20260816", ["RO000H", "RO000P"])

    assert set(stacks) == {"RO000H"}


# ── the Romanian post ────────────────────────────────────────────────────────


def _night_with_perseids(index):
    for i in range(36):
        _add_detection(index, "RO000H", "20260816", 180000 + i, 1.5, "PER")
    for i in range(60):
        _add_detection(index, "RO000H", "20260816", 190000 + i, 2.0, "SPO")
    _add_detection(index, "RO001F", "20260816", 215221, -6.35, "PER")
    return nt.collect_night("20260816", {"RO000H", "RO001F"}, -4.0)


def test_post_is_in_romanian(index):
    night = _night_with_perseids(index)
    groups = nt.classify(night, {"a": ["RO000H"], "b": ["RO001F"]}, set(), 30)
    msg = nt.build_message(night, groups, {"RO000H": "Ciocârlia",
                                           "RO001F": "Bisoca"}, -4.0)

    assert "Rezumatul nopții" in msg
    assert "detecții" in msg
    assert "16 august 2026" in msg
    assert "Nightly Summary" not in msg
    assert "meteors" not in msg


def test_post_names_the_active_shower(index):
    night = _night_with_perseids(index)
    groups = nt.classify(night, {"a": ["RO000H"], "b": ["RO001F"]}, set(), 30)
    msg = nt.build_message(night, groups, {}, -4.0)

    assert "Perseide" in msg
    assert "Roiuri" in msg


def test_post_reports_the_real_fireball_and_its_magnitude(index):
    night = _night_with_perseids(index)
    groups = nt.classify(night, {"a": ["RO000H"], "b": ["RO001F"]}, set(), 30)
    msg = nt.build_message(night, groups, {"RO001F": "Bisoca"}, -4.0)

    assert "1 bolid" in msg
    assert "-6.4" in msg or "-6.3" in msg
    assert "Bisoca" in msg


def test_post_pluralises_fireballs(index):
    for i, mag in enumerate([-4.5, -5.5]):
        _add_detection(index, "RO000H", "20260816", 180000 + i, mag)
    night = nt.collect_night("20260816", {"RO000H"}, -4.0)
    groups = nt.classify(night, {"a": ["RO000H"]}, set(), 30)
    assert "2 bolizi" in nt.build_message(night, groups, {}, -4.0)


def test_post_falls_back_to_brightest_when_no_fireball(index):
    _add_detection(index, "RO000H", "20260816", 180000, -2.6)
    night = nt.collect_night("20260816", {"RO000H"}, -4.0)
    groups = nt.classify(night, {"a": ["RO000H"]}, set(), 30)
    msg = nt.build_message(night, groups, {"RO000H": "Ciocârlia"}, -4.0)

    assert "bolid" not in msg
    assert "Cea mai luminoasă" in msg
    assert "-2.6" in msg


def test_empty_night_says_so_without_crashing(index):
    night = nt.collect_night("20260816", {"RO000H"}, -4.0)
    groups = nt.classify(night, {"a": ["RO000H"]}, set(), 30)
    msg = nt.build_message(night, groups, {}, -4.0)

    assert "Nicio detecție" in msg


def test_location_names_are_html_escaped(index):
    _add_detection(index, "RO000H", "20260816", 180000, 1.0)
    night = nt.collect_night("20260816", {"RO000H"}, -4.0)
    groups = nt.classify(night, {"a": ["RO000H"]}, set(), 30)
    msg = nt.build_message(night, groups, {"RO000H": "Foo & <Bar>"}, -4.0)

    assert "Foo &amp; &lt;Bar&gt;" in msg
    assert "<Bar>" not in msg


def test_public_base_url_becomes_a_link(index, monkeypatch):
    monkeypatch.setenv("ROVIMEN_PUBLIC_BASE_URL", "https://dashboard.rovimen.org/")
    _add_detection(index, "RO000H", "20260816", 180000, 1.0)
    night = nt.collect_night("20260816", {"RO000H"}, -4.0)
    groups = nt.classify(night, {"a": ["RO000H"]}, set(), 30)
    msg = nt.build_message(night, groups, {}, -4.0)

    assert 'href="https://dashboard.rovimen.org"' in msg


# ── Telegram delivery is checked ─────────────────────────────────────────────


class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def test_send_message_reports_failure(monkeypatch):
    monkeypatch.setattr(nt.requests, "post",
                        lambda *a, **k: _Resp(400, text="bad parse_mode"))
    tg = nt.Telegram("tok", ["-100"], pause=0)

    assert tg.send_message("hi") is False


def test_send_message_reports_success(monkeypatch):
    monkeypatch.setattr(nt.requests, "post", lambda *a, **k: _Resp(200))
    tg = nt.Telegram("tok", ["-100"], pause=0)

    assert tg.send_message("hi") is True


def test_rate_limit_is_retried_with_retry_after(monkeypatch):
    calls = {"n": 0}
    slept: list[float] = []

    def fake_post(*_a, **_k):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Resp(429, {"parameters": {"retry_after": 3}})
        return _Resp(200)

    monkeypatch.setattr(nt.requests, "post", fake_post)
    monkeypatch.setattr(nt.time, "sleep", slept.append)
    tg = nt.Telegram("tok", ["-100"], pause=0)

    assert tg.send_message("hi") is True
    assert calls["n"] == 2
    assert 4.0 in slept


def test_photos_are_posted_as_one_album(monkeypatch, tmp_path):
    seen: list[tuple[str, dict]] = []

    def fake_post(url, data=None, files=None, timeout=None):
        seen.append((url, data or {}))
        return _Resp(200)

    monkeypatch.setattr(nt.requests, "post", fake_post)
    monkeypatch.setattr(nt.time, "sleep", lambda _s: None)
    photos = []
    for i in range(3):
        p = tmp_path / f"p{i}.jpg"
        p.write_bytes(b"jpg")
        photos.append((p, f"cam {i}"))
    tg = nt.Telegram("tok", ["-100"], pause=0)

    assert tg.send_photo_album(photos) is True
    assert len(seen) == 1, "three photos must be one album, not three messages"
    assert "sendMediaGroup" in seen[0][0]
    assert len(json.loads(seen[0][1]["media"])) == 3


def test_albums_are_chunked_at_ten(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(nt.requests, "post",
                        lambda url, **k: (seen.append(url), _Resp(200))[1])
    monkeypatch.setattr(nt.time, "sleep", lambda _s: None)
    photos = []
    for i in range(12):
        p = tmp_path / f"p{i}.jpg"
        p.write_bytes(b"jpg")
        photos.append((p, ""))
    tg = nt.Telegram("tok", ["-100"], pause=0)

    tg.send_photo_album(photos)

    assert len(seen) == 2  # 10 + 2


def test_failed_summary_does_not_write_a_sent_marker(index, tmp_path, monkeypatch):
    """A rejected post must not mark the night as done."""
    cfg = _write_config(tmp_path, {
        "gmnro03": {"status": "active", "location_name": "Ciocarlia",
                    "cameras": [{"code": "RO000H"}]},
    })
    _add_detection(index, "RO000H", "20260816", 180000, -1.0)
    state = tmp_path / "state"
    monkeypatch.setenv("ROVIMEN_CONFIG", str(cfg))
    monkeypatch.setenv("ROVIMEN_NIGHTLY_STATE_DIR", str(state))
    monkeypatch.setenv("ROVIMEN_TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("ROVIMEN_TELEGRAM_RO_CHAT_ID", "-100")
    monkeypatch.setattr(detection_db, "is_ready", lambda *a, **k: True)
    monkeypatch.setattr(nt.requests, "post", lambda *a, **k: _Resp(400, text="nope"))
    monkeypatch.setattr(nt.time, "sleep", lambda _s: None)

    rc = nt.run("20260816", dry_run=False, force=False, send_clip=False)

    assert rc == 1
    assert not (state / "20260816.sent").exists()


# ── completeness: nothing topped-N, nothing truncated ────────────────────────


def test_every_reporting_camera_is_listed(index):
    """The post is a reconcilable record, not a highlights reel."""
    for cam in range(12):
        code = f"RO{cam:04X}"
        for i in range(cam + 1):
            _add_detection(index, code, "20260816", 180000 + cam * 100 + i, 1.0)
    cams = {f"RO{c:04X}" for c in range(12)}
    night = nt.collect_night("20260816", cams, -4.0)
    groups = nt.classify(night, {"s": sorted(cams)}, set(), 30)

    msg = nt.build_message(night, groups, {c: c for c in cams}, -4.0)

    assert len(groups["reported"]) == 12
    for code in cams:
        assert code in msg, f"{code} missing from the post"


def test_every_shower_is_listed_with_sporadics(index):
    for i, code in enumerate(["PER", "KCG", "NDA", "SDA", "AUD", "ERI", "SPO"]):
        for n in range(i + 1):
            _add_detection(index, "RO000H", "20260816", 180000 + i * 100 + n, 1.0, code)
    night = nt.collect_night("20260816", {"RO000H"}, -4.0)
    groups = nt.classify(night, {"s": ["RO000H"]}, set(), 30)

    msg = nt.build_message(night, groups, {}, -4.0)

    for name in ("Perseide", "Kappa Cygnide", "Delta Aquaride nordice",
                 "Delta Aquaride sudice", "Aurigide", "Eta Eridanide", "sporadici"):
        assert name in msg, f"{name} missing"


def test_every_fireball_is_listed(index):
    for i in range(9):
        _add_detection(index, "RO000H", "20260816", 180000 + i, -4.1 - i * 0.3)
    night = nt.collect_night("20260816", {"RO000H"}, -4.0)
    groups = nt.classify(night, {"s": ["RO000H"]}, set(), 30)

    msg = nt.build_message(night, groups, {}, -4.0)

    assert "9 bolizi" in msg
    assert msg.count("mag <b>") == 9


def test_totals_are_labelled_as_detections_not_meteors(index):
    """One meteor seen by three cameras is three index rows."""
    for cam in ("RO000H", "RO000L", "RO000J"):
        _add_detection(index, cam, "20260816", 180000, 1.0)
    night = nt.collect_night("20260816", {"RO000H", "RO000L", "RO000J"}, -4.0)
    groups = nt.classify(night, {"s": ["RO000H", "RO000L", "RO000J"]}, set(), 30)

    msg = nt.build_message(night, groups, {}, -4.0)

    assert "<b>3</b> detecții" in msg
    assert "meteori" not in msg


def test_long_post_is_split_not_truncated():
    text = "\n".join(f"  line {i} with some padding text" for i in range(400))
    chunks = nt.split_message(text)

    assert len(chunks) > 1
    assert all(len(c) <= nt.MESSAGE_LIMIT for c in chunks)
    # Every line survives, in order.
    assert "\n".join(chunks) == text


def test_short_post_is_one_chunk():
    assert nt.split_message("hello") == ["hello"]


def test_split_never_cuts_inside_a_line():
    text = "\n".join(f"<b>row {i}</b> aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" for i in range(300))
    for chunk in nt.split_message(text):
        for line in chunk.split("\n"):
            assert line.count("<b>") == line.count("</b>")


def test_send_message_posts_every_chunk(monkeypatch):
    sent = []

    def fake_post(url, data=None, files=None, timeout=None):
        sent.append((data or {}).get("text", ""))
        return _Resp(200)

    monkeypatch.setattr(nt.requests, "post", fake_post)
    monkeypatch.setattr(nt.time, "sleep", lambda _s: None)
    long_text = "\n".join(f"  line {i} padded out a bit" for i in range(400))
    tg = nt.Telegram("tok", ["-100"], pause=0)

    assert tg.send_message(long_text) is True
    assert len(sent) == len(nt.split_message(long_text)) > 1
    assert "\n".join(sent) == long_text


# ── operator detail must not vanish when no operator chat is set ─────────────
#
# The 2026-08-21 06:33 UTC run logged "8 index-gap" and posted to 1 chat: the
# deployment has no ROVIMEN_TELEGRAM_CHAT_ID, so the appendix naming those eight
# cameras was built and then dropped. The counts were in the journal; the camera
# names were nowhere.


def _run_with_one_chat(index, tmp_path, monkeypatch, sent, ops_chat=None):
    cfg = _write_config(tmp_path, {
        "gmnro05": {"status": "active", "location_name": "Bistrita",
                    "cameras": [{"code": "RO0003"}]},
        "gmnro03": {"status": "active", "location_name": "Ciocarlia",
                    "cameras": [{"code": "RO000H"}]},
    })
    _add_detection(index, "RO000H", "20260816", 180000, -1.0)
    _set_poll(index, "gmnro05", 1e12)
    _set_poll(index, "gmnro03", 1e12)
    for i in range(105):
        _add_pointer(index, "RO0003", "20260816", "clip",
                     f"RO0003_20260816_{180000 + i:06d}_color.mkv", {"locked": True})
    monkeypatch.setenv("ROVIMEN_CONFIG", str(cfg))
    monkeypatch.setenv("ROVIMEN_NIGHTLY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ROVIMEN_TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("ROVIMEN_TELEGRAM_RO_CHAT_ID", "-100")
    if ops_chat:
        monkeypatch.setenv("ROVIMEN_TELEGRAM_CHAT_ID", ops_chat)
    else:
        monkeypatch.delenv("ROVIMEN_TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setattr(detection_db, "is_ready", lambda *a, **k: True)
    monkeypatch.setattr(nt.time, "sleep", lambda _s: None)

    def fake_post(url, data=None, files=None, timeout=None):
        sent.append(((data or {}).get("chat_id"), (data or {}).get("text", "")))
        return _Resp(200)

    monkeypatch.setattr(nt.requests, "post", fake_post)
    return nt.run("20260816", dry_run=False, force=True, send_clip=False)


def test_ops_detail_is_logged_when_there_is_no_operator_chat(
        index, tmp_path, monkeypatch, caplog):
    sent: list = []
    caplog.set_level("WARNING", logger="nightly_telegram")

    rc = _run_with_one_chat(index, tmp_path, monkeypatch, sent)

    assert rc == 0
    # Nothing operator-shaped went to the public channel.
    assert all("RO0003" not in text for _chat, text in sent)
    # But the camera is named in the log, with its clip count, and without tags.
    logged = caplog.text
    assert "RO0003" in logged
    assert "105 clips" in logged
    assert "<b>" not in logged


def test_ops_detail_still_goes_to_the_operator_chat_when_set(
        index, tmp_path, monkeypatch):
    sent: list = []
    _run_with_one_chat(index, tmp_path, monkeypatch, sent, ops_chat="42")

    ops_msgs = [t for c, t in sent if c == "42" and "RO0003" in t]
    assert ops_msgs, "operator chat must still receive the appendix"
    assert all("RO0003" not in t for c, t in sent if c == "-100")


def test_strip_html_unwraps_tags_and_entities():
    assert nt._strip_html("<b>a &amp; b</b>\nc") == "a & b\nc"


# ── default night ────────────────────────────────────────────────────────────
#
# The summary posts to a PUBLIC channel and writes <night>.sent, so naming a
# night that has not happened yet both publishes a false "no detections" and
# makes the next morning's real summary skip as already sent. The 06:30 UTC
# timer has Persistent=true, so a VPS booted after noon catches up the missed
# run in the afternoon; a manual afternoon re-run does the same.


def _frozen_now(monkeypatch, iso: str) -> None:
    from datetime import datetime as _dt

    fixed = _dt.fromisoformat(iso)

    class _Frozen(_dt):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001, ANN206 -- datetime.now signature
            return fixed if tz is None else fixed.astimezone(tz)

    monkeypatch.setattr(nt, "datetime", _Frozen)


@pytest.mark.parametrize(
    ("now", "night"),
    [
        ("2026-11-22T06:30:00+00:00", "20261121"),  # the timer's own run
        ("2026-11-22T13:00:00+00:00", "20261121"),  # Persistent=true catch-up after a noon boot
        ("2026-11-22T23:59:00+00:00", "20261121"),  # tonight is still running
        ("2026-11-22T05:59:00+00:00", "20261120"),  # last night not over by the 06:00 cut
    ],
)
def test_default_night_is_the_last_night_that_ended_by_0600_utc(monkeypatch, now, night):
    _frozen_now(monkeypatch, now)
    assert nt.default_night_id() == night
