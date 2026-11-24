"""Archive-first clip serving through /api/cached-video (routes/archive.py).

Push-era inversion: the pusher lands locked clips (and faststart .mp4
siblings) on the storage box within minutes, so the operator route must
serve hot-cache → archive → station, in that order — the station sits
behind a ~10 Mbps home uplink and used to be tried FIRST.

These tests drive a real ``create_app`` instance (same pattern as
test_public_dashboard_authz.py) with ``ARCHIVE_PATH`` / ``CLIP_CACHE_DIR``
pointed at tmp dirs. The single configured station is unreachable
(127.0.0.1 refused), so any response that succeeds proves the bytes came
from the archive tiers, not the station proxy.
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import pytest
import yaml

# Real ffmpeg encodes/decodes: 9-17 s each locally, 4x that on a slow CI runner,
# so the suite-wide 60 s timeout (pyproject) would flake on them.
pytestmark = pytest.mark.timeout(300)


@pytest.fixture(scope="module")
def app_env():
    tmp = Path(tempfile.mkdtemp(prefix="rovimen_archfirst_"))

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
                    {"code": "RO0017", "cam_ip": "127.0.0.1",
                     "label": "N", "rotate": False},
                ],
            },
        },
    }))

    os.environ["ROVIMEN_USERS_PATH"] = str(users_path)
    os.environ["ROVIMEN_SECRET_KEY"] = "archfirst-test-secret"
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
def media_roots(tmp_path, monkeypatch):
    """Point the archive and hot clip cache at fresh tmp dirs."""
    import clip_cache
    import routes.archive as archive_routes

    archive_root = tmp_path / "archive"
    hot_root = tmp_path / "hot"
    archive_root.mkdir()
    hot_root.mkdir()
    monkeypatch.setattr(archive_routes, "ARCHIVE_PATH", archive_root)
    monkeypatch.setattr(clip_cache, "CLIP_CACHE_DIR", hot_root)
    return archive_root, hot_root


CAM = "RO0017"
DATE = "20260709"
STEM = f"{CAM}_{DATE}_214233_1_color"
URL = f"/api/cached-video/teststation/{CAM}/{DATE}/{STEM}"


def _mk(root: Path, rel: str, data: bytes) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


# ── Tier 1: NVMe hot cache ────────────────────────────────────────────────


def test_hot_cache_hit_serves_mp4(admin, media_roots):
    _, hot_root = media_roots
    _mk(hot_root, f"{CAM}/{DATE}/{STEM}.mp4", b"hot-mp4-bytes")
    r = admin.get(f"{URL}.mkv?format=mp4")
    assert r.status_code == 200
    assert r.mimetype == "video/mp4"
    assert r.data == b"hot-mp4-bytes"


def test_hot_cache_hit_via_mp4_filename(admin, media_roots):
    """A payload that learned the .mp4 sibling name hits the same entry."""
    _, hot_root = media_roots
    _mk(hot_root, f"{CAM}/{DATE}/{STEM}.mp4", b"hot-mp4-bytes")
    r = admin.get(f"{URL}.mp4?format=mp4")
    assert r.status_code == 200
    assert r.data == b"hot-mp4-bytes"


# ── Tier 2: storagebox archive (station never contacted) ─────────────────


def test_archive_mp4_sibling_served_directly(admin, media_roots):
    archive_root, _ = media_roots
    _mk(archive_root, f"{CAM}/{DATE}/meteors/{STEM}.mp4", b"sibling-bytes")
    r = admin.get(f"{URL}.mkv?format=mp4")
    assert r.status_code == 200
    assert r.mimetype == "video/mp4"
    assert r.data == b"sibling-bytes"
    assert "immutable" in r.headers.get("Cache-Control", "")


def test_archive_sibling_promoted_to_hot_cache(admin, media_roots):
    """Serving the archive sibling also copies it onto NVMe in the
    background, so the next click is a hot-cache hit."""
    archive_root, hot_root = media_roots
    _mk(archive_root, f"{CAM}/{DATE}/meteors/{STEM}.mp4", b"sibling-bytes")
    r = admin.get(f"{URL}.mkv?format=mp4")
    assert r.status_code == 200
    hot = hot_root / CAM / DATE / f"{STEM}.mp4"
    deadline = time.time() + 5.0
    while time.time() < deadline and not hot.exists():
        time.sleep(0.05)
    assert hot.read_bytes() == b"sibling-bytes"


def test_mp4_filename_accepted_and_served(admin, media_roots):
    """Regression: .mp4 payload filenames used to 400 at validation."""
    archive_root, _ = media_roots
    _mk(archive_root, f"{CAM}/{DATE}/meteors/{STEM}.mp4", b"sibling-bytes")
    r = admin.get(f"{URL}.mp4")
    assert r.status_code == 200
    assert r.mimetype == "video/mp4"
    assert r.data == b"sibling-bytes"


def test_archive_mkv_served_without_format_param(admin, media_roots):
    archive_root, _ = media_roots
    _mk(archive_root, f"{CAM}/{DATE}/meteors/{STEM}.mkv", b"matroska-bytes")
    r = admin.get(f"{URL}.mkv")
    assert r.status_code == 200
    assert r.mimetype == "video/x-matroska"
    assert r.data == b"matroska-bytes"


def test_night_root_archive_file_found(admin, media_roots):
    """Clips at the night root (older upload layout) are still found."""
    archive_root, _ = media_roots
    _mk(archive_root, f"{CAM}/{DATE}/{STEM}.mkv", b"root-bytes")
    r = admin.get(f"{URL}.mkv")
    assert r.status_code == 200
    assert r.data == b"root-bytes"


# ── Validation ────────────────────────────────────────────────────────────


def test_other_extensions_still_rejected(admin, media_roots):
    r = admin.get(f"/api/cached-video/teststation/{CAM}/{DATE}/{STEM}.webm")
    assert r.status_code == 400


def test_traversal_rejected(admin, media_roots):
    r = admin.get(
        f"/api/cached-video/teststation/{CAM}/{DATE}/..%2f..%2fevil.mkv"
    )
    assert r.status_code in (400, 404)


# ── VDB chunks: recent nights from the pushed chunk listing (#1250) ──────


@pytest.fixture
def pushed(tmp_path, monkeypatch):
    """A fresh station_state DB the chunk listings are stored in and read
    from, and a station_get_raw that fails the test if anything calls it."""
    import chunk_listings
    import routes.media as media_routes
    import station_state

    db = tmp_path / "station_state.db"
    monkeypatch.setattr(station_state, "DB_PATH", db)
    monkeypatch.setattr(chunk_listings, "_schema_ready", set())

    def no_station(*a, **kw):
        raise AssertionError(f"station called: {a[3] if len(a) > 3 else a}")

    monkeypatch.setattr(media_routes, "station_get_raw", no_station, raising=False)

    def put(cam, date, chunks, locks=None, morning_done=False):
        chunk_listings.store("teststation", {
            "cam": cam, "date": date, "morning_done": morning_done,
            "chunks": chunks, "locks": locks or {}, "reencoded": [],
        }, path=db)
    return put


def test_vdb_chunks_tonight_come_from_the_pushed_listing(admin, pushed):
    """Tonight's chunk list is the listing the station pushed, expanded to
    the station's own /api/chunks shape; no station round-trip, first open
    or second."""
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    pushed(CAM, today, [[f"{today}_214233", 12.5]],
           locks={f"{today}_214233": ["detection", f"{today[:4]}-{today[4:6]}-{today[6:]} 21:42:40.1", 7.1]})
    url = f"/api/videodb/chunks/teststation/{CAM}/{today}?locked_only=0"
    r1 = admin.get(url)
    assert r1.status_code == 200
    (chunk,) = r1.get_json()
    assert chunk["filename"] == f"{CAM}_{today}_214233_color.mkv"
    assert chunk["stack"] == f"{CAM}_{today}_214233_stack.webp"
    assert chunk["time"] == "21:42:33"
    assert chunk["size_mb"] == 12.5
    assert chunk["locked"] is True and chunk["lock_type"] == "detection"
    assert chunk["detection_offset_s"] == 7.1
    r2 = admin.get(url)
    assert r2.get_json() == r1.get_json()


def test_vdb_chunks_station_error_degrades_to_archive(admin, monkeypatch):
    import routes.media as media_routes
    from datetime import datetime, timezone

    def boom(*a, **kw):
        raise RuntimeError("station unreachable")

    monkeypatch.setattr(media_routes, "station_get_raw", boom, raising=False)
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    r = admin.get(f"/api/videodb/chunks/teststation/RO0099/{today}")
    assert r.status_code == 200
    assert r.get_json() == []  # archive has nothing for this camera


# ── VDB archive dedup: faststart .mp4 sibling must not double-list ────────


def test_vdb_archive_collapses_faststart_mp4_siblings(admin, monkeypatch):
    """Faststart stations write BOTH X_color.mkv and X_color.mp4 into
    meteors/; the archive VDB must list each clip ONCE (keep the canonical
    .mkv), not twice -- the '100 clips = 50 pairs' bug on gmnro01."""
    import rovimen_dashboard
    from cache_store import _NightIndex

    b1 = f"{CAM}_{DATE}_010203_1"
    b2 = f"{CAM}_{DATE}_040506_2"
    ni = _NightIndex(
        meteor_files=(
            f"{b1}_color.mkv", f"{b1}_color.mp4",
            f"{b2}_color.mkv", f"{b2}_color.mp4",
        ),
        stack_files=frozenset({f"{b1}_stack.webp", f"{b2}_stack.webp"}),
        timelapse_files=(),
    )
    monkeypatch.setattr(rovimen_dashboard._archive_idx, "night_files",
                        lambda cam, date: ni)

    # An old date (>2 days) forces the archive fallback, not the station.
    r = admin.get(f"/api/videodb/chunks/teststation/{CAM}/20260101")
    assert r.status_code == 200
    chunks = r.get_json()
    assert sorted(c["filename"] for c in chunks) == [
        f"{b1}_color.mkv", f"{b2}_color.mkv",
    ]
    # The kept .mkv still resolves its stack.
    for c in chunks:
        assert c["stack"] == c["filename"].replace("_color.mkv", "_stack.webp")


def test_vdb_archive_keeps_mp4_when_mkv_reaped(admin, monkeypatch):
    """The storagebox janitor deletes the heavy .mkv but leaves the light
    .mp4 on aged nights; that clip must still appear (as the .mp4) with its
    stack resolved from the clip base."""
    import rovimen_dashboard
    from cache_store import _NightIndex

    b = f"{CAM}_{DATE}_070809_9"
    ni = _NightIndex(
        meteor_files=(f"{b}_color.mp4",),  # .mkv already reaped
        stack_files=frozenset({f"{b}_stack.webp"}),
        timelapse_files=(),
    )
    monkeypatch.setattr(rovimen_dashboard._archive_idx, "night_files",
                        lambda cam, date: ni)
    r = admin.get(f"/api/videodb/chunks/teststation/{CAM}/20260102")
    assert r.status_code == 200
    chunks = r.get_json()
    assert [c["filename"] for c in chunks] == [f"{b}_color.mp4"]
    assert chunks[0]["stack"] == f"{b}_stack.webp"

# ── VDB default view: archived detections merged into the station list ────


def test_vdb_default_view_merges_archived_detections(admin, monkeypatch, pushed):
    """The retention janitor deletes uploaded (= detection-locked) clips
    from the station FIRST while unlocked bulk footage survives, so the
    default (locked_only=0) list used to come back non-empty but with no
    detections -- they only appeared behind the 'Locked only' toggle. The
    default view must merge archived locked clips back in, deduped by clip
    base so a chunk still on station is not double-listed."""
    import rovimen_dashboard
    from cache_store import _NightIndex
    from datetime import datetime, timezone

    cam = "RO0088"
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    bulk = f"{cam}_{today}_010000_color.mkv"   # survives on station
    det = f"{cam}_{today}_003000_color.mkv"    # janitor-reaped, archive only

    pushed(cam, today, [[f"{today}_010000", 10.0]])
    # Archive holds the reaped detection clip AND a copy of the bulk chunk
    # (uploaded earlier) -- only the former may be merged in.
    ni = _NightIndex(
        meteor_files=(det, bulk),
        stack_files=frozenset({det.replace("_color.mkv", "_stack.webp")}),
        timelapse_files=(),
    )
    monkeypatch.setattr(rovimen_dashboard._archive_idx, "night_files",
                        lambda c, d: ni)

    r = admin.get(f"/api/videodb/chunks/teststation/{cam}/{today}?locked_only=0")
    assert r.status_code == 200
    chunks = r.get_json()
    # Chronological (filename) order: the 00:30 detection before the 01:00 bulk.
    assert [c["filename"] for c in chunks] == [det, bulk]
    merged = chunks[0]
    assert merged["locked"] is True
    assert merged["lock_type"] == "detection"
    assert merged["source"] == "archive"
    # The station's own (pushed) copy of the bulk chunk won, not the archive's.
    assert chunks[1]["source"] == "pushed"


def test_vdb_locked_only_request_keeps_station_semantics(admin, monkeypatch, pushed):
    """locked_only=1 (the RMS tab) must pass the station's locked list
    through untouched -- no archive merge -- so an empty locked list keeps
    falling through to the archive fallback exactly as before."""
    import rovimen_dashboard
    from cache_store import _NightIndex
    from datetime import datetime, timezone

    cam = "RO0077"
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    station_locked = f"{cam}_{today}_020000_color.mkv"
    archive_only = f"{cam}_{today}_003000_color.mkv"

    pushed(cam, today, [[f"{today}_020000", 9.0], [f"{today}_020020", 9.0]],
           locks={f"{today}_020000": ["detection", None, None]})
    ni = _NightIndex(
        meteor_files=(archive_only,),
        stack_files=frozenset(),
        timelapse_files=(),
    )
    monkeypatch.setattr(rovimen_dashboard._archive_idx, "night_files",
                        lambda c, d: ni)

    r = admin.get(f"/api/videodb/chunks/teststation/{cam}/{today}?locked_only=1")
    assert r.status_code == 200
    assert [c["filename"] for c in r.get_json()] == [station_locked]


def test_vdb_time_window_filter_applies_to_the_pushed_listing(admin, pushed):
    """from/to (HHMM, wrapping midnight when from > to) filter the pushed
    listing the way the station's /api/chunks filtered its own."""
    from datetime import datetime, timezone

    cam = "RO0066"
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    pushed(cam, today, [[f"{today}_{t}", 1.0] for t in ("010000", "040000", "200000", "233000")])
    r = admin.get(f"/api/videodb/chunks/teststation/{cam}/{today}?from=23:00&to=02:00")
    assert r.status_code == 200
    assert [c["time"] for c in r.get_json()] == ["01:00:00", "23:30:00"]


# ── starprint: details variant preferred when its sibling exists ──────────


def _details_night_index(base_stem: str, with_details: bool = False):
    """A _NightIndex holding one base clip + its stack, and its details
    render when ``with_details`` (it sits in the same meteors/ listing)."""
    from cache_store import _NightIndex
    return _NightIndex(
        meteor_files=(f"{base_stem}_color.mkv",)
        + ((f"{base_stem}_color.details.mkv",) if with_details else ()),
        stack_files=frozenset({f"{base_stem}_stack.webp"}),
        timelapse_files=(),
    )


def test_archive_meteors_prefers_details_variant(admin, monkeypatch, media_roots):
    """api_archive_meteors hands the frontend X_color.details.mkv when the
    night's meteors/ listing has it, while the stack stays derived from the BASE."""
    import rovimen_dashboard
    import routes.archive as ra

    base_stem = f"{CAM}_{DATE}_010203_1"
    monkeypatch.setattr(rovimen_dashboard._archive_idx, "night_files",
                        lambda cam, date: _details_night_index(base_stem, with_details=True))
    monkeypatch.setattr(ra, "_is_nonempty_file",
                        lambda p: (_ for _ in ()).throw(AssertionError("stat on the box")))

    r = admin.get(f"/api/archive/meteors/{CAM}/{DATE}")
    assert r.status_code == 200
    entries = r.get_json()
    assert len(entries) == 1
    assert entries[0]["filename"] == f"{base_stem}_color.details.mkv"
    # Stack derived from the BASE name, not the details name.
    assert entries[0]["stack"] == f"{base_stem}_stack.webp"


def test_archive_meteors_falls_back_to_base_without_sibling(admin, monkeypatch, media_roots):
    """No details sibling on the box -> the base X_color.mkv is served."""
    import rovimen_dashboard
    _archive_root, _ = media_roots

    base_stem = f"{CAM}_{DATE}_040506_2"
    monkeypatch.setattr(rovimen_dashboard._archive_idx, "night_files",
                        lambda cam, date: _details_night_index(base_stem))
    # No .details.mkv written.

    r = admin.get(f"/api/archive/meteors/{CAM}/{DATE}")
    assert r.status_code == 200
    entries = r.get_json()
    assert [e["filename"] for e in entries] == [f"{base_stem}_color.mkv"]
    assert entries[0]["stack"] == f"{base_stem}_stack.webp"


def test_archive_meteors_skips_details_as_own_entry(admin, monkeypatch, media_roots):
    """A .details.mkv (and its .details.mp4 faststart) that appear directly in
    the meteor listing must not be double-listed: they only ever surface as the
    served filename of their base clip."""
    import rovimen_dashboard
    from cache_store import _NightIndex
    archive_root, _ = media_roots

    base_stem = f"{CAM}_{DATE}_070809_3"
    ni = _NightIndex(
        meteor_files=(
            f"{base_stem}_color.mkv",
            f"{base_stem}_color.details.mkv",
            f"{base_stem}_color.details.mp4",
        ),
        stack_files=frozenset({f"{base_stem}_stack.webp"}),
        timelapse_files=(),
    )
    monkeypatch.setattr(rovimen_dashboard._archive_idx, "night_files",
                        lambda cam, date: ni)
    _mk(archive_root, f"{CAM}/{DATE}/meteors/{base_stem}_color.details.mkv", b"d")

    r = admin.get(f"/api/archive/meteors/{CAM}/{DATE}")
    assert r.status_code == 200
    entries = r.get_json()
    # Exactly ONE entry — the base clip, served AS the details variant.
    assert [e["filename"] for e in entries] == [f"{base_stem}_color.details.mkv"]
    assert entries[0]["stack"] == f"{base_stem}_stack.webp"


def test_nights_full_prefers_details_variant(admin, monkeypatch, media_roots):
    """The all-nights listing (_build_nights_full) prefers the details variant
    too, keeping the stack + lock metadata derived from the base clip."""
    import rovimen_dashboard
    from cache_store import _NightIndex
    archive_root, _ = media_roots

    # Distinct camera per nights_full test: the per-camera nights_full_cache is
    # a module-level cache shared across the module-scoped app.
    cam = "RODETP"
    base_stem = f"{cam}_{DATE}_112233_4"
    # The details render sits next to its base clip in the night's meteors/
    # listing; the swap is answered from that listing, not a stat on the box.
    ni = _NightIndex(
        meteor_files=(f"{base_stem}_color.mkv", f"{base_stem}_color.details.mkv"),
        stack_files=frozenset({f"{base_stem}_stack.webp"}),
        timelapse_files=(),
        state_chunks={
            f"{base_stem}_color.mkv": {
                "lock": {"meteor_time": "2026-03-15T21:12:34.000Z",
                         "detection_time": "2026-03-15T21:12:35.000Z"},
            },
        },
    )
    monkeypatch.setattr(rovimen_dashboard._archive_idx, "nights",
                        lambda c: [DATE])
    monkeypatch.setattr(rovimen_dashboard._archive_idx, "night_files_for_listing",
                        lambda c, date: ni)
    import routes.archive as ra
    monkeypatch.setattr(ra, "_is_nonempty_file",
                        lambda p: (_ for _ in ()).throw(AssertionError("stat on the box")))

    r = admin.get(f"/api/archive/nights_full/{cam}")
    assert r.status_code == 200
    nights = r.get_json()
    assert len(nights) == 1
    meteors = nights[0]["meteors"]
    assert len(meteors) == 1
    assert meteors[0]["filename"] == f"{base_stem}_color.details.mkv"
    assert meteors[0]["stack"] == f"{base_stem}_stack.webp"
    # Lock-derived metadata still resolved from the base clip's state entry.
    assert meteors[0]["meteor_time"] == "2026-03-15T21:12:34.000Z"


def test_nights_full_falls_back_to_base_without_sibling(admin, monkeypatch, media_roots):
    """No details sibling -> nights_full serves the base clip name."""
    import rovimen_dashboard
    from cache_store import _NightIndex
    _archive_root, _ = media_roots

    cam = "RODETF"
    base_stem = f"{cam}_{DATE}_143000_5"
    ni = _NightIndex(
        meteor_files=(f"{base_stem}_color.mkv",),
        stack_files=frozenset({f"{base_stem}_stack.webp"}),
        timelapse_files=(),
    )
    monkeypatch.setattr(rovimen_dashboard._archive_idx, "nights",
                        lambda c: [DATE])
    monkeypatch.setattr(rovimen_dashboard._archive_idx, "night_files_for_listing",
                        lambda c, date: ni)

    r = admin.get(f"/api/archive/nights_full/{cam}")
    assert r.status_code == 200
    meteors = r.get_json()[0]["meteors"]
    assert [m["filename"] for m in meteors] == [f"{base_stem}_color.mkv"]


# ── Station pulls never outlast nginx's 30 s (background job) ──────────────

import threading  # noqa: E402


class _GatedResp:
    status_code = 200

    def __init__(self, parts, gate):
        self._parts, self._gate = parts, gate
        self.headers = {"Content-Length": str(sum(len(p) for p in parts))}

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=None):
        yield self._parts[0]
        self._gate.wait(5)
        yield from self._parts[1:]

    def close(self):
        pass


class _GatedSession:
    """A station that sends its first byte only once ``first_byte`` is set."""

    def __init__(self, parts, first_byte, rest):
        self._parts, self._first, self._rest = parts, first_byte, rest

    def get(self, url, **kw):
        self._first.wait(5)
        return _GatedResp(self._parts, self._rest)

    def head(self, url, **kw):
        raise ConnectionError("no HEAD in this test")


@pytest.fixture
def station(monkeypatch, media_roots):
    import routes.archive as archive_routes

    monkeypatch.setattr(archive_routes, "_station_jobs", {})
    monkeypatch.setattr(archive_routes, "STATION_WAIT_S", 0.3)
    # CACHE_PATH is bound at import; another test module may have imported
    # cache_store first with the production default.
    monkeypatch.setattr(archive_routes, "CACHE_PATH", media_roots[0].parent / "cache")

    def install(parts, first_byte, rest):
        monkeypatch.setattr(archive_routes, "_session_for_url",
                            lambda url: _GatedSession(parts, first_byte, rest))
    return install


def _wait_job_done(timeout=5.0):
    import routes.archive as archive_routes
    deadline = time.time() + timeout
    while time.time() < deadline:
        jobs = list(archive_routes._station_jobs.values())
        if jobs and all(j.done.is_set() for j in jobs):
            return
        time.sleep(0.02)
    raise AssertionError("station job did not finish")


def test_slow_station_answers_202_then_serves_the_pulled_clip(admin, station, monkeypatch):
    """The station renders the overlay before its first byte; the request
    answers 202 + Retry-After instead of hanging into nginx's 504, and the
    retry after the background pull finished gets the MP4."""
    import routes.archive as archive_routes

    first, rest = threading.Event(), threading.Event()
    rest.set()
    station([b"station-", b"bytes"], first, rest)

    def fake_remux(source, tmp_path):
        Path(tmp_path).write_bytes(Path(source).read_bytes())

    monkeypatch.setattr(archive_routes, "_remux_to_mp4", fake_remux)
    date = "20260925"
    url = f"/api/cached-video/teststation/{CAM}/{date}/{CAM}_{date}_190342_color.mkv?format=mp4"
    r = admin.get(url)
    assert r.status_code == 202
    assert r.headers.get("Retry-After") == "3"
    assert r.get_json()["preparing"] is True

    first.set()
    _wait_job_done()
    r2 = admin.get(url)
    assert r2.status_code == 200
    assert r2.mimetype == "video/mp4"
    assert r2.data == b"station-bytes"


def test_mkv_streams_while_the_pull_is_still_running(admin, station):
    first, rest = threading.Event(), threading.Event()
    first.set()
    station([b"first-half-", b"second-half"], first, rest)
    threading.Timer(0.3, rest.set).start()
    date = "20260925"
    r = admin.get(f"/api/cached-video/teststation/{CAM}/{date}/{CAM}_{date}_191302_color.mkv")
    assert r.status_code == 200
    assert r.mimetype == "video/x-matroska"
    assert r.data == b"first-half-second-half"


def test_failed_station_pull_is_a_clean_502(admin, media_roots, monkeypatch):
    import routes.archive as archive_routes

    class _Down:
        def get(self, url, **kw):
            raise ConnectionError("station down")

        def head(self, url, **kw):
            raise ConnectionError("station down")

    monkeypatch.setattr(archive_routes, "_station_jobs", {})
    monkeypatch.setattr(archive_routes, "CACHE_PATH", media_roots[0].parent / "cache")
    monkeypatch.setattr(archive_routes, "_session_for_url", lambda url: _Down())
    date = "20260925"
    r = admin.get(f"/api/cached-video/teststation/{CAM}/{date}/{CAM}_{date}_192002_color.mkv")
    assert r.status_code == 502
    assert r.get_json()["error"] == "Video temporarily unavailable"


def test_archive_file_serves_mp4_for_playback_and_mkv_for_download(admin, media_roots):
    """/api/archive/file ignored ?format=mp4, so Safari/iOS got MKV."""
    archive_root, _ = media_roots
    _mk(archive_root, f"{CAM}/{DATE}/meteors/{STEM}.mkv", b"matroska-bytes")
    _mk(archive_root, f"{CAM}/{DATE}/meteors/{STEM}.mp4", b"sibling-bytes")
    base = f"/api/archive/file/{CAM}/{DATE}/meteors/{STEM}.mkv"
    r = admin.get(base + "?format=mp4")
    assert r.status_code == 200
    assert r.mimetype == "video/mp4"
    assert r.data == b"sibling-bytes"
    d = admin.get(base + "?format=mp4&download=1")
    assert d.data == b"matroska-bytes"


# ── Stitch: open to viewers, sensible size, streamed ──────────────────────

import json as _json  # noqa: E402
import shutil as _shutil  # noqa: E402
import subprocess as _sp  # noqa: E402

_HAVE_FFMPEG = _shutil.which("ffmpeg") is not None and _shutil.which("ffprobe") is not None


def _test_clip(path: Path, seconds: int = 3) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _sp.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
             "-i", f"testsrc=size=1280x848:rate=25:duration={seconds}",
             "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
             "-y", str(path)], check=True)


def _probe_dims(data: bytes, tmp: Path) -> tuple[int, int]:
    f = tmp / "out.mp4"
    f.write_bytes(data)
    out = _sp.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                   "-show_entries", "stream=width,height", "-of", "json", str(f)],
                  capture_output=True, text=True, check=True).stdout
    st = _json.loads(out)["streams"][0]
    return st["width"], st["height"]


def _stitch(client, payload: dict):
    """POST a stitch, then poll its job URL like the browser does; returns
    the final GET response."""
    import time as _time
    r = client.post("/api/stitch-multi", json=payload)
    assert r.status_code == 202, r.data[:200]
    url = r.get_json()["url"]
    for _ in range(600):
        got = client.get(url)
        if got.status_code != 202:
            return got
        assert got.headers["Retry-After"]
        _time.sleep(0.1)
    raise AssertionError("stitch never finished")


@pytest.fixture
def host_user(app_env, monkeypatch, tmp_path):
    """A station operator (host role) who owns a different station."""
    import routes.archive as archive_routes

    # CACHE_PATH is bound at import (stitch jobs render under it); another
    # test module may have imported cache_store first with the production default.
    monkeypatch.setattr(archive_routes, "CACHE_PATH", tmp_path / "cache")
    c = app_env.test_client()
    with c.session_transaction() as sess:
        sess["user"] = "someone"
        sess["role"] = "host"
        sess["stations"] = ["otherstation"]
    return c


@pytest.mark.skipif(not _HAVE_FFMPEG, reason="needs ffmpeg")
@pytest.mark.parametrize("n,dims", [(1, (1280, 848)), (3, (2560, 1696)), (5, (1920, 848))])
def test_stitch_grid_size_and_stream(host_user, media_roots, tmp_path, n, dims):
    archive_root, _ = media_roots
    videos = []
    for i in range(n):
        name = f"{CAM}_{DATE}_2142{30 + i:02d}_color.mkv"
        _test_clip(archive_root / CAM / DATE / "meteors" / name)
        videos.append({"cam": CAM, "date": DATE, "filename": name, "offset": 1.0})
    r = _stitch(host_user, {
        "videos": videos, "layout": "grid", "master_start": -1, "master_end": 1})
    # A host who does not own the station used to get 403 here.
    assert r.status_code == 200, r.data[:200]
    assert r.mimetype == "video/mp4"
    assert r.data[4:8] == b"ftyp"
    # A plain file with a length (it used to be a chunked stream, which the
    # Cloudflare edge cut off at 14 MB on 2026-10-19).
    assert int(r.headers["Content-Length"]) == len(r.data)
    assert _probe_dims(r.data, tmp_path) == dims


@pytest.mark.skipif(not _HAVE_FFMPEG, reason="needs ffmpeg")
def test_stitch_keeps_the_whole_window_when_a_chunk_is_short(host_user, media_roots, tmp_path):
    """2026-10-19: a 10 s request came out 8.0 s, because each camera's cut
    stopped at its own chunk edge and the grid kept only the shortest. A
    meteor 0.5 s into one chunk and 2.0 s into another (3 s chunks) with a
    -2..+2 s window now gives 4 s, padded with black where a chunk has no
    frames."""
    archive_root, _ = media_roots
    videos = []
    for i, offset in enumerate((0.5, 2.0)):
        name = f"{CAM}_{DATE}_2142{40 + i:02d}_color.mkv"
        _test_clip(archive_root / CAM / DATE / "meteors" / name)
        videos.append({"cam": CAM, "date": DATE, "filename": name, "offset": offset})
    r = _stitch(host_user, {
        "videos": videos, "layout": "grid", "master_start": -2, "master_end": 2})
    assert r.status_code == 200, r.data[:200]
    f = tmp_path / "out.mp4"
    f.write_bytes(r.data)
    frames = _sp.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                      "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(f)],
                     capture_output=True, text=True, check=True).stdout.strip()
    assert int(frames) == 100  # 4 s at 25 fps


def test_stitch_bad_input_is_an_error_not_a_truncated_200(host_user, media_roots):
    archive_root, _ = media_roots
    name = f"{CAM}_{DATE}_214240_color.mkv"
    _mk(archive_root, f"{CAM}/{DATE}/meteors/{name}", b"not a video")
    r = _stitch(host_user, {
        "videos": [{"cam": CAM, "date": DATE, "filename": name, "offset": 1.0}]})
    assert r.status_code == 500
    assert r.get_json()["error"] == "ffmpeg failed"


def test_stitch_result_names_the_file_and_rejects_unknown_jobs(host_user, media_roots):
    assert host_user.get("/api/stitch-multi/" + "0" * 32).status_code == 404
    assert host_user.get("/api/stitch-multi/../../etc").status_code == 404


@pytest.fixture
def stitch_cmds(monkeypatch):
    """Record each stitch's ffmpeg command instead of running it."""
    import routes.archive as archive_routes

    cmds: list[list[str]] = []

    def _record(cmd, job, d):
        cmds.append(cmd)
        archive_routes._stitch_sem.release()

    monkeypatch.setattr(archive_routes, "_stitch_render", _record)
    return cmds


def _three_clips(archive_root: Path) -> list[dict]:
    videos = []
    for i in range(3):
        name = f"{CAM}_{DATE}_2142{50 + i:02d}_color.mkv"
        _mk(archive_root, f"{CAM}/{DATE}/meteors/{name}", b"mkv")
        videos.append({"cam": CAM, "date": DATE, "filename": name, "offset": 1.0})
    return videos


def _inputs(cmd: list[str]) -> list[str]:
    return [Path(cmd[i + 1]).name for i, a in enumerate(cmd) if a == "-i"]


@pytest.mark.parametrize("idx", [0, 2])
def test_stitch_focused_layout_puts_the_focused_clip_first(host_user, media_roots,
                                                           stitch_cmds, idx):
    """Focusing the FIRST clip used to give a grid: ``int(x or -1)`` turned
    index 0 into -1 ("no focus")."""
    archive_root, _ = media_roots
    videos = _three_clips(archive_root)
    r = host_user.post("/api/stitch-multi", json={
        "videos": videos, "layout": "focused", "focused_idx": idx,
        "master_start": -1, "master_end": 1})
    assert r.status_code == 202, r.data[:200]
    (cmd,) = stitch_cmds
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "vstack" in graph and "xstack" not in graph
    assert _inputs(cmd)[0] == videos[idx]["filename"]


@pytest.mark.parametrize("body", [{}, {"focused_idx": -1}, {"focused_idx": None}])
def test_stitch_focused_layout_without_a_focus_is_a_grid(host_user, media_roots,
                                                         stitch_cmds, body):
    archive_root, _ = media_roots
    r = host_user.post("/api/stitch-multi", json={
        "videos": _three_clips(archive_root), "layout": "focused", **body})
    assert r.status_code == 202, r.data[:200]
    graph = stitch_cmds[0][stitch_cmds[0].index("-filter_complex") + 1]
    assert "xstack" in graph


@pytest.mark.parametrize("bad", ["first", 1.5, [0], True])
def test_stitch_rejects_a_focus_that_is_not_an_index(host_user, media_roots,
                                                     stitch_cmds, bad):
    archive_root, _ = media_roots
    r = host_user.post("/api/stitch-multi", json={
        "videos": _three_clips(archive_root), "layout": "focused", "focused_idx": bad})
    assert r.status_code == 400
    assert stitch_cmds == []


def test_highlights_clip_plays_mp4_by_default(admin, media_roots, monkeypatch):
    """The highlights modal got raw MKV, which Safari and iOS cannot play."""
    import rovimen_dashboard as rd

    archive_root, _ = media_roots
    monkeypatch.setattr(rd, "ARCHIVE_PATH", archive_root)
    _mk(archive_root, f"{CAM}/{DATE}/meteors/{STEM}.mkv", b"matroska-bytes")
    _mk(archive_root, f"{CAM}/{DATE}/meteors/{STEM}.mp4", b"sibling-bytes")
    iso = f"{DATE[:4]}-{DATE[4:6]}-{DATE[6:]}"
    r = admin.get(f"/api/highlights/clip/{CAM}/{iso}/{STEM}.mkv")
    assert r.status_code == 200
    assert r.mimetype == "video/mp4"
    assert r.data == b"sibling-bytes"
    raw = admin.get(f"/api/highlights/clip/{CAM}/{iso}/{STEM}.mkv?format=mkv")
    assert raw.mimetype == "video/x-matroska"
    assert raw.data == b"matroska-bytes"


def test_video_route_honours_format_mp4(admin, media_roots):
    archive_root, _ = media_roots
    _mk(archive_root, f"{CAM}/{DATE}/meteors/{STEM}.mkv", b"matroska-bytes")
    _mk(archive_root, f"{CAM}/{DATE}/meteors/{STEM}.mp4", b"sibling-bytes")
    r = admin.get(f"/video/teststation/{CAM}/{DATE}/{STEM}.mkv?format=mp4")
    assert r.status_code == 200
    assert r.mimetype == "video/mp4"
    assert r.data == b"sibling-bytes"
    raw = admin.get(f"/video/teststation/{CAM}/{DATE}/{STEM}.mkv")
    assert raw.data == b"matroska-bytes"


def _stalled_mount(monkeypatch, *modules):
    """Every guarded Storage Box stat times out (the dawn-window symptom)."""
    for mod in modules:
        monkeypatch.setattr(mod, "_with_sshfs_timeout",
                            lambda fn, timeout=5.0, default=None: default)


def _index_listing(monkeypatch, *names):
    import rovimen_dashboard
    from cache_store import _NightIndex
    ni = _NightIndex(meteor_files=tuple(names), stack_files=frozenset(),
                     timelapse_files=())
    monkeypatch.setattr(rovimen_dashboard._archive_idx, "night_files",
                        lambda cam, date: ni)


def test_download_trusts_the_index_when_the_mount_stalls(admin, media_roots, monkeypatch):
    """A stalled stat used to read as "not archived" and send the card
    download to a station that had already pruned the chunk."""
    import routes.archive as archive_routes

    archive_root, _ = media_roots
    _mk(archive_root, f"{CAM}/{DATE}/meteors/{STEM}.mkv", b"matroska-bytes")
    _stalled_mount(monkeypatch, archive_routes)
    _index_listing(monkeypatch, f"{STEM}.mkv")
    r = admin.get(f"/download/teststation/{CAM}/{DATE}/{STEM}.mkv?format=mkv")
    assert r.status_code == 200
    assert r.data == b"matroska-bytes"
    assert "attachment" in r.headers.get("Content-Disposition", "")


def test_download_gives_the_mp4_copy_by_default(admin, media_roots):
    """Operators downloaded .mkv files most desktop players and phones cannot
    open; the stream-copied .mp4 carries the same frames."""
    archive_root, _ = media_roots
    _mk(archive_root, f"{CAM}/{DATE}/meteors/{STEM}.mkv", b"matroska-bytes")
    _mk(archive_root, f"{CAM}/{DATE}/meteors/{STEM}.mp4", b"sibling-bytes")
    r = admin.get(f"/download/teststation/{CAM}/{DATE}/{STEM}.mkv")
    assert r.status_code == 200
    assert r.mimetype == "video/mp4"
    assert r.data == b"sibling-bytes"
    assert r.headers["Content-Disposition"] == f'attachment; filename="{STEM}.mp4"'
    raw = admin.get(f"/download/teststation/{CAM}/{DATE}/{STEM}.mkv?format=mkv")
    assert raw.data == b"matroska-bytes"
    assert raw.headers["Content-Disposition"] == f'attachment; filename="{STEM}.mkv"'


def test_public_clip_trusts_the_index_when_the_mount_stalls(app_env, media_roots, monkeypatch):
    import public_api
    import rovimen_dashboard as rd

    archive_root, _ = media_roots
    monkeypatch.setattr(rd, "ARCHIVE_PATH", archive_root)
    _mk(archive_root, f"{CAM}/{DATE}/meteors/{STEM}.mkv", b"matroska-bytes")
    _stalled_mount(monkeypatch, public_api)
    _index_listing(monkeypatch, f"{STEM}.mkv")
    iso = f"{DATE[:4]}-{DATE[4:6]}-{DATE[6:]}"
    r = app_env.test_client().get(f"/media/v1/clip/{CAM}/{iso}/{STEM}.mkv")
    assert r.status_code == 200
    assert r.data == b"matroska-bytes"
