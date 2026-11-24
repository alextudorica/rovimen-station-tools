"""Station-side FF-thumbnail live push (detection_indexer + archive_upload).

The indexer renders a tiny BW WebP from the detection's FF maxpixel as soon as
the row is indexed and POSTs it to the VPS ffthumbs blob subdir; archive_upload
backfills any thumbnail the live push could not deliver. These cover naming,
the render pass, POST URL construction, retry-on-failure semantics, and the
backfill sweep.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits as afits

import archive_upload
import detection_indexer as di
import flags_manager

CAM = "RO000P"
NIGHT = "20260723"
FF = f"FF_{CAM}_{NIGHT}_201148_123_0000000.fits"
CHUNK = f"{CAM}_{NIGHT}_201145_color.mkv"
THUMB = f"{CAM}_{NIGHT}_201145.webp"


def _write_ff(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = np.zeros((32, 32), dtype=np.uint8)
    data[5, 5] = 255
    afits.HDUList(
        [afits.PrimaryHDU(), afits.ImageHDU(data=data, name="MAXPIXEL")]
    ).writeto(path, overwrite=True)


def _insert_row(con: sqlite3.Connection, **overrides) -> None:
    payload = {c: None for c in di.COLS}
    payload.update(
        cam=CAM, date=NIGHT, ff_file=FF, meteor_no=1,
        time_utc="2026-07-23T20:11:50", chunk_file=CHUNK,
    )
    payload.update(overrides)
    con.execute(di._INSERT_SQL, payload)
    con.commit()


@pytest.fixture()
def env(tmp_path):
    cap = tmp_path / "cap"
    rms = tmp_path / "rms"
    _write_ff(rms / "CapturedFiles" / f"{CAM}_{NIGHT}_195500_000000" / FF)
    (cap / CAM / NIGHT).mkdir(parents=True)
    cfg = {
        "videocapture_path": str(cap),
        "stations": {CAM: {"rms_data_path": str(rms)}},
    }
    con = sqlite3.connect(":memory:")
    con.executescript(di._SCHEMA)
    # Reset module-level queue state between tests.
    while not di._ffthumb_queue.empty():
        di._ffthumb_queue.get_nowait()
    di._ffthumb_queued.clear()
    yield {"cfg": cfg, "con": con, "cap": cap}
    con.close()


def _thumbs(env) -> Path:
    """Where the indexer renders this night's thumbnails (ffthumb_root)."""
    import rovimen_config
    return rovimen_config.ffthumb_root(env["cfg"]) / CAM / NIGHT


# ---------------------------------------------------------------------------
# Thumbnail naming
# ---------------------------------------------------------------------------

class TestFfthumbName:
    def test_chunk_stem_wins(self):
        assert di._ffthumb_name(CHUNK, FF) == THUMB

    def test_stitched_chunk_stem(self):
        stitched = f"{CAM}_{NIGHT}_002833_002853_color.mkv"
        assert di._ffthumb_name(stitched, FF) == f"{CAM}_{NIGHT}_002833_002853.webp"

    def test_ff_fallback_without_chunk(self):
        assert di._ffthumb_name(None, FF) == f"{CAM}_{NIGHT}_201148.webp"

    def test_radiant_only_row_has_no_thumb(self):
        assert di._ffthumb_name(None, "RAD_20260723_201150_000000") is None


# ---------------------------------------------------------------------------
# Render pass
# ---------------------------------------------------------------------------

class TestFfthumbPass:
    def test_renders_webp_named_after_chunk(self, env):
        _insert_row(env["con"])
        rendered = di._ffthumb_pass(env["con"], env["cfg"])
        assert rendered == 1
        thumb = _thumbs(env) / THUMB
        head = thumb.read_bytes()[:12]
        assert head[:4] == b"RIFF" and head[8:12] == b"WEBP"

    def test_enqueues_for_upload(self, env):
        _insert_row(env["con"])
        di._ffthumb_pass(env["con"], env["cfg"])
        assert di._ffthumb_queue.qsize() == 1
        path, cam, night = di._ffthumb_queue.get_nowait()
        assert (path.name, cam, night) == (THUMB, CAM, NIGHT)

    def test_second_pass_does_not_rerender_but_reenqueues(self, env):
        _insert_row(env["con"])
        di._ffthumb_pass(env["con"], env["cfg"])
        # Simulate the worker finishing with a FAILED post: item drained,
        # dedup key released, uploaded flag NOT set.
        di._ffthumb_queue.get_nowait()
        di._ffthumb_queued.clear()
        assert di._ffthumb_pass(env["con"], env["cfg"]) == 0  # no re-render
        assert di._ffthumb_queue.qsize() == 1                 # but re-enqueued

    def test_uploaded_thumb_not_reenqueued(self, env):
        _insert_row(env["con"])
        di._ffthumb_pass(env["con"], env["cfg"])
        di._ffthumb_queue.get_nowait()
        di._ffthumb_queued.clear()
        flags_manager.mark_ffthumb_uploaded(CAM, NIGHT, THUMB, env["cfg"])
        di._ffthumb_pass(env["con"], env["cfg"])
        assert di._ffthumb_queue.qsize() == 0

    def test_uploaded_thumb_deleted_by_the_janitor_is_not_rerendered(self, env):
        # The janitor deletes local thumbnails after stacks_days (7) while the
        # pass looks back HOT_DAYS (14): an uploaded one must stay gone, or
        # nights 8-14 days old are rendered again every poll.
        _insert_row(env["con"])
        assert di._ffthumb_pass(env["con"], env["cfg"]) == 1
        di._ffthumb_queue.get_nowait()
        di._ffthumb_queued.clear()
        flags_manager.mark_ffthumb_uploaded(CAM, NIGHT, THUMB, env["cfg"])
        (_thumbs(env) / THUMB).unlink()
        assert di._ffthumb_pass(env["con"], env["cfg"]) == 0
        assert not (_thumbs(env) / THUMB).exists()
        assert di._ffthumb_queue.qsize() == 0

    def test_queued_item_not_duplicated(self, env):
        _insert_row(env["con"])
        di._ffthumb_pass(env["con"], env["cfg"])
        di._ffthumb_pass(env["con"], env["cfg"])
        assert di._ffthumb_queue.qsize() == 1

    def test_ff_fallback_name_without_chunk(self, env):
        _insert_row(env["con"], chunk_file=None)
        di._ffthumb_pass(env["con"], env["cfg"])
        assert (_thumbs(env) / f"{CAM}_{NIGHT}_201148.webp").exists()

    def test_radiant_only_rows_skipped(self, env):
        _insert_row(env["con"], ff_file="RAD_20260723_201150_000000",
                    chunk_file=None)
        assert di._ffthumb_pass(env["con"], env["cfg"]) == 0
        assert not _thumbs(env).exists()

    def test_missing_ff_skipped_without_error(self, env):
        _insert_row(env["con"], ff_file=f"FF_{CAM}_{NIGHT}_223344_000_0000000.fits",
                    chunk_file=None)
        assert di._ffthumb_pass(env["con"], env["cfg"]) == 0

    def test_since_date_filters_old_nights(self, env):
        _insert_row(env["con"])
        assert di._ffthumb_pass(env["con"], env["cfg"], since_date="20270101") == 0


# ---------------------------------------------------------------------------
# POST
# ---------------------------------------------------------------------------

def _ingest_env(monkeypatch):
    monkeypatch.setenv("ROVIMEN_INGEST_BASE_URL", "https://vps.example")
    monkeypatch.setenv("ROVIMEN_HOST_KEY", "gmnro99")
    monkeypatch.setenv("ROVIMEN_INGEST_KEY", "SECRET")


class TestPostFfthumb:
    def test_posts_to_ffthumbs_blob_url_and_marks(self, env, monkeypatch):
        _ingest_env(monkeypatch)
        thumb = env["cap"] / CAM / NIGHT / "ffthumbs" / THUMB
        thumb.parent.mkdir(parents=True, exist_ok=True)
        thumb.write_bytes(b"webp-bytes")
        seen: dict = {}
        monkeypatch.setattr(di, "post_blob",
                            lambda url, key, data, timeout=180:
                            (seen.update(url=url, key=key, data=data), True)[1])
        assert di._post_ffthumb(thumb, CAM, NIGHT, env["cfg"]) is True
        assert seen["url"] == ("https://vps.example/api/ingest/v1/gmnro99"
                               f"/blob/{CAM}/{NIGHT}/ffthumbs/{THUMB}")
        assert seen["key"] == "SECRET"
        assert seen["data"] == b"webp-bytes"
        assert flags_manager.is_ffthumb_uploaded(CAM, NIGHT, THUMB, env["cfg"])

    def test_failed_post_leaves_flag_unset(self, env, monkeypatch):
        _ingest_env(monkeypatch)
        thumb = env["cap"] / CAM / NIGHT / "ffthumbs" / THUMB
        thumb.parent.mkdir(parents=True, exist_ok=True)
        thumb.write_bytes(b"x")
        monkeypatch.setattr(di, "post_blob", lambda *a, **k: False)
        assert di._post_ffthumb(thumb, CAM, NIGHT, env["cfg"]) is False
        assert not flags_manager.is_ffthumb_uploaded(CAM, NIGHT, THUMB, env["cfg"])

    def test_unconfigured_ingest_never_posts(self, env, monkeypatch):
        for var in ("ROVIMEN_INGEST_BASE_URL", "ROVIMEN_HOST_KEY",
                    "ROVIMEN_INGEST_KEY"):
            monkeypatch.delenv(var, raising=False)
        # Force resolution failure regardless of what the dev machine has in
        # ~/.config/rovimen by pointing the resolvers at empties.
        monkeypatch.setattr(di, "_resolve_base_url", lambda cfg: None)
        called: list[int] = []
        monkeypatch.setattr(di, "post_blob",
                            lambda *a, **k: (called.append(1), True)[1])
        thumb = env["cap"] / CAM / NIGHT / "ffthumbs" / THUMB
        thumb.parent.mkdir(parents=True, exist_ok=True)
        thumb.write_bytes(b"x")
        assert di._post_ffthumb(thumb, CAM, NIGHT, env["cfg"]) is False
        assert called == []

    def test_end_to_end_retry_next_pass(self, env, monkeypatch):
        """Failed POST -> flag unset -> next pass re-enqueues -> success marks."""
        _ingest_env(monkeypatch)
        _insert_row(env["con"])
        di._ffthumb_pass(env["con"], env["cfg"])

        def drain_once() -> bool:
            path, cam, night = di._ffthumb_queue.get_nowait()
            ok = di._post_ffthumb(path, cam, night, env["cfg"])
            di._ffthumb_queued.discard((cam, night, path.name))
            return ok

        monkeypatch.setattr(di, "post_blob", lambda *a, **k: False)
        assert drain_once() is False

        di._ffthumb_pass(env["con"], env["cfg"])
        assert di._ffthumb_queue.qsize() == 1
        monkeypatch.setattr(di, "post_blob", lambda *a, **k: True)
        assert drain_once() is True

        di._ffthumb_pass(env["con"], env["cfg"])
        assert di._ffthumb_queue.qsize() == 0


# ---------------------------------------------------------------------------
# archive_upload backfill
# ---------------------------------------------------------------------------

def _uploader(cap: Path) -> archive_upload._Uploader:
    up = archive_upload._Uploader({
        "archive": {"enabled": True},
        "videocapture_path": str(cap),
        "stations": {CAM: {}},
    })
    up.ingest_base_url = "https://vps.example"
    up.ingest_key = "SECRET"
    up.ingest_host_key = "gmnro99"
    return up


class TestArchiveUploadBackfill:
    def test_scan_finds_unuploaded_ffthumbs(self, tmp_path):
        cap = tmp_path / "cap"
        (cap / CAM / NIGHT / "ffthumbs").mkdir(parents=True)
        (cap / CAM / NIGHT / "ffthumbs" / THUMB).write_bytes(b"x")
        up = _uploader(cap)
        scan = up._scan_all()
        assert [(p.name, s, d) for p, s, d in scan.ffthumbs] == [(THUMB, CAM, NIGHT)]

    def test_scan_skips_uploaded_ffthumbs(self, tmp_path):
        cap = tmp_path / "cap"
        (cap / CAM / NIGHT / "ffthumbs").mkdir(parents=True)
        (cap / CAM / NIGHT / "ffthumbs" / THUMB).write_bytes(b"x")
        up = _uploader(cap)
        flags_manager.mark_ffthumb_uploaded(CAM, NIGHT, THUMB, up._cfg)
        assert up._scan_all().ffthumbs == []

    def test_cycle_posts_to_ffthumbs_subdir_and_marks(self, tmp_path, monkeypatch):
        cap = tmp_path / "cap"
        (cap / CAM / NIGHT / "ffthumbs").mkdir(parents=True)
        (cap / CAM / NIGHT / "ffthumbs" / THUMB).write_bytes(b"x")
        up = _uploader(cap)
        urls: list[str] = []
        monkeypatch.setattr(archive_upload, "post_blob_status",
                            lambda url, key, data, timeout=180:
                            (urls.append(url), 200)[1])
        count = up.cycle()
        thumb_urls = [u for u in urls if "/ffthumbs/" in u]
        assert thumb_urls == [
            f"https://vps.example/api/ingest/v1/gmnro99/blob/{CAM}/{NIGHT}/ffthumbs/{THUMB}"
        ]
        assert count >= 1
        assert flags_manager.is_ffthumb_uploaded(CAM, NIGHT, THUMB, up._cfg)

    def test_cycle_night_backfills_ffthumbs(self, tmp_path, monkeypatch):
        cap = tmp_path / "cap"
        (cap / CAM / NIGHT / "ffthumbs").mkdir(parents=True)
        (cap / CAM / NIGHT / "ffthumbs" / THUMB).write_bytes(b"x")
        up = _uploader(cap)
        urls: list[str] = []
        monkeypatch.setattr(archive_upload, "post_blob_status",
                            lambda url, key, data, timeout=180:
                            (urls.append(url), 200)[1])
        assert up.cycle_night(CAM, NIGHT) >= 1
        assert any(f"/ffthumbs/{THUMB}" in u for u in urls)
        assert flags_manager.is_ffthumb_uploaded(CAM, NIGHT, THUMB, up._cfg)


class TestThumbsWithoutColourDisk:
    """The BW FF thumbnails come from RMS's FF files and must exist whether or
    not colour video does (gmnro01 (Ghirdoveni), 2026-10-19: the colour disk
    vanished and every detection stayed imageless)."""

    def test_renders_when_the_capture_root_is_unwritable(self, env):
        env["cap"].chmod(0o500)
        try:
            _insert_row(env["con"], chunk_file=None)
            assert di._ffthumb_pass(env["con"], env["cfg"]) == 1
        finally:
            env["cap"].chmod(0o700)
        assert (_thumbs(env) / f"{CAM}_{NIGHT}_201148.webp").exists()

    def test_a_thumbnail_in_the_legacy_dir_is_reused(self, env):
        legacy = env["cap"] / CAM / NIGHT / "ffthumbs"
        legacy.mkdir(parents=True)
        (legacy / THUMB).write_bytes(b"old-render")
        _insert_row(env["con"])
        assert di._ffthumb_pass(env["con"], env["cfg"]) == 0
        path, _cam, _night = di._ffthumb_queue.get_nowait()
        assert path == legacy / THUMB

    def test_backfill_sweeps_the_root_without_any_capture_dir(self, env):
        import archive_upload
        thumbs = _thumbs(env)
        thumbs.mkdir(parents=True)
        (thumbs / THUMB).write_bytes(b"x")
        up = archive_upload._Uploader(env["cfg"])
        scan = up._scan_all()
        assert [(p.name, c, d) for p, c, d in scan.ffthumbs] == [(THUMB, CAM, NIGHT)]


# ---------------------------------------------------------------------------
# Every chunk gets the FF block at its middle (all-footage grid thumbnails)
# ---------------------------------------------------------------------------

from datetime import datetime, timedelta, timezone  # noqa: E402

CHUNK_START = datetime(2026, 7, 23, 20, 11, 45, tzinfo=timezone.utc)
CHUNK_END = CHUNK_START + timedelta(seconds=20)


@pytest.fixture()
def chunk_env(env, monkeypatch):
    (env["cap"] / CAM / NIGHT / CHUNK).write_bytes(b"\0")
    monkeypatch.setattr(di, "_ffthumb_env_ok", True)
    di._chunk_ffthumb_skip.clear()
    yield env
    di._chunk_ffthumb_skip.clear()


def _after_end(seconds: float) -> datetime:
    return CHUNK_END + timedelta(seconds=seconds)


class TestChunkFfthumbPass:
    def test_renders_every_chunk_under_its_stem(self, chunk_env):
        assert di._chunk_ffthumb_pass(chunk_env["cfg"], now=_after_end(60)) == 1
        assert (_thumbs(chunk_env) / THUMB).read_bytes()[8:12] == b"WEBP"
        path, cam, night = di._ffthumb_queue.get_nowait()
        assert (path.name, cam, night) == (THUMB, CAM, NIGHT)

    def test_waits_until_the_chunk_has_settled(self, chunk_env):
        assert di._chunk_ffthumb_pass(chunk_env["cfg"], now=_after_end(5)) == 0
        assert not (_thumbs(chunk_env) / THUMB).exists()

    def test_chunk_without_ff_is_given_up_after_the_grace(self, chunk_env):
        other = f"{CAM}_{NIGHT}_230000_color.mkv"
        (chunk_env["cap"] / CAM / NIGHT / other).write_bytes(b"\0")
        late = datetime(2026, 7, 23, 23, 0, 20, tzinfo=timezone.utc)
        di._chunk_ffthumb_pass(chunk_env["cfg"], now=late + timedelta(seconds=60))
        assert (CAM, NIGHT, f"{CAM}_{NIGHT}_230000.webp") not in di._chunk_ffthumb_skip
        di._chunk_ffthumb_pass(chunk_env["cfg"], now=late + timedelta(seconds=1000))
        assert (CAM, NIGHT, f"{CAM}_{NIGHT}_230000.webp") in di._chunk_ffthumb_skip

    def test_detection_thumb_is_not_rendered_twice(self, chunk_env):
        _insert_row(chunk_env["con"])
        assert di._ffthumb_pass(chunk_env["con"], chunk_env["cfg"]) == 1
        assert di._chunk_ffthumb_pass(chunk_env["cfg"], now=_after_end(60)) == 0

    def test_uploaded_thumb_is_not_queued(self, chunk_env):
        di._chunk_ffthumb_pass(chunk_env["cfg"], now=_after_end(60))
        di._ffthumb_queue.get_nowait()
        di._ffthumb_queued.clear()
        flags_manager.mark_ffthumb_uploaded(CAM, NIGHT, THUMB, chunk_env["cfg"])
        di._chunk_ffthumb_pass(chunk_env["cfg"], now=_after_end(120))
        assert di._ffthumb_queue.qsize() == 0

    def test_only_the_newest_two_nights(self, chunk_env):
        old_night = "20260720"
        old_chunk = f"{CAM}_{old_night}_201145_color.mkv"
        (chunk_env["cap"] / CAM / old_night).mkdir()
        (chunk_env["cap"] / CAM / old_night / old_chunk).write_bytes(b"\0")
        for n in ("20260721", "20260722"):
            (chunk_env["cap"] / CAM / n).mkdir()
        di._chunk_ffthumb_pass(chunk_env["cfg"], now=_after_end(60))
        import rovimen_config
        root = rovimen_config.ffthumb_root(chunk_env["cfg"]) / CAM
        assert not (root / old_night).exists()


class TestFfForChunk:
    def _ffs(self, *secs: float) -> list:
        base = datetime(2026, 7, 23, 20, 0, 0, tzinfo=timezone.utc)
        return [(base + timedelta(seconds=s), Path(f"ff{s}")) for s in secs]

    def test_block_covering_the_middle(self):
        ffs = self._ffs(0, 10.24, 20.48)
        start = datetime(2026, 7, 23, 20, 0, 5, tzinfo=timezone.utc)
        assert di._ff_for_chunk(ffs, start, start + timedelta(seconds=20)) == Path("ff10.24")

    def test_overlapping_block_when_the_middle_is_a_gap(self):
        ffs = self._ffs(0)
        start = datetime(2026, 7, 23, 20, 0, 5, tzinfo=timezone.utc)
        assert di._ff_for_chunk(ffs, start, start + timedelta(seconds=20)) == Path("ff0")

    def test_no_block_in_the_chunk(self):
        ffs = self._ffs(0, 100)
        start = datetime(2026, 7, 23, 20, 0, 30, tzinfo=timezone.utc)
        assert di._ff_for_chunk(ffs, start, start + timedelta(seconds=20)) is None
