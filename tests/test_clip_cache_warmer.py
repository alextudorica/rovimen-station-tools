"""Tests for the NVMe clip hot-cache: warmer logic and path mapping.

The warmer (clip_cache_warmer.py) is stdlib-only and injectable, so these
tests build a fake archive tree in tmp_path and stub the remux with a byte
copy — no ffmpeg, no SSHFS.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest

import clip_cache
from clip_cache_warmer import (
    copy_clip,
    discover_nights,
    evict_to_budget,
    iter_night_clips,
    prune_old_nights,
    warm,
)

OLD = time.time() - 3600  # comfortably past the min-age window


def _mk_clip(root: Path, cam: str, date: str, name: str,
             size: int = 1000, mtime: float = OLD) -> Path:
    p = root / cam / date / "meteors" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    os.utime(p, (mtime, mtime))
    return p


def _copy_remux(src: Path, dest: Path) -> None:
    shutil.copyfile(src, dest)


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    a = tmp_path / "archive"
    a.mkdir()
    return a


@pytest.fixture
def cache(tmp_path: Path) -> Path:
    c = tmp_path / "clips"
    c.mkdir()
    return c


# ── discovery ────────────────────────────────────────────────────────────────

def test_discover_nights_newest_first_across_cameras(archive: Path):
    _mk_clip(archive, "RO0017", "20260704", "a.mkv")
    _mk_clip(archive, "RO0017", "20260706", "b.mkv")
    _mk_clip(archive, "DE001B", "20260705", "c.mkv")
    _mk_clip(archive, "DE001B", "20260703", "d.mkv")
    assert discover_nights(archive, 3) == ["20260706", "20260705", "20260704"]


def test_discover_nights_ignores_junk_dirs(archive: Path):
    _mk_clip(archive, "RO0017", "20260706", "a.mkv")
    (archive / "lost+found").mkdir()
    (archive / "RO0017" / "notadate").mkdir()
    assert discover_nights(archive, 5) == ["20260706"]


def test_discover_nights_empty_on_missing_root(tmp_path: Path):
    assert discover_nights(tmp_path / "nope", 3) == []


def test_iter_clips_newest_night_first(archive: Path):
    _mk_clip(archive, "RO0017", "20260705", "old.mkv")
    _mk_clip(archive, "RO0017", "20260706", "new.mkv")
    _mk_clip(archive, "RO0017", "20260706", "new.txt")  # non-mkv ignored
    order = [(d, p.name) for _, d, p in
             iter_night_clips(archive, ["20260706", "20260705"])]
    assert order == [("20260706", "new.mkv"), ("20260705", "old.mkv")]


# ── warm pass ────────────────────────────────────────────────────────────────

def test_warm_remuxes_and_reruns_are_incremental(archive: Path, cache: Path):
    _mk_clip(archive, "RO0017", "20260706", "c1.mkv", size=100)
    stats = warm(archive, cache, nights=3, max_bytes=10**6,
                 remux_fn=_copy_remux)
    assert stats["warmed"] == 1
    assert (cache / "RO0017" / "20260706" / "c1.mp4").read_bytes() == b"x" * 100

    stats = warm(archive, cache, nights=3, max_bytes=10**6,
                 remux_fn=_copy_remux)
    assert stats["warmed"] == 0 and stats["already"] == 1


def test_warm_max_per_run_caps_and_resumes(archive: Path, cache: Path):
    # Five eligible clips, cap of 2 per run -> 2, 2, 1 across three ticks.
    for i in range(5):
        _mk_clip(archive, "RO0017", "20260706", f"c{i}.mkv", size=100)
    total = 0
    for expected in (2, 2, 1):
        stats = warm(archive, cache, nights=3, max_bytes=10**6,
                     max_per_run=2, remux_fn=_copy_remux)
        assert stats["warmed"] == expected
        total += stats["warmed"]
        if expected == 2:
            assert stats.get("hit_run_cap") == 1
    assert total == 5
    # Everything cached now: a further run does nothing.
    assert warm(archive, cache, nights=3, max_bytes=10**6,
                max_per_run=2, remux_fn=_copy_remux)["warmed"] == 0


def test_warm_pauses_between_remuxes(archive: Path, cache: Path, monkeypatch):
    _mk_clip(archive, "RO0017", "20260706", "a.mkv", size=50)
    _mk_clip(archive, "RO0017", "20260706", "b.mkv", size=50)
    calls: list[float] = []
    monkeypatch.setattr("clip_cache_warmer.time.sleep", lambda s: calls.append(s))
    warm(archive, cache, nights=3, max_bytes=10**6,
         pause_s=1.5, remux_fn=_copy_remux)
    # Paused after each of the two remuxes (no cap hit to short-circuit).
    assert calls == [1.5, 1.5]


def test_warm_skips_still_uploading_clips(archive: Path, cache: Path):
    _mk_clip(archive, "RO0017", "20260706", "fresh.mkv", mtime=time.time())
    stats = warm(archive, cache, nights=3, max_bytes=10**6,
                 remux_fn=_copy_remux)
    assert stats["warmed"] == 0 and stats["skipped_young"] == 1


def test_warm_budget_degrades_oldest_nights_first(archive: Path, cache: Path):
    # Budget fits the newest night (2 clips) but not the older one.
    _mk_clip(archive, "RO0017", "20260706", "n1.mkv", size=400)
    _mk_clip(archive, "RO0017", "20260706", "n2.mkv", size=400)
    _mk_clip(archive, "RO0017", "20260705", "o1.mkv", size=400)
    stats = warm(archive, cache, nights=3, max_bytes=1000,
                 remux_fn=_copy_remux)
    assert stats["warmed"] == 2 and stats["skipped_budget"] == 1
    assert (cache / "RO0017" / "20260706" / "n1.mp4").exists()
    assert (cache / "RO0017" / "20260706" / "n2.mp4").exists()
    assert not (cache / "RO0017" / "20260705" / "o1.mp4").exists()


def test_warm_prunes_nights_older_than_window(archive: Path, cache: Path):
    _mk_clip(archive, "RO0017", "20260706", "a.mkv")
    stale = cache / "RO0017" / "20260601"
    stale.mkdir(parents=True)
    (stale / "gone.mp4").write_bytes(b"y" * 50)
    stats = warm(archive, cache, nights=3, max_bytes=10**6,
                 remux_fn=_copy_remux)
    assert stats["pruned_dirs"] == 1
    assert not stale.exists()


def test_warm_aborts_without_pruning_when_archive_empty(
    archive: Path, cache: Path
):
    keep = cache / "RO0017" / "20260706"
    keep.mkdir(parents=True)
    (keep / "keep.mp4").write_bytes(b"z" * 10)
    stats = warm(archive, cache, nights=3, max_bytes=10**6,
                 remux_fn=_copy_remux)
    assert stats == {"aborted": 1}
    assert (keep / "keep.mp4").exists()


def test_warm_replaces_stale_mp4_when_source_newer(archive: Path, cache: Path):
    src = _mk_clip(archive, "RO0017", "20260706", "c1.mkv", size=100)
    dest = cache / "RO0017" / "20260706" / "c1.mp4"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"old")
    os.utime(dest, (OLD - 100, OLD - 100))  # older than the source
    stats = warm(archive, cache, nights=3, max_bytes=10**6,
                 remux_fn=_copy_remux)
    assert stats["warmed"] == 1
    assert dest.stat().st_size == src.stat().st_size


def test_warm_survives_remux_failure(archive: Path, cache: Path):
    _mk_clip(archive, "RO0017", "20260706", "bad.mkv")
    _mk_clip(archive, "RO0017", "20260706", "good.mkv")

    def flaky(src: Path, dest: Path) -> None:
        if "bad" in src.name:
            raise RuntimeError("ffmpeg exploded")
        _copy_remux(src, dest)

    stats = warm(archive, cache, nights=3, max_bytes=10**6, remux_fn=flaky)
    assert stats["warmed"] == 1 and stats["failed"] == 1


# ── prune / evict primitives ─────────────────────────────────────────────────

def test_prune_keeps_wanted_nights(cache: Path):
    for date in ("20260706", "20260601"):
        d = cache / "RO0017" / date
        d.mkdir(parents=True)
        (d / "c.mp4").write_bytes(b"a" * 10)
    dirs, size = prune_old_nights(cache, {"20260706"})
    assert dirs == 1 and size == 10
    assert (cache / "RO0017" / "20260706").exists()
    assert not (cache / "RO0017" / "20260601").exists()


def test_evict_drops_oldest_until_under_budget(cache: Path):
    for date, size in (("20260704", 100), ("20260705", 100), ("20260706", 100)):
        d = cache / "RO0017" / date
        d.mkdir(parents=True)
        (d / "c.mp4").write_bytes(b"a" * size)
    assert evict_to_budget(cache, 250) == 1
    assert not (cache / "RO0017" / "20260704").exists()
    assert (cache / "RO0017" / "20260706").exists()


# ── mp4 sibling copy path ────────────────────────────────────────────────────

def test_iter_clips_prefers_mp4_sibling(archive: Path):
    _mk_clip(archive, "RO0017", "20260706", "a_color.mkv")
    _mk_clip(archive, "RO0017", "20260706", "a_color.mp4")
    _mk_clip(archive, "RO0017", "20260706", "b_color.mkv")
    got = [p.name for _, _, p in iter_night_clips(archive, ["20260706"])]
    assert got == ["a_color.mp4", "b_color.mkv"]


def test_warm_copies_sibling_and_remuxes_the_rest(archive: Path, cache: Path):
    _mk_clip(archive, "RO0017", "20260706", "a_color.mkv", size=100)
    _mk_clip(archive, "RO0017", "20260706", "a_color.mp4", size=90)
    _mk_clip(archive, "RO0017", "20260706", "b_color.mkv", size=100)
    remuxed: list[str] = []

    def fake_remux(src: Path, dest: Path) -> None:
        remuxed.append(src.name)
        shutil.copyfile(src, dest)

    stats = warm(archive, cache, nights=3, max_bytes=10**9,
                 remux_fn=fake_remux)
    assert stats["warmed"] == 2
    assert stats["copied"] == 1
    assert remuxed == ["b_color.mkv"]  # sibling never hits ffmpeg
    a = cache / "RO0017" / "20260706" / "a_color.mp4"
    assert a.read_bytes() == b"x" * 90  # sibling bytes, not a remux product


def test_copy_clip_atomic_and_cleans_tmp_on_failure(tmp_path: Path):
    src = tmp_path / "src.mp4"
    src.write_bytes(b"y" * 10)
    dest = tmp_path / "out" / "dest.mp4"
    dest.parent.mkdir()
    copy_clip(src, dest)
    assert dest.read_bytes() == b"y" * 10
    with pytest.raises(Exception):
        copy_clip(tmp_path / "missing.mp4", dest)
    assert not list(dest.parent.glob("*.tmp"))


# ── clip_cache path mapping (read side) ──────────────────────────────────────

def test_hot_path_disabled_without_env():
    assert clip_cache.CLIP_CACHE_DIR is None  # env unset in tests
    assert clip_cache.hot_mp4_path("RO0017", "20260706", "a_color.mkv") is None


def test_hot_path_mapping_and_validation(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(clip_cache, "CLIP_CACHE_DIR", tmp_path)
    p = clip_cache.hot_mp4_path(
        "RO0017", "20260706", "RO0017_20260706_194842_color.mkv"
    )
    assert p == tmp_path / "RO0017" / "20260706" / (
        "RO0017_20260706_194842_color.mp4"
    )
    # .mkv and its .mp4 sibling map to the SAME cache entry (stem-keyed).
    assert clip_cache.hot_mp4_path(
        "RO0017", "20260706", "RO0017_20260706_194842_color.mp4"
    ) == p
    bad = [
        ("../..", "20260706", "a.mkv"),
        ("RO0017", "2026-07-06", "a.mkv"),
        ("RO0017", "20260706", "a/b.mkv"),
        ("RO0017", "20260706", "a..b/../c.mkv"),
        ("RO0017", "20260706", "a.webm"),
        ("ro0017", "20260706", "a.mkv"),
    ]
    for cam, date, name in bad:
        assert clip_cache.hot_mp4_path(cam, date, name) is None
