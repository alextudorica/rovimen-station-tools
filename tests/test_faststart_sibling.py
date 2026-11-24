"""The VPS derives a meteor clip's faststart .mp4 sibling at ingest
(dashboard/faststart_sibling.py, wired in as the blob spool's before_flush
hook), so stations upload each clip once, as .mkv.

The archive must end up with the same two files the stations used to send, so
every reader resolves the clip as before: the media pointer, the archive
listing, the .mp4-first serve path, the public /media/v1 sibling lookup, the
storage-box janitor, the clip requester and the hot clip cache.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from flask import Flask

import blob_spool
import clip_cache
import clip_requester
import faststart_sibling
import ingest_api
import route_helpers
import station_keys
import station_state as ss
import storagebox_janitor

STATION, CAM, NIGHT = "gmn0002", "RO000M", "20260709"
CLIP = f"{CAM}_{NIGHT}_214720_color.mkv"
DETAILS = f"{CAM}_{NIGHT}_214720_color.details.mkv"


@pytest.fixture(autouse=True)
def _require_keys(monkeypatch):
    monkeypatch.setenv("ROVIMEN_STATION_KEYS_REQUIRED", "1")
    station_keys._invalidate_cache()
    yield
    station_keys._invalidate_cache()


@pytest.fixture
def fake_remux(monkeypatch) -> list[Path]:
    """ffmpeg stand-in: the 'mp4' is the source bytes behind a marker."""
    calls: list[Path] = []

    def remux(source: Path, dest: Path) -> None:
        calls.append(source)
        dest.write_bytes(b"MP4:" + source.read_bytes())

    monkeypatch.setattr(faststart_sibling, "remux", remux)
    return calls


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(clip_cache, "CLIP_CACHE_DIR", tmp_path / "hotclips")
    keys_path = tmp_path / "station_keys.yaml"
    _, secret = station_keys.add_key(STATION, path=keys_path)
    station_keys._invalidate_cache()
    app = Flask(__name__)
    app.testing = True
    ingest_api.register_ingest_routes(
        app,
        cache_set_status=lambda *a, **k: None,
        cache_set_vitals=lambda *a, **k: None,
        cache_set_live_thumb=lambda *a, **k: None,
        known_stations=lambda: {STATION},
        detections_db_path=tmp_path / "detections.db",
        station_state_path=tmp_path / "state.db",
        station_keys_path=keys_path,
        archive_media_path=tmp_path / "archive",
        blob_spool_dir=tmp_path / "spool",
        limiter=None,
    )
    return {
        "client": app.test_client(), "app": app, "secret": secret,
        "archive": tmp_path / "archive", "state_path": tmp_path / "state.db",
        "hot": tmp_path / "hotclips",
    }


def _post(env, name: str, body: bytes) -> None:
    r = env["client"].post(
        f"/api/ingest/v1/{STATION}/blob/{CAM}/{NIGHT}/meteors/{name}",
        data=body, headers={"X-Station-Key": env["secret"]},
        content_type="application/octet-stream")
    assert r.status_code == 200, r.get_data(as_text=True)


def _drain(env) -> None:
    spooler = env["app"]._blob_spooler
    for _ in range(3):  # the derived sibling flushes on the next pass
        spooler.flush_once()


def _meteors(env) -> Path:
    return env["archive"] / CAM / NIGHT / "meteors"


# ---------------------------------------------------------------------------
# Ingest derives the sibling
# ---------------------------------------------------------------------------

def test_mkv_only_upload_archives_both_files(env, fake_remux):
    _post(env, CLIP, b"clip-bytes")
    _drain(env)
    d = _meteors(env)
    assert (d / CLIP).read_bytes() == b"clip-bytes"
    assert (d / CLIP.replace(".mkv", ".mp4")).read_bytes() == b"MP4:clip-bytes"
    assert len(fake_remux) == 1
    spool = env["app"]._blob_spooler.spool_root
    assert not [p for p in spool.rglob("*") if p.is_file()]  # spool drained


def test_details_variant_gets_its_details_mp4(env, fake_remux):
    _post(env, DETAILS, b"details")
    _drain(env)
    assert (_meteors(env) / DETAILS.replace(".mkv", ".mp4")).read_bytes() == b"MP4:details"


def test_derived_sibling_lands_in_the_hot_clip_cache(env, fake_remux):
    _post(env, CLIP, b"clip-bytes")
    env["app"]._blob_spooler.flush_once()
    hot = env["hot"] / CAM / NIGHT / CLIP.replace(".mkv", ".mp4")
    assert hot.read_bytes() == b"MP4:clip-bytes"
    assert not list(hot.parent.glob(".*"))


def test_non_clip_blobs_are_not_remuxed(env, fake_remux):
    for rel, body in (("timelapse/x_timelapse.mp4", b"t"), ("stacks/x_stack.webp", b"s")):
        r = env["client"].post(f"/api/ingest/v1/{STATION}/blob/{CAM}/{NIGHT}/{rel}",
                               data=body, headers={"X-Station-Key": env["secret"]})
        assert r.status_code == 200
    _drain(env)
    assert fake_remux == []


def test_failed_remux_still_archives_the_mkv(env, monkeypatch):
    def boom(source: Path, dest: Path) -> None:
        raise RuntimeError("ffmpeg exited 1")

    monkeypatch.setattr(faststart_sibling, "remux", boom)
    _post(env, CLIP, b"clip-bytes")
    _drain(env)
    assert sorted(p.name for p in _meteors(env).iterdir()) == [CLIP]


def test_a_station_on_the_old_bundle_keeps_its_own_mp4(env, fake_remux):
    """An old bundle still POSTs the .mp4 right after the .mkv. Both are the
    same bitstream in production, so whichever lands last is fine; the
    archive ends with the two files and the spool drains."""
    _post(env, CLIP, b"clip-bytes")
    _post(env, CLIP.replace(".mkv", ".mp4"), b"station-mp4")
    _drain(env)
    assert sorted(p.name for p in _meteors(env).iterdir()) == \
        sorted([CLIP, CLIP.replace(".mkv", ".mp4")])
    spool = env["app"]._blob_spooler.spool_root
    assert not [p for p in spool.rglob("*") if p.is_file()]


def test_a_sibling_waiting_in_the_spool_is_not_remuxed(tmp_path, fake_remux):
    spool = tmp_path / "meteors"
    spool.mkdir()
    (spool / CLIP.replace(".mkv", ".mp4")).write_bytes(b"station-mp4")
    claimed = spool / f"{CLIP}.flushing-1-2"
    claimed.write_bytes(b"clip")
    assert faststart_sibling.derive(Path(CAM, NIGHT, "meteors", CLIP), claimed, spool) is None
    assert fake_remux == []


def test_flush_retry_after_a_box_failure_does_not_lose_the_sibling(tmp_path, fake_remux):
    """A failed archive copy returns the claim to the spool and the retry
    still archives both files.

    How many remuxes that takes depends on the flush lanes' order: if the
    sibling is still in the spool when the clip is retried it is reused (one
    remux); if it has already been archived, derive() writes it again, an
    identical overwrite (two). CI hit the second order after #1439."""
    archive, spool = tmp_path / "archive", tmp_path / "spool"
    attempts = {"n": 0}

    def flaky_guard(fn, timeout, default):
        attempts["n"] += 1
        return default if attempts["n"] == 1 else fn()

    bs = blob_spool.BlobSpool(
        archive, spool_root=spool, sshfs_guard=flaky_guard,
        before_flush=lambda rel, claimed: faststart_sibling.derive(rel, claimed, claimed.parent))
    src = spool / CAM / NIGHT / "meteors" / CLIP
    src.parent.mkdir(parents=True)
    src.write_bytes(b"clip")
    for _ in range(3):
        bs.flush_once()
    assert (archive / CAM / NIGHT / "meteors" / CLIP).read_bytes() == b"clip"
    assert (archive / CAM / NIGHT / "meteors" / CLIP.replace(".mkv", ".mp4")).exists()
    assert 1 <= len(fake_remux) <= 2


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_real_remux_is_a_faststart_mp4(tmp_path):
    mkv = tmp_path / "c_color.mkv"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "testsrc=size=64x48:rate=5:duration=1", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", str(mkv)], check=True, timeout=60)
    mp4 = tmp_path / "c_color.mp4"
    faststart_sibling.remux(mkv, mp4)
    data = mp4.read_bytes()
    assert data.index(b"moov") < data.index(b"mdat")  # faststart: moov first
    assert not list(tmp_path.glob(".*"))


# ---------------------------------------------------------------------------
# Every reader resolves the clip as before
# ---------------------------------------------------------------------------

def _ingested(env) -> tuple[Path, Path]:
    _post(env, CLIP, b"clip-bytes")
    _drain(env)
    d = _meteors(env)
    return d / CLIP, d / CLIP.replace(".mkv", ".mp4")


def test_media_pointer_names_the_mkv(env, fake_remux):
    _ingested(env)
    ptrs = ss.get_media_pointers(STATION, NIGHT, path=env["state_path"])
    clips = [p["filename"] for p in ptrs if p["kind"] == "clip"]
    assert clips == [CLIP]


def test_archive_listing_shows_one_entry_under_the_mkv_name(env, fake_remux):
    mkv, _mp4 = _ingested(env)
    names = sorted(p.name for p in mkv.parent.iterdir())
    assert route_helpers.listed_clip_names(names) == [CLIP]
    assert route_helpers.stack_name_for_clip(CLIP.replace(".mkv", ".mp4")) == \
        CLIP.replace("_color.mkv", "_stack.webp")


def test_archive_serve_path_finds_the_mp4_sibling(env, fake_remux, monkeypatch):
    import routes.archive as ra

    _mkv, mp4 = _ingested(env)
    monkeypatch.setattr(ra, "ARCHIVE_PATH", env["archive"])
    monkeypatch.setattr(ra, "_DEPS", None)
    stem = CLIP[: -len(".mkv")]
    assert ra._archive_clip_path(CAM, NIGHT, stem + ".mp4") == mp4
    assert ra._archive_clip_path(CAM, NIGHT, CLIP) == _mkv


def test_public_media_route_finds_the_mp4_sibling(env, fake_remux, monkeypatch):
    import public_api
    import rovimen_dashboard as rd

    _mkv, mp4 = _ingested(env)
    monkeypatch.setattr(rd, "ARCHIVE_PATH", env["archive"])
    iso = f"{NIGHT[:4]}-{NIGHT[4:6]}-{NIGHT[6:]}"
    found = public_api._resolve_media_path(CAM, iso, mp4.name, "meteors")
    assert found is not None and found.resolve() == mp4.resolve()


def test_janitor_sees_both_videos_of_the_clip(env, fake_remux):
    mkv, mp4 = _ingested(env)
    present = {p.name for p in mkv.parent.iterdir()}
    paths, size = storagebox_janitor._existing_videos(mkv.parent, CLIP, present)
    assert sorted(paths) == sorted([mkv, mp4])
    assert size == mkv.stat().st_size + mp4.stat().st_size


def test_clip_requester_sees_the_clip_as_archived(env, fake_remux):
    _ingested(env)
    names = clip_requester.Archive(env["archive"]).meteor_names(CAM, NIGHT)
    assert names is not None and CLIP in names


def test_hot_cache_serves_the_clip_under_either_name(env, fake_remux):
    _ingested(env)
    for name in (CLIP, CLIP.replace(".mkv", ".mp4")):
        hot = clip_cache.hot_mp4_path(CAM, NIGHT, name)
        assert hot is not None and hot.read_bytes() == b"MP4:clip-bytes"


def test_a_night_archived_with_both_files_still_lists_once(tmp_path):
    """Nights uploaded before this change carry both station-sent files."""
    names = [CLIP, CLIP.replace(".mkv", ".mp4"), DETAILS, DETAILS.replace(".mkv", ".mp4")]
    assert route_helpers.listed_clip_names(names) == [CLIP]


# ---------------------------------------------------------------------------
# The dashboard's other MKV-to-MP4 remuxes run through faststart_sibling.remux
# with their own timeouts and temp paths
# ---------------------------------------------------------------------------

def _real_mkv(tmp_path: Path) -> Path:
    mkv = tmp_path / "c_color.mkv"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "testsrc=size=64x48:rate=5:duration=1", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", str(mkv)], check=True, timeout=60)
    return mkv


@pytest.mark.timeout(300)
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
@pytest.mark.parametrize("caller", ["archive", "public_api", "warmer"])
def test_on_demand_remuxes_write_a_faststart_mp4_and_no_temp(tmp_path, caller):
    import clip_cache_warmer
    import public_api
    import routes.archive as archive_routes

    fn = {"archive": archive_routes._remux_to_mp4,
          "public_api": public_api._remux_mkv_to_mp4,
          "warmer": clip_cache_warmer.remux}[caller]
    mkv = _real_mkv(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    mp4 = out_dir / "c_color.mp4"
    fn(mkv, mp4)
    data = mp4.read_bytes()
    assert data.index(b"moov") < data.index(b"mdat")
    assert [p.name for p in out_dir.iterdir()] == ["c_color.mp4"]


@pytest.mark.parametrize("caller,timeout,tmp_name", [
    ("archive", 30, "out.mp4"),          # in place: the caller's own temp
    ("public_api", 30, "out.mp4"),
    ("warmer", 180.0, "out.mp4.tmp"),
    ("derive", 120, ".out.mp4.remux-"),
])
def test_each_caller_keeps_its_timeout_and_temp_name(tmp_path, monkeypatch, caller,
                                                     timeout, tmp_name):
    import clip_cache_warmer
    import public_api
    import routes.archive as archive_routes

    seen: dict = {}

    def fake_run(cmd, **kw):
        seen["timeout"] = kw["timeout"]
        seen["out"] = Path(cmd[-1]).name
        Path(cmd[-1]).write_bytes(b"mp4")
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(faststart_sibling.subprocess, "run", fake_run)
    fn = {"archive": archive_routes._remux_to_mp4,
          "public_api": public_api._remux_mkv_to_mp4,
          "warmer": clip_cache_warmer.remux,
          "derive": faststart_sibling.remux}[caller]
    dest = tmp_path / "out.mp4"
    fn(tmp_path / "in.mkv", dest)
    assert seen["timeout"] == timeout
    assert seen["out"].startswith(tmp_name)
    assert dest.read_bytes() == b"mp4"


@pytest.mark.parametrize("caller", ["archive", "public_api"])
def test_in_place_failure_removes_the_callers_temp(tmp_path, monkeypatch, caller):
    import public_api
    import routes.archive as archive_routes

    def fail(cmd, **kw):
        Path(cmd[-1]).write_bytes(b"partial")
        return subprocess.CompletedProcess(cmd, 1, b"", b"bad input")

    monkeypatch.setattr(faststart_sibling.subprocess, "run", fail)
    fn = {"archive": archive_routes._remux_to_mp4,
          "public_api": public_api._remux_mkv_to_mp4}[caller]
    dest = tmp_path / "remux_x.mp4"
    dest.write_bytes(b"")  # mkstemp'd by the caller
    with pytest.raises(RuntimeError, match="ffmpeg exited 1: bad input"):
        fn(tmp_path / "in.mkv", dest)
    assert not dest.exists()
