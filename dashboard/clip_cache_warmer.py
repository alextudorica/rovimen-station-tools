#!/usr/bin/env python3
"""Warm the NVMe clip hot-cache from the storage-box archive.

Fills the last N nights of detection clips (``<CAM>/<date>/meteors/*``
on the storage box) into seekable faststart MP4s under
``ROVIMEN_CLIP_CACHE_DIR`` so the dashboard and public media surface can
serve them instantly via nginx X-Accel instead of remuxing over SSHFS per
request (see ``clip_cache.py`` for the read side). Push stations upload a
ready-made faststart ``.mp4`` sibling next to each ``.mkv``; when present
it is plain-copied (no ffmpeg) — only sibling-less clips pay for a remux.

Runs standalone from a systemd timer (``rovimen-clip-warmer.timer``) — never
inside the gunicorn worker. Stdlib-only, plus the stdlib-only
``faststart_sibling.remux`` it shares with the dashboard.

Budget model (graceful degradation):
  - hard prune: cache night-dirs outside the newest N archive nights are
    deleted first;
  - byte budget: nights are warmed newest-first; a clip that would push the
    cache over ``ROVIMEN_CLIP_CACHE_MAX_BYTES`` is skipped (and counted).
    On a shower peak the newest night stays fully hot and older nights
    degrade to the per-request slow path — never the other way around.

Safety: if the archive mount yields no nights at all (SSHFS down), the run
aborts WITHOUT pruning — an unmounted box must not empty the cache.
"""

from __future__ import annotations

import argparse
import fcntl
import logging
import os
import re
import shutil
import sys
import time
from pathlib import Path

import faststart_sibling

logger = logging.getLogger("clip_cache_warmer")

_CAMERA_RE = re.compile(r"^[A-Z0-9]{4,12}$")
_DATE_RE = re.compile(r"^\d{8}$")

DEFAULT_NIGHTS = int(os.environ.get("ROVIMEN_CLIP_CACHE_NIGHTS", "3"))
DEFAULT_MAX_BYTES = int(
    os.environ.get("ROVIMEN_CLIP_CACHE_MAX_BYTES", str(40 * 1024**3))
)
DEFAULT_ARCHIVE = os.environ.get("ROVIMEN_ARCHIVE_PATH", "/srv/rovimen/archive")
DEFAULT_CACHE_DIR = os.environ.get("ROVIMEN_CLIP_CACHE_DIR", "")
# Clips younger than this may still be uploading (rsync/scp in flight).
DEFAULT_MIN_AGE_S = 300.0
REMUX_TIMEOUT_S = 180.0
# Throttle: the storagebox SSHFS mount is shared with live request serving, so
# an unbounded bulk fill can saturate it and starve the dashboard. Cap the
# number of remuxes per run and pause between them so each timer tick does a
# short burst and then leaves the mount idle; the fill spreads across ticks.
# 0 = unlimited (original behaviour). "medium gentle" defaults set on the VPS.
DEFAULT_MAX_PER_RUN = int(os.environ.get("ROVIMEN_CLIP_CACHE_MAX_PER_RUN", "0"))
DEFAULT_PAUSE_S = float(os.environ.get("ROVIMEN_CLIP_CACHE_PAUSE_S", "0"))


def discover_nights(archive_root: Path, nights: int) -> list[str]:
    """Newest-first list of the last *nights* distinct YYYYMMDD dirs fleet-wide."""
    dates: set[str] = set()
    try:
        cam_dirs = [
            d for d in archive_root.iterdir()
            if d.is_dir() and _CAMERA_RE.match(d.name)
        ]
    except OSError:
        return []
    for cam in cam_dirs:
        try:
            for d in cam.iterdir():
                if d.is_dir() and _DATE_RE.match(d.name):
                    dates.add(d.name)
        except OSError:
            continue
    return sorted(dates, reverse=True)[:nights]


def iter_night_clips(archive_root: Path, dates: list[str]):
    """Yield ``(camera, date, clip_path)`` newest-date-first.

    Iteration order IS the warm priority: the newest night is fully
    processed before any older one, so budget exhaustion degrades the
    oldest nights first.

    One entry per clip stem: when a station uploaded a ready-made
    faststart ``.mp4`` sibling next to the ``.mkv``, the sibling is
    yielded (plain copy downstream); otherwise the ``.mkv`` (remux).
    """
    try:
        cams = sorted(
            d.name for d in archive_root.iterdir()
            if d.is_dir() and _CAMERA_RE.match(d.name)
        )
    except OSError:
        return
    for date in dates:  # already newest-first
        for cam in cams:
            meteors = archive_root / cam / date / "meteors"
            try:
                names = sorted(p.name for p in meteors.iterdir()
                               if p.name.endswith((".mkv", ".mp4")))
            except OSError:
                continue
            by_stem: dict[str, str] = {}
            for name in names:
                stem, ext = name.rsplit(".", 1)
                if ext == "mp4" or stem not in by_stem:
                    by_stem[stem] = name
            for stem in sorted(by_stem):
                yield cam, date, meteors / by_stem[stem]


def cache_size_bytes(cache_root: Path) -> int:
    total = 0
    for p in cache_root.rglob("*.mp4"):
        try:
            total += p.stat().st_size
        except OSError:
            pass
    return total


def prune_old_nights(cache_root: Path, keep_dates: set[str],
                     dry_run: bool = False) -> tuple[int, int]:
    """Delete cache night-dirs not in *keep_dates*. Returns (dirs, bytes)."""
    removed_dirs = 0
    removed_bytes = 0
    for cam_dir in sorted(cache_root.iterdir() if cache_root.exists() else []):
        if not cam_dir.is_dir():
            continue
        for date_dir in sorted(cam_dir.iterdir()):
            if not date_dir.is_dir() or not _DATE_RE.match(date_dir.name):
                continue
            if date_dir.name in keep_dates:
                continue
            size = sum(
                f.stat().st_size for f in date_dir.rglob("*") if f.is_file()
            )
            logger.info("prune: %s (%.1f MB)", date_dir, size / 1e6)
            if not dry_run:
                shutil.rmtree(date_dir, ignore_errors=True)
            removed_dirs += 1
            removed_bytes += size
        if not dry_run:
            try:
                cam_dir.rmdir()  # only succeeds when empty
            except OSError:
                pass
    return removed_dirs, removed_bytes


def evict_to_budget(cache_root: Path, max_bytes: int,
                    dry_run: bool = False) -> int:
    """Drop oldest night-dirs until the cache fits *max_bytes*.

    Only needed when the budget was lowered between runs; steady-state the
    warm loop never writes past the budget.
    """
    used = cache_size_bytes(cache_root)
    if used <= max_bytes:
        return 0
    # (date, dir) pairs oldest-first across cameras.
    night_dirs = sorted(
        (d for cam in cache_root.iterdir() if cam.is_dir()
         for d in cam.iterdir() if d.is_dir() and _DATE_RE.match(d.name)),
        key=lambda d: d.name,
    )
    evicted = 0
    for d in night_dirs:
        if used <= max_bytes:
            break
        size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
        logger.info("evict (over budget): %s (%.1f MB)", d, size / 1e6)
        if not dry_run:
            shutil.rmtree(d, ignore_errors=True)
        used -= size
        evicted += 1
    return evicted


def remux(source: Path, dest: Path) -> None:
    """MKV → faststart MP4, stream copy, through ``<dest>.tmp``. Raises on
    failure."""
    faststart_sibling.remux(source, dest, timeout=REMUX_TIMEOUT_S,
                            tmp=dest.with_name(dest.name + ".tmp"))


def copy_clip(source: Path, dest: Path) -> None:
    """Plain-copy a ready-made faststart MP4 sibling. Raises on failure."""
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        shutil.copyfile(source, tmp)
        tmp.replace(dest)  # atomic — readers never see a partial file
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def warm(archive_root: Path, cache_root: Path, *, nights: int,
         max_bytes: int, min_age_s: float = DEFAULT_MIN_AGE_S,
         max_per_run: int = 0, pause_s: float = 0.0,
         dry_run: bool = False, remux_fn=remux,
         copy_fn=copy_clip) -> dict[str, int]:
    """One warm pass. Returns a stats dict (also logged).

    ``max_per_run`` caps how many clips are transferred this pass (0 = no
    cap); the rest warm on the next tick. ``pause_s`` sleeps between
    transfers to leave the shared SSHFS mount idle. Only actual transfers
    (copy or remux) count toward the cap / get a pause — already-cached /
    young / budget-skipped clips are free.
    """
    dates = discover_nights(archive_root, nights)
    if not dates:
        logger.error(
            "no archive nights found under %s — box unmounted? "
            "Aborting WITHOUT pruning.", archive_root,
        )
        return {"aborted": 1}

    cache_root.mkdir(parents=True, exist_ok=True)
    pruned_dirs, pruned_bytes = prune_old_nights(
        cache_root, set(dates), dry_run=dry_run
    )
    evicted = evict_to_budget(cache_root, max_bytes, dry_run=dry_run)

    used = cache_size_bytes(cache_root)
    now = time.time()
    stats = {
        "warmed": 0, "warmed_bytes": 0, "copied": 0, "already": 0,
        "skipped_budget": 0, "skipped_young": 0, "failed": 0,
        "pruned_dirs": pruned_dirs, "pruned_bytes": pruned_bytes,
        "evicted_dirs": evicted,
    }
    for cam, date, src in iter_night_clips(archive_root, dates):
        try:
            st = src.stat()
        except OSError:
            continue
        if now - st.st_mtime < min_age_s:
            stats["skipped_young"] += 1
            continue
        dest = cache_root / cam / date / (src.stem + ".mp4")
        try:
            dst_st = dest.stat()
            if dst_st.st_size > 0 and dst_st.st_mtime >= st.st_mtime:
                stats["already"] += 1
                continue
            used -= dst_st.st_size  # stale entry will be replaced
        except OSError:
            pass
        # Stream-copy MP4 ≈ source size; estimate before spending the remux.
        if used + st.st_size > max_bytes:
            stats["skipped_budget"] += 1
            continue
        if dry_run:
            stats["warmed"] += 1
            stats["warmed_bytes"] += st.st_size
            used += st.st_size
            if max_per_run and stats["warmed"] >= max_per_run:
                stats["hit_run_cap"] = 1
                break
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        is_copy = src.suffix == ".mp4"
        try:
            (copy_fn if is_copy else remux_fn)(src, dest)
        except Exception as exc:
            stats["failed"] += 1
            logger.warning("%s failed %s: %s",
                           "copy" if is_copy else "remux", src.name, exc)
            continue
        size = dest.stat().st_size
        used += size
        stats["warmed"] += 1
        stats["warmed_bytes"] += size
        if is_copy:
            stats["copied"] += 1

        if max_per_run and stats["warmed"] >= max_per_run:
            stats["hit_run_cap"] = 1
            logger.info(
                "hit per-run cap (%d clips) — remaining clips warm next tick",
                max_per_run,
            )
            break
        if pause_s > 0:
            time.sleep(pause_s)

    logger.info(
        "warm done: nights=%s warmed=%d (%.2f GB, %d copied) already=%d "
        "budget-skipped=%d young=%d failed=%d pruned=%d dirs (%.2f GB) "
        "evicted=%d — cache %.2f / %.0f GB",
        ",".join(dates), stats["warmed"], stats["warmed_bytes"] / 1e9,
        stats["copied"], stats["already"], stats["skipped_budget"],
        stats["skipped_young"], stats["failed"], pruned_dirs,
        pruned_bytes / 1e9, evicted, used / 1e9, max_bytes / 1e9,
    )
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--archive", default=DEFAULT_ARCHIVE)
    parser.add_argument("--cache-dir", default=DEFAULT_CACHE_DIR)
    parser.add_argument("--nights", type=int, default=DEFAULT_NIGHTS)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--min-age", type=float, default=DEFAULT_MIN_AGE_S)
    parser.add_argument("--max-per-run", type=int, default=DEFAULT_MAX_PER_RUN,
                        help="max clips to (re)mux per run; 0 = unlimited")
    parser.add_argument("--pause", type=float, default=DEFAULT_PAUSE_S,
                        help="seconds to sleep between remuxes (mount relief)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    if not args.cache_dir:
        logger.error("ROVIMEN_CLIP_CACHE_DIR / --cache-dir not set")
        return 2
    cache_root = Path(args.cache_dir)
    cache_root.mkdir(parents=True, exist_ok=True)

    # Single-instance guard for manual runs (the timer never overlaps a
    # still-active oneshot unit, but a human might).
    lock_fh = open(cache_root / ".warmer.lock", "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.info("another warmer run holds the lock — exiting")
        return 0

    stats = warm(
        Path(args.archive), cache_root,
        nights=args.nights, max_bytes=args.max_bytes,
        min_age_s=args.min_age, max_per_run=args.max_per_run,
        pause_s=args.pause, dry_run=args.dry_run,
    )
    return 1 if stats.get("aborted") else 0


if __name__ == "__main__":
    sys.exit(main())
