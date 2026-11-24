"""Download-basket store + routes: per-user isolation, payload validation,
and the one-zip checkout (clips + stack images + manifest.json).

Follows the real-create_app pattern from ``test_hall_of_fame.py`` so the
genuine auth gate + decorators + config model are exercised.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml


# ── Store unit tests (no app) ───────────────────────────────────────────


@pytest.fixture
def bk_store(tmp_path):
    import basket_store as store

    orig_path, orig_lock = store._PATH, store._LOCK_PATH
    store._PATH = tmp_path / "download_baskets.json"
    store._LOCK_PATH = tmp_path / "basket.lock"
    yield store
    store._PATH, store._LOCK_PATH = orig_path, orig_lock


def _entry(i: int, **over) -> dict:
    base = {
        "id": f"bk_{i:012x}",
        "camera": "PUB001",
        "date": "2026-07-01",
        "filename": f"PUB001_20260701_22000{i}_color.mkv",
        "added_at": f"2026-07-01T00:00:0{min(i, 9)}+00:00",
    }
    base.update(over)
    return base


class TestStore:
    def test_add_list_per_user_isolation(self, bk_store):
        bk_store.add("alice", _entry(1), max_items=10)
        bk_store.add("bob", _entry(2), max_items=10)
        assert [e["id"] for e in bk_store.list_for("alice")] == ["bk_000000000001"]
        assert [e["id"] for e in bk_store.list_for("bob")] == ["bk_000000000002"]
        assert bk_store.list_for("carol") == []

    def test_duplicate_clip_raises(self, bk_store):
        bk_store.add("alice", _entry(1), max_items=10)
        with pytest.raises(bk_store.DuplicateClipError):
            bk_store.add("alice", _entry(2, filename=_entry(1)["filename"]),
                         max_items=10)
        # Same clip in ANOTHER user's basket is fine.
        bk_store.add("bob", _entry(3, filename=_entry(1)["filename"]),
                     max_items=10)

    def test_item_cap_raises(self, bk_store):
        bk_store.add("alice", _entry(1), max_items=2)
        bk_store.add("alice", _entry(2), max_items=2)
        with pytest.raises(bk_store.BasketFullError):
            bk_store.add("alice", _entry(3), max_items=2)

    def test_remove_and_clear(self, bk_store):
        bk_store.add("alice", _entry(1), max_items=10)
        bk_store.add("alice", _entry(2), max_items=10)
        assert bk_store.remove("alice", "bk_000000000001") is True
        assert bk_store.remove("alice", "bk_000000000001") is False
        assert bk_store.clear("alice") == 1
        assert bk_store.clear("alice") == 0
        assert bk_store.list_for("alice") == []

    def test_find_clip(self, bk_store):
        e = _entry(1)
        bk_store.add("alice", e, max_items=10)
        assert bk_store.find_clip("alice", e["camera"], e["date"],
                                  e["filename"])["id"] == e["id"]
        assert bk_store.find_clip("bob", e["camera"], e["date"],
                                  e["filename"]) is None

    def test_corrupt_file_recovers_empty(self, bk_store):
        bk_store._PATH.write_text("{not json")
        assert bk_store.list_for("alice") == []


# ── App factory (real create_app; pattern from test_hall_of_fame) ────────


def _build_app():
    tmp = Path(tempfile.mkdtemp(prefix="rovimen_basket_"))

    (tmp / "users.yaml").write_text(yaml.safe_dump({
        "admin":  {"display_name": "Admin",  "password_hash": "",
                   "role": "admin",   "require_totp": False},
        "viewer": {"display_name": "Viewer", "password_hash": "",
                   "role": "visitor", "require_totp": False},
    }))

    cfg = {
        "correlation_window_s": 1,
        "station_api_port": 7779,
        "public_pages": ["overview", "live"],
        "stations": {
            "pub_station": {
                "ip": "127.0.0.1", "label": "Public Station",
                "ssh_user": "test", "public": True,
                "lat": 45.0, "lon": 25.0,
                "cameras": [{"code": "PUB001", "cam_ip": "127.0.0.1",
                             "label": "N", "rotate": False}],
            },
        },
    }
    config_path = tmp / "dashboard_config.yaml"
    config_path.write_text(yaml.safe_dump(cfg))

    os.environ["ROVIMEN_USERS_PATH"] = str(tmp / "users.yaml")
    os.environ["ROVIMEN_SECRET_KEY"] = "basket-test-secret"
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

    security._LOGIN_THRESHOLD = _saved_threshold
    security._LOGIN_LOCKOUT_SECONDS = _saved_lockout
    security.init_limiter = _saved_init_limiter
    return app, tmp


def _logged_in(app, user="viewer", role="visitor"):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["user"] = user
        sess["role"] = role
        sess["stations"] = []
        sess["mfa"] = "totp"
    return c


@pytest.fixture
def basket_app(bk_store):
    app, tmp = _build_app()
    yield app, tmp, bk_store


def _clip_payload(**over):
    p = {
        "camera": "PUB001",
        "date": "20260701",   # compact on purpose — server must normalise
        "filename": "PUB001_20260701_220001_color.mkv",
    }
    p.update(over)
    return p


# ── Route surface ───────────────────────────────────────────────────────


class TestRoutes:
    def test_anon_gets_401_everywhere(self, basket_app):
        app, _, _ = basket_app
        anon = app.test_client()
        assert anon.get("/api/basket").status_code == 401
        assert anon.post("/api/basket", json=_clip_payload()).status_code == 401
        assert anon.post("/api/basket/clear", json={}).status_code == 401
        assert anon.get("/api/basket/checkout").status_code == 401

    def test_add_normalises_date_and_lists(self, basket_app):
        app, _, _ = basket_app
        c = _logged_in(app)
        r = c.post("/api/basket", json=_clip_payload())
        assert r.status_code == 201
        item = r.get_json()["item"]
        assert item["date"] == "2026-07-01"
        got = c.get("/api/basket").get_json()
        assert got["count"] == 1
        assert got["items"][0]["filename"] == _clip_payload()["filename"]

    def test_add_rejects_bad_payloads(self, basket_app):
        app, _, _ = basket_app
        c = _logged_in(app)
        assert c.post("/api/basket", json=_clip_payload(camera="../x")).status_code == 400
        assert c.post("/api/basket", json=_clip_payload(date="yesterday")).status_code == 400
        assert c.post("/api/basket", json=_clip_payload(
            filename="evil/../../etc/passwd")).status_code == 400
        assert c.post("/api/basket", json=_clip_payload(
            filename="notavideo.txt")).status_code == 400

    def test_duplicate_add_is_409(self, basket_app):
        app, _, _ = basket_app
        c = _logged_in(app)
        assert c.post("/api/basket", json=_clip_payload()).status_code == 201
        # Same clip with the OTHER date shape must still collide.
        assert c.post("/api/basket",
                      json=_clip_payload(date="2026-07-01")).status_code == 409

    def test_baskets_are_private_per_user(self, basket_app):
        app, _, _ = basket_app
        viewer = _logged_in(app, "viewer", "visitor")
        admin = _logged_in(app, "admin", "admin")
        item = viewer.post("/api/basket", json=_clip_payload()).get_json()["item"]
        assert admin.get("/api/basket").get_json()["count"] == 0
        # Another account can't delete someone else's entry.
        assert admin.delete(f"/api/basket/{item['id']}", json={}).status_code == 404
        assert viewer.delete(f"/api/basket/{item['id']}", json={}).status_code == 200
        assert viewer.get("/api/basket").get_json()["count"] == 0

    def test_remove_validates_id_shape(self, basket_app):
        app, _, _ = basket_app
        c = _logged_in(app)
        assert c.delete("/api/basket/../../etc", json={}).status_code in (400, 404)
        assert c.delete("/api/basket/bk_zzzz", json={}).status_code == 400

    def test_clear(self, basket_app):
        app, _, _ = basket_app
        c = _logged_in(app)
        c.post("/api/basket", json=_clip_payload())
        r = c.post("/api/basket/clear", json={})
        assert r.status_code == 200 and r.get_json()["removed"] == 1
        assert c.get("/api/basket").get_json()["count"] == 0


# ── Checkout ────────────────────────────────────────────────────────────


def _seed_archive(root: Path, camera="PUB001", date="20260701",
                  clip="PUB001_20260701_220001_color.mkv",
                  stack="PUB001_20260701_220001_stack.webp") -> Path:
    meteors = root / camera / date / "meteors"
    meteors.mkdir(parents=True, exist_ok=True)
    if clip:
        (meteors / clip).write_bytes(b"MKVDATA-" + clip.encode())
    if stack:
        (meteors / stack).write_bytes(b"WEBPDATA-" + stack.encode())
    return root


class TestCheckout:
    def test_empty_basket_is_400(self, basket_app):
        app, _, _ = basket_app
        assert _logged_in(app).get("/api/basket/checkout").status_code == 400

    def test_zip_contains_clip_stack_and_manifest(self, basket_app, tmp_path):
        app, _, _ = basket_app
        import cache_store

        archive = _seed_archive(tmp_path / "archive")
        c = _logged_in(app)
        assert c.post("/api/basket", json=_clip_payload()).status_code == 201

        with patch.object(cache_store, "ARCHIVE_PATH", archive):
            r = c.get("/api/basket/checkout")
        assert r.status_code == 200
        assert r.mimetype == "application/zip"

        zf = zipfile.ZipFile(io.BytesIO(r.data))
        names = set(zf.namelist())
        # Flat root: names embed camera+date+time, no folder nesting.
        assert names == {
            "PUB001_20260701_220001_color.mkv",
            "PUB001_20260701_220001_stack.webp",
            "manifest.json",
        }
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["item_count"] == 1
        assert manifest["items"][0]["status"] == "included"
        assert manifest["items"][0]["stack"] == (
            "PUB001_20260701_220001_stack.webp")

    def test_falls_back_to_mp4_sibling(self, basket_app, tmp_path):
        app, _, _ = basket_app
        import cache_store

        archive = _seed_archive(tmp_path / "archive", clip=None, stack=None)
        meteors = archive / "PUB001" / "20260701" / "meteors"
        (meteors / "PUB001_20260701_220001_color.mp4").write_bytes(b"MP4DATA")

        c = _logged_in(app)
        c.post("/api/basket", json=_clip_payload())   # basketed as .mkv
        with patch.object(cache_store, "ARCHIVE_PATH", archive):
            r = c.get("/api/basket/checkout")
        assert r.status_code == 200
        names = set(zipfile.ZipFile(io.BytesIO(r.data)).namelist())
        assert "PUB001_20260701_220001_color.mp4" in names

    def test_prefers_mp4_over_mkv_when_both_archived(self, basket_app, tmp_path):
        app, _, _ = basket_app
        import cache_store

        archive = _seed_archive(tmp_path / "archive")   # .mkv + stack
        meteors = archive / "PUB001" / "20260701" / "meteors"
        (meteors / "PUB001_20260701_220001_color.mp4").write_bytes(b"MP4DATA")

        c = _logged_in(app)
        c.post("/api/basket", json=_clip_payload())   # basketed as .mkv
        with patch.object(cache_store, "ARCHIVE_PATH", archive):
            r = c.get("/api/basket/checkout")
        assert r.status_code == 200
        zf = zipfile.ZipFile(io.BytesIO(r.data))
        names = set(zf.namelist())
        assert "PUB001_20260701_220001_color.mp4" in names
        assert "PUB001_20260701_220001_color.mkv" not in names
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["items"][0]["clip"] == (
            "PUB001_20260701_220001_color.mp4")

    def test_missing_clip_reported_not_fatal(self, basket_app, tmp_path):
        app, _, _ = basket_app
        import cache_store

        archive = _seed_archive(tmp_path / "archive")
        c = _logged_in(app)
        c.post("/api/basket", json=_clip_payload())
        c.post("/api/basket", json=_clip_payload(
            filename="PUB001_20260701_230000_color.mkv"))   # not archived

        with patch.object(cache_store, "ARCHIVE_PATH", archive):
            r = c.get("/api/basket/checkout")
        assert r.status_code == 200
        zf = zipfile.ZipFile(io.BytesIO(r.data))
        manifest = json.loads(zf.read("manifest.json"))
        statuses = {i["filename"]: i["status"] for i in manifest["items"]}
        assert statuses["PUB001_20260701_220001_color.mkv"] == "included"
        assert statuses["PUB001_20260701_230000_color.mkv"] == "missing"

    def test_nothing_archived_is_404(self, basket_app, tmp_path):
        app, _, _ = basket_app
        import cache_store

        c = _logged_in(app)
        c.post("/api/basket", json=_clip_payload())
        with patch.object(cache_store, "ARCHIVE_PATH", tmp_path / "empty"):
            assert c.get("/api/basket/checkout").status_code == 404

    def test_oversize_basket_is_413(self, basket_app, tmp_path):
        app, _, _ = basket_app
        import cache_store
        import routes.basket as basket_routes

        archive = _seed_archive(tmp_path / "archive")
        c = _logged_in(app)
        c.post("/api/basket", json=_clip_payload())
        with patch.object(cache_store, "ARCHIVE_PATH", archive), \
             patch.object(basket_routes, "_MAX_TOTAL_BYTES", 4):
            assert c.get("/api/basket/checkout").status_code == 413


# ── Trimmed entries (modal handle selection) ────────────────────────────


def _fake_cut_writer(calls: list | None = None, fail: str | None = None):
    """A _cut_clip stand-in that writes *dest* (or fails with *fail*)."""
    def _fake(src: Path, start: float, duration: float, dest: Path):
        if calls is not None:
            calls.append((src.name, start, duration, dest.name))
        if fail:
            return fail
        dest.write_bytes(b"CUTDATA")
        return None
    return _fake


class TestTrim:
    def test_add_stores_trim_window(self, basket_app):
        app, _, _ = basket_app
        c = _logged_in(app)
        r = c.post("/api/basket",
                   json=_clip_payload(trim_start=12.34, trim_end=15.42))
        assert r.status_code == 201
        item = r.get_json()["item"]
        # Bounds are rounded to 0.1 s server-side.
        assert (item["trim_start"], item["trim_end"]) == (12.3, 15.4)
        got = c.get("/api/basket").get_json()["items"][0]
        assert (got["trim_start"], got["trim_end"]) == (12.3, 15.4)

    def test_add_without_trim_keeps_todays_entry_shape(self, basket_app):
        app, _, _ = basket_app
        c = _logged_in(app)
        item = c.post("/api/basket", json=_clip_payload()).get_json()["item"]
        assert "trim_start" not in item
        assert "trim_end" not in item

    def test_add_rejects_bad_trim_windows(self, basket_app):
        app, _, _ = basket_app
        c = _logged_in(app)
        # One bound without the other, inverted/empty range, negative, junk.
        assert c.post("/api/basket", json=_clip_payload(
            trim_start=1.0)).status_code == 400
        assert c.post("/api/basket", json=_clip_payload(
            trim_start=5.0, trim_end=3.0)).status_code == 400
        assert c.post("/api/basket", json=_clip_payload(
            trim_start=3.0, trim_end=3.0)).status_code == 400
        assert c.post("/api/basket", json=_clip_payload(
            trim_start=-1.0, trim_end=3.0)).status_code == 400
        assert c.post("/api/basket", json=_clip_payload(
            trim_start="soon", trim_end=3.0)).status_code == 400

    def test_checkout_ships_cut_with_range_suffixed_arcname(
            self, basket_app, tmp_path):
        app, _, _ = basket_app
        import cache_store
        import routes.basket as basket_routes

        archive = _seed_archive(tmp_path / "archive")
        calls: list = []
        c = _logged_in(app)
        assert c.post("/api/basket", json=_clip_payload(
            trim_start=12.3, trim_end=15.4)).status_code == 201
        with patch.object(cache_store, "ARCHIVE_PATH", archive), \
             patch.object(basket_routes, "_cut_clip", _fake_cut_writer(calls)):
            r = c.get("/api/basket/checkout")
        assert r.status_code == 200

        # ffmpeg got the archived source and the stored -ss/-t window.
        src, start, duration, dest = calls[0]
        assert src == "PUB001_20260701_220001_color.mkv"
        assert start == 12.3
        assert duration == pytest.approx(3.1)
        assert dest == "PUB001_20260701_220001_color_t12.3-15.4s.mp4"

        zf = zipfile.ZipFile(io.BytesIO(r.data))
        # The cut replaces the full clip; the suffixed name can't collide
        # with it under the first-wins arcname dedup.
        assert set(zf.namelist()) == {
            "PUB001_20260701_220001_color_t12.3-15.4s.mp4",
            "PUB001_20260701_220001_stack.webp",
            "manifest.json",
        }
        row = json.loads(zf.read("manifest.json"))["items"][0]
        assert row["clip"] == "PUB001_20260701_220001_color_t12.3-15.4s.mp4"
        assert row["trim"] == {"start": 12.3, "end": 15.4,
                               "status": "applied"}

    def test_zip_starts_streaming_before_the_cut_runs(self, basket_app, tmp_path):
        """Cuts used to run before the first zip byte, and a few of them
        outlasted nginx's 30 s: the checkout 504'd. They now run when the
        stream reaches them."""
        app, _, _ = basket_app
        import cache_store
        import routes.basket as basket_routes

        second = "PUB001_20260701_220021_color.mkv"
        archive = _seed_archive(tmp_path / "archive")
        _seed_archive(tmp_path / "archive", clip=second, stack=None)
        calls: list = []
        c = _logged_in(app)
        assert c.post("/api/basket", json=_clip_payload()).status_code == 201
        assert c.post("/api/basket", json=_clip_payload(
            filename=second, trim_start=1.0, trim_end=4.0)).status_code == 201
        with patch.object(cache_store, "ARCHIVE_PATH", archive), \
             patch.object(basket_routes, "_cut_clip", _fake_cut_writer(calls)):
            r = c.get("/api/basket/checkout", buffered=False)
            assert r.status_code == 200
            assert calls == []                 # headers and first bytes first
            data = r.get_data()
            assert len(calls) == 1             # the cut ran inside the stream
        names = set(zipfile.ZipFile(io.BytesIO(data)).namelist())
        assert "PUB001_20260701_220021_color_t1.0-4.0s.mp4" in names
        assert "manifest.json" in names

    def test_cut_failure_falls_back_to_full_clip(self, basket_app, tmp_path):
        app, _, _ = basket_app
        import cache_store
        import routes.basket as basket_routes

        archive = _seed_archive(tmp_path / "archive")
        c = _logged_in(app)
        c.post("/api/basket", json=_clip_payload(trim_start=12.3,
                                                 trim_end=15.4))
        with patch.object(cache_store, "ARCHIVE_PATH", archive), \
             patch.object(basket_routes, "_cut_clip",
                          _fake_cut_writer(fail="boom")):
            r = c.get("/api/basket/checkout")
        assert r.status_code == 200
        zf = zipfile.ZipFile(io.BytesIO(r.data))
        assert "PUB001_20260701_220001_color.mkv" in zf.namelist()
        row = json.loads(zf.read("manifest.json"))["items"][0]
        assert row["status"] == "included"
        assert row["clip"] == "PUB001_20260701_220001_color.mkv"
        assert row["trim"] == {"start": 12.3, "end": 15.4,
                               "status": "cut failed (boom); "
                                         "full clip included"}

    def test_untrimmed_checkout_never_invokes_ffmpeg(self, basket_app,
                                                     tmp_path):
        app, _, _ = basket_app
        import cache_store
        import routes.basket as basket_routes

        archive = _seed_archive(tmp_path / "archive")
        calls: list = []
        c = _logged_in(app)
        c.post("/api/basket", json=_clip_payload())
        with patch.object(cache_store, "ARCHIVE_PATH", archive), \
             patch.object(basket_routes, "_cut_clip", _fake_cut_writer(calls)):
            r = c.get("/api/basket/checkout")
        assert r.status_code == 200
        assert calls == []
        zf = zipfile.ZipFile(io.BytesIO(r.data))
        assert "PUB001_20260701_220001_color.mkv" in zf.namelist()
        assert "trim" not in json.loads(zf.read("manifest.json"))["items"][0]


# ── Fireball analysis packs (admin-only checkout extra) ─────────────────


def _fake_pack_writer(members: dict[str, bytes], calls: list | None = None):
    """A _download_pack stand-in that writes *members* into dest as a zip."""
    def _fake(config, tunnels, camera, date_compact, time_s, dest: Path):
        if calls is not None:
            calls.append((camera, date_compact, time_s))
        with zipfile.ZipFile(dest, "w") as zf:
            for name, data in members.items():
                zf.writestr(name, data)
        return None
    return _fake


class TestFireballPack:
    def test_fireball_flag_is_admin_only(self, basket_app, tmp_path):
        app, _, _ = basket_app
        import cache_store

        archive = _seed_archive(tmp_path / "archive")
        c = _logged_in(app)   # visitor role
        c.post("/api/basket", json=_clip_payload())
        with patch.object(cache_store, "ARCHIVE_PATH", archive):
            r = c.get("/api/basket/checkout?fireball=1")
            r.get_data()  # the zip streams lazily: consume it while patched
        assert r.status_code == 403

    def test_admin_pack_lands_in_zip_with_manifest_status(
            self, basket_app, tmp_path):
        app, _, _ = basket_app
        import cache_store
        import routes.basket as basket_routes

        archive = _seed_archive(tmp_path / "archive")
        calls: list = []
        fake = _fake_pack_writer({
            "SESS/platepar_cmn2010.cal": b"{}",
            "frames/FF_PUB001_20260701_220001_000_0000000.fits": b"F",
            "pack_info.json": b"{}",
        }, calls)

        c = _logged_in(app, "admin", "admin")
        c.post("/api/basket", json=_clip_payload())
        with patch.object(cache_store, "ARCHIVE_PATH", archive), \
             patch.object(basket_routes, "_download_pack", fake):
            r = c.get("/api/basket/checkout?fireball=1")
            r.get_data()  # the zip streams lazily: consume it while patched
        assert r.status_code == 200

        # The station was asked for the right night + event time (from the
        # clip filename), with the ISO date flattened to the archive shape.
        assert calls == [("PUB001", "20260701", "220001")]

        zf = zipfile.ZipFile(io.BytesIO(r.data))
        names = set(zf.namelist())
        # Packs live under a per-event folder so two events' identically
        # named members (SESS/..., frames/...) can't collide.
        assert ("fireball_pack/PUB001_20260701_220001/SESS/platepar_cmn2010.cal"
                in names)
        assert ("fireball_pack/PUB001_20260701_220001/frames/"
                "FF_PUB001_20260701_220001_000_0000000.fits" in names)
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["items"][0]["fireball_pack"] == "included"

    def test_unreachable_station_degrades_to_manifest_note(
            self, basket_app, tmp_path):
        app, _, _ = basket_app
        import cache_store
        import routes.basket as basket_routes

        archive = _seed_archive(tmp_path / "archive")
        c = _logged_in(app, "admin", "admin")
        c.post("/api/basket", json=_clip_payload())
        with patch.object(cache_store, "ARCHIVE_PATH", archive), \
             patch.object(basket_routes, "_download_pack",
                          lambda *a: "station unreachable"):
            r = c.get("/api/basket/checkout?fireball=1")
            r.get_data()  # the zip streams lazily: consume it while patched
        assert r.status_code == 200
        zf = zipfile.ZipFile(io.BytesIO(r.data))
        manifest = json.loads(zf.read("manifest.json"))
        assert (manifest["items"][0]["fireball_pack"]
                == "unavailable: station unreachable")
        # The clip itself still ships.
        assert "PUB001_20260701_220001_color.mkv" in zf.namelist()

    def test_traversal_members_are_dropped(self, basket_app, tmp_path):
        app, _, _ = basket_app
        import cache_store
        import routes.basket as basket_routes

        archive = _seed_archive(tmp_path / "archive")
        fake = _fake_pack_writer({
            "../evil.txt": b"nope",
            "/abs/evil.txt": b"nope",
            "ok/platepar_cmn2010.cal": b"{}",
        })
        c = _logged_in(app, "admin", "admin")
        c.post("/api/basket", json=_clip_payload())
        with patch.object(cache_store, "ARCHIVE_PATH", archive), \
             patch.object(basket_routes, "_download_pack", fake):
            r = c.get("/api/basket/checkout?fireball=1")
            r.get_data()  # the zip streams lazily: consume it while patched
        assert r.status_code == 200
        names = set(zipfile.ZipFile(io.BytesIO(r.data)).namelist())
        assert ("fireball_pack/PUB001_20260701_220001/ok/platepar_cmn2010.cal"
                in names)
        assert not any("evil" in n for n in names)

    def test_one_fetch_per_camera_night_event(self, basket_app, tmp_path):
        app, _, _ = basket_app
        import cache_store
        import routes.basket as basket_routes

        archive = _seed_archive(tmp_path / "archive")
        # Second clip, same night + same event second (e.g. mkv + crop).
        _seed_archive(tmp_path / "archive",
                      clip="PUB001_20260701_220001_crop.mkv", stack=None)
        calls: list = []
        fake = _fake_pack_writer({"pack_info.json": b"{}"}, calls)

        c = _logged_in(app, "admin", "admin")
        c.post("/api/basket", json=_clip_payload())
        c.post("/api/basket", json=_clip_payload(
            filename="PUB001_20260701_220001_crop.mkv"))
        with patch.object(cache_store, "ARCHIVE_PATH", archive), \
             patch.object(basket_routes, "_download_pack", fake):
            r = c.get("/api/basket/checkout?fireball=1")
            r.get_data()  # the zip streams lazily: consume it while patched
        assert r.status_code == 200
        assert len(calls) == 1
        manifest = json.loads(zipfile.ZipFile(io.BytesIO(r.data))
                              .read("manifest.json"))
        assert all(i["fireball_pack"] == "included"
                   for i in manifest["items"])

    def test_packs_are_fetched_in_parallel(self, basket_app, tmp_path):
        # Fetched one after another inside the stream, an 11-pack basket
        # stayed silent ~50 s and nginx's 30 s read timeout cut the download
        # (2026-10-16). Two different stations' packs must be in flight at
        # the same time: each fake fetch waits for the other at a barrier,
        # which only a parallel fetch can pass.
        app, _, _ = basket_app
        import threading

        import cache_store
        import routes.basket as basket_routes

        archive = _seed_archive(tmp_path / "archive")
        _seed_archive(tmp_path / "archive",
                      clip="PUB001_20260701_221500_color.mkv", stack=None)
        barrier = threading.Barrier(2, timeout=5)
        write = _fake_pack_writer({"pack_info.json": b"{}"})

        def fake(config, tunnels, camera, date_compact, time_s, dest):
            barrier.wait()  # BrokenBarrierError if fetched serially
            return write(config, tunnels, camera, date_compact, time_s, dest)

        c = _logged_in(app, "admin", "admin")
        c.post("/api/basket", json=_clip_payload())
        c.post("/api/basket", json=_clip_payload(
            filename="PUB001_20260701_221500_color.mkv"))
        with patch.object(cache_store, "ARCHIVE_PATH", archive), \
             patch.object(basket_routes, "_download_pack", fake):
            r = c.get("/api/basket/checkout?fireball=1")
            r.get_data()  # the zip streams lazily: consume it while patched
        assert r.status_code == 200
        zf = zipfile.ZipFile(io.BytesIO(r.data))
        names = set(zf.namelist())
        assert "fireball_pack/PUB001_20260701_220001/pack_info.json" in names
        assert "fireball_pack/PUB001_20260701_221500/pack_info.json" in names
        manifest = json.loads(zf.read("manifest.json"))
        assert [i["fireball_pack"] for i in manifest["items"]] == [
            "included", "included"]

    def test_plain_checkout_has_no_fireball_keys(self, basket_app, tmp_path):
        app, _, _ = basket_app
        import cache_store

        archive = _seed_archive(tmp_path / "archive")
        c = _logged_in(app, "admin", "admin")
        c.post("/api/basket", json=_clip_payload())
        with patch.object(cache_store, "ARCHIVE_PATH", archive):
            r = c.get("/api/basket/checkout")
        manifest = json.loads(zipfile.ZipFile(io.BytesIO(r.data))
                              .read("manifest.json"))
        assert "fireball_pack" not in manifest["items"][0]
