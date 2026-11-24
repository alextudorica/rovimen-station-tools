"""Tests for the thumb-cache free-space backstop, dome-timelapse aging, and the
orphaned-tmp reaper added after the July 2026 disk-full incident.

The cache prune's 2-day age rule was already covered by behaviour elsewhere;
these guard the parts that let the volume run to 98% before: content that is
NOT under a date-dir (dome timelapses), and the free-space eviction that must
bound the cache regardless of age or layout.
"""

from __future__ import annotations

import os
import time
import types
from pathlib import Path

import cache_store


def _mk_date_dir(root: Path, *parts: str) -> Path:
    d = root.joinpath(*parts)
    d.mkdir(parents=True, exist_ok=True)
    (d / "x.webp").write_bytes(b"data")
    return d


def test_evict_removes_oldest_first_until_recovered(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_store, "THUMB_CACHE_DIR", tmp_path)
    monkeypatch.setattr(cache_store, "THUMB_CACHE_MIN_FREE_GB", 1.0)
    dirs = [
        _mk_date_dir(tmp_path, "gmnro03", "RO000H", "20260101"),
        _mk_date_dir(tmp_path, "gmnro03", "RO000H", "20260102"),
        _mk_date_dir(tmp_path, "gmnro03", "RO000H", "20260103"),
    ]
    # free stays below the 1 GB floor for the first two stat calls, then
    # recovers -- so exactly the two oldest date-dirs should be evicted.
    calls = {"n": 0}

    def fake_disk_usage(_p):
        calls["n"] += 1
        free = 2 * 1024**3 if calls["n"] > 2 else 0
        return types.SimpleNamespace(total=0, used=0, free=free)

    monkeypatch.setattr(cache_store.shutil, "disk_usage", fake_disk_usage)

    evicted = cache_store._evict_thumb_cache_to_free(list(dirs))
    assert evicted == 2
    assert not dirs[0].exists() and not dirs[1].exists()
    assert dirs[2].exists()  # newest kept


def test_evict_noop_when_above_floor(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_store, "THUMB_CACHE_DIR", tmp_path)
    monkeypatch.setattr(cache_store, "THUMB_CACHE_MIN_FREE_GB", 1.0)
    d = _mk_date_dir(tmp_path, "gmnro03", "RO000H", "20260101")
    monkeypatch.setattr(
        cache_store.shutil, "disk_usage",
        lambda _p: types.SimpleNamespace(total=0, used=0, free=99 * 1024**3),
    )
    assert cache_store._evict_thumb_cache_to_free([d]) == 0
    assert d.exists()


def test_evict_disabled_when_floor_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_store, "THUMB_CACHE_DIR", tmp_path)
    monkeypatch.setattr(cache_store, "THUMB_CACHE_MIN_FREE_GB", 0.0)
    d = _mk_date_dir(tmp_path, "g", "c", "20260101")

    def _boom(_p):
        raise AssertionError("disk_usage must not be consulted when disabled")

    monkeypatch.setattr(cache_store.shutil, "disk_usage", _boom)
    assert cache_store._evict_thumb_cache_to_free([d]) == 0
    assert d.exists()


def test_prune_dome_timelapses_ages_by_date(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_store, "THUMB_CACHE_DIR", tmp_path)
    dome = tmp_path / "gmnro03" / "sky_dome_timelapse"
    dome.mkdir(parents=True)
    old = dome / "20260101.mp4"
    old.write_bytes(b"x")
    new = dome / "20260301.mp4"
    new.write_bytes(b"x")
    removed = cache_store._prune_dome_timelapses("20260201")
    assert removed == 1
    assert not old.exists()
    assert new.exists()


def test_date_dirs_walk_finds_all_layouts(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_store, "THUMB_CACHE_DIR", tmp_path)
    _mk_date_dir(tmp_path, "gmnro03", "RO000H", "20260101")
    _mk_date_dir(tmp_path, "gmnro03", "rms_plots", "RO000H", "20260101")
    _mk_date_dir(tmp_path, "gmnro03", "sky_dome_archive", "20260101")
    # A non-date dir must not be picked up.
    (tmp_path / "gmnro03" / "sky_dome_timelapse").mkdir(parents=True)
    found = {p.relative_to(tmp_path).as_posix() for p in cache_store._thumb_cache_date_dirs()}
    assert found == {
        "gmnro03/RO000H/20260101",
        "gmnro03/rms_plots/RO000H/20260101",
        "gmnro03/sky_dome_archive/20260101",
    }


def test_reap_stale_tmp_matches_prefixes_and_age(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_store.tempfile, "gettempdir", lambda: str(tmp_path))
    now = time.time()
    old = now - 10_000  # older than the 7200 s default
    fresh = now

    def _mk(name: str, mtime: float, *, is_dir: bool) -> Path:
        p = tmp_path / name
        if is_dir:
            p.mkdir()
        else:
            p.write_bytes(b"x")
        os.utime(p, (mtime, mtime))
        return p

    reaped_dir = _mk("rovimen_dome_abc", old, is_dir=True)
    reaped_file = _mk("crop_xyz.mp4", old, is_dir=False)
    fresh_dir = _mk("rovimen_media_new", fresh, is_dir=True)   # too young
    other = _mk("someapp_scratch", old, is_dir=True)           # not our prefix
    lock = _mk("rovimen_compilation_1.lock", old, is_dir=False)  # never touch locks

    reaped = cache_store._reap_stale_tmp_once(max_age_s=7200)
    assert reaped == 2
    assert not reaped_dir.exists() and not reaped_file.exists()
    assert fresh_dir.exists() and other.exists() and lock.exists()


def test_branded_stack_nights_age_out_by_last_render(tmp_path, monkeypatch):
    """The branded-stack cache (routes/archive.py) is aged by render time, not
    by the night's date: every stack that needs a render predates the station
    overlay (nights before August 2026), so a night-date bound would empty it
    on every pass."""
    root = tmp_path / "branded_stacks"
    monkeypatch.setattr(cache_store, "BRANDED_STACK_DIR", root)
    now = time.time()
    stale = _mk_date_dir(root, "RO000H", "20260701")
    os.utime(stale / "x.webp", (now - 31 * 86400, now - 31 * 86400))
    recent = _mk_date_dir(root, "RO000H", "20260601")
    os.utime(recent / "x.webp", (now - 29 * 86400, now - 29 * 86400))
    mixed = _mk_date_dir(root, "RO000J", "20260501")
    os.utime(mixed / "x.webp", (now - 90 * 86400, now - 90 * 86400))
    (mixed / "y.webp").write_bytes(b"rendered today")

    assert cache_store._prune_branded_stacks(now) == 1
    assert not stale.exists()
    assert recent.exists() and mixed.exists()


def test_hourly_prune_covers_branded_stacks(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_store, "THUMB_CACHE_DIR", tmp_path / "thumb_cache")
    monkeypatch.setattr(cache_store, "FFTHUMB_DIR", tmp_path / "ffthumbs")
    monkeypatch.setattr(cache_store, "THUMB_ARCHIVE_DIR", None)
    monkeypatch.setattr(cache_store, "THUMB_CACHE_MIN_FREE_GB", 0.0)
    monkeypatch.setattr(cache_store, "BRANDED_STACK_DIR", tmp_path / "branded_stacks")
    (tmp_path / "thumb_cache").mkdir()
    stale = _mk_date_dir(tmp_path, "branded_stacks", "RO000H", "20260701")
    old = time.time() - 60 * 86400
    os.utime(stale / "x.webp", (old, old))

    cache_store._prune_thumb_cache_once()
    assert not stale.exists()

# ── one runner per host ──────────────────────────────────────────────────────
#
# Every gunicorn worker starts these loops, but the thumb cache, its cold tier
# and the temp dir are shared by the whole host. Three workers promoting the
# same expired night-dirs over SSHFS through one fixed ".part" name could
# interleave into one temp and publish a torn thumbnail.


class _StopLoop(BaseException):
    """Breaks a forever-loop out of its patched sleep."""


def _run_loop_ticks(monkeypatch, loop, gate, ticks: int) -> None:
    calls = {"sleep": 0}

    def _sleep(_s):
        calls["sleep"] += 1
        if calls["sleep"] > ticks:
            raise _StopLoop

    monkeypatch.setattr(cache_store.time, "sleep", _sleep)
    try:
        loop(gate)
    except _StopLoop:
        pass


def test_thumb_prune_runs_only_while_this_worker_holds_the_duty(monkeypatch):
    passes: list[int] = []
    monkeypatch.setattr(cache_store, "_prune_thumb_cache_once", lambda: passes.append(1))
    leader = iter([False, True, True])
    _run_loop_ticks(monkeypatch, cache_store._prune_thumb_cache, lambda: next(leader), ticks=3)
    assert len(passes) == 2


def test_tmp_reaper_runs_only_while_this_worker_holds_the_duty(monkeypatch):
    passes: list[int] = []
    monkeypatch.setattr(cache_store, "_reap_stale_tmp_once", lambda: passes.append(1) or 0)
    _run_loop_ticks(monkeypatch, cache_store._reap_stale_tmp, lambda: False, ticks=3)
    assert passes == []
    # The reaper sweeps before its first sleep: one sleep means two passes.
    _run_loop_ticks(monkeypatch, cache_store._reap_stale_tmp, lambda: True, ticks=1)
    assert len(passes) == 2


def test_promotion_temp_name_is_unique_per_process(tmp_path, monkeypatch):
    hot, cold = tmp_path / "hot", tmp_path / "cold"
    night = _mk_date_dir(hot, "gmnro03", "RO000H", "20260101")
    monkeypatch.setattr(cache_store, "THUMB_CACHE_DIR", hot)
    monkeypatch.setattr(cache_store, "THUMB_ARCHIVE_DIR", cold)
    temps: list[Path] = []
    real_copy2 = cache_store.shutil.copy2

    def _spy(src, dst, *a, **k):
        temps.append(Path(dst))
        return real_copy2(src, dst, *a, **k)

    monkeypatch.setattr(cache_store.shutil, "copy2", _spy)
    assert cache_store._promote_dir_to_archive(night) == 1
    assert (cold / "gmnro03" / "RO000H" / "20260101" / "x.webp").read_bytes() == b"data"
    assert len(temps) == 1
    assert str(os.getpid()) in temps[0].name and temps[0].name.endswith(".part")
    assert not temps[0].exists()
