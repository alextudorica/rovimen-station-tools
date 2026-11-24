"""Cache infrastructure -- thumbnail cache, disk persistence, archive index."""

import gzip
import io
import json
import logging
import fcntl
from contextlib import contextmanager
import os
import re
import shutil
import tempfile
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from typing import Any

import yaml

from atomic_write import write_atomic
from config_file import load_config
from models import DashboardConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Bounded per-process LRU cache
# ---------------------------------------------------------------------------
#
# WHY: every gunicorn worker is a full process with its OWN in-heap caches.
# The Redis backend (#637) shares status/vitals/rate-limit/live-thumb across
# workers, but NOT the prefetch dedup sets, the platepar/timelapse SWR caches,
# etc. Those were bounded only by age (14-day prune in the prefetch loop) or
# not at all, so a single worker's RSS climbed with uptime × cameras × nights.
# Two workers under load spiked the ~15 GB dev box into the OOM killer, which
# is why GUNICORN_WORKERS>1 was not deployable.
#
# This is a drop-in replacement for the plain ``dict`` those caches used: it
# subclasses ``OrderedDict`` so ``cache[k] = v`` / ``.get`` / ``.pop`` /
# ``len`` / iteration are unchanged, but every insert moves the key to the MRU
# end and, once the cap is exceeded, evicts the least-recently-used entry.
# With a generous cap (default 512 — the "cache sizes" log showed tens of
# entries in normal operation) nothing user-visible changes at current scale;
# the cap only bites when a cache would otherwise grow without bound.
#
# NOT thread-safe on its own: callers already serialise access under their
# existing per-cache lock (``_prefetched_lock``, the colour-stack lock in
# routes/station_ops, the platepar/timelapse locks, …). Eviction happens
# inside ``__setitem__`` so it runs under that same held lock — do not add a
# second lock here or you risk lock-ordering surprises with the caller's.

def _cache_cap(env_var: str, default: int) -> int:
    """Read a cache cap from ``env_var``, falling back to ``default``.

    A non-positive or unparseable value disables the cap (cap of 0 means
    "unbounded"), matching the disk-cache "0 == disabled" convention used
    elsewhere. Caps are read once at construction so a worker's bound is
    fixed for its lifetime.
    """
    raw = os.environ.get(env_var)
    if raw is None:
        return default
    try:
        val = int(raw)
    except ValueError:
        logger.warning("Ignoring non-integer %s=%r; using default %d",
                       env_var, raw, default)
        return default
    return max(0, val)


class BoundedCache(OrderedDict):
    """OrderedDict with a hard entry cap and LRU eviction on insert.

    ``maxsize <= 0`` disables eviction (behaves like a plain dict). Reads via
    ``get``/``__getitem__`` do NOT promote (kept cheap and lock-light); recency
    is defined by insert order, which is what all our callers do on every
    refresh anyway (they re-``__setitem__`` the entry each time they touch it).
    """

    def __init__(self, maxsize: int, *args: Any, **kwargs: Any) -> None:
        self._maxsize = maxsize
        super().__init__(*args, **kwargs)

    def __setitem__(self, key: Any, value: Any) -> None:
        if key in self:
            # Refresh recency: move an existing key to the MRU end so it isn't
            # the next thing evicted.
            self.move_to_end(key)
        super().__setitem__(key, value)
        if self._maxsize > 0:
            while len(self) > self._maxsize:
                self.popitem(last=False)  # drop the LRU (oldest-inserted) entry


# ---------------------------------------------------------------------------
# Thumbnail cache
# ---------------------------------------------------------------------------

# Hot tier: local NVMe SSD for the last few nights.  94.7% of thumbnail
# requests hit the last 3 days, so a short retention keeps the SSD small
# while nginx X-Accel-Redirect serves them at <1 ms.
THUMB_CACHE_DIR = Path(os.environ.get("THUMB_CACHE_DIR", "/opt/rovimen/thumb_cache"))
THUMB_CACHE_DAYS = int(os.environ.get("ROVIMEN_THUMB_CACHE_DAYS", "2"))

# Emergency free-space floor for the volume hosting the thumb cache. The
# 2-day age prune keeps steady-state small, but it can't reclaim (a) content
# that is NOT under a YYYYMMDD date-dir, nor (b) space taken by anything else
# sharing the volume (e.g. the clip cache). When free space drops below this
# floor we evict the OLDEST cached date-dirs regardless of age until it
# recovers. Keying off free BYTES (not a per-subtree budget) is what makes
# this a true backstop against the disk-full -> "skipping writes" failure that
# silently stops thumbnail caching. Set 0 to disable.
THUMB_CACHE_MIN_FREE_GB = float(
    os.environ.get("ROVIMEN_THUMB_CACHE_MIN_FREE_GB", "8")
)
# Sky-dome per-night timelapse MP4s live at
# ``<host>/sky_dome_timelapse/<date>.mp4`` -- files, not YYYYMMDD dirs, so the
# date-dir prune never touches them and they accumulate forever. Age them out
# on a longer horizon (they are a nice per-night artifact, but not forever).
THUMB_DOME_TIMELAPSE_DAYS = int(
    os.environ.get("ROVIMEN_THUMB_DOME_TL_DAYS", "30")
)

# When nginx is in front, use X-Accel-Redirect so nginx serves cached files
# directly from disk without going through Python.
NGINX_ACCEL = os.environ.get("NGINX_ACCEL") == "1"

# Refuse to write to the thumbnail cache when free space drops below this
# threshold. The cache lives on NVMe; if it fills, every subsequent write
# 500s the worker and we lose the request entirely. Better to serve the
# bytes once and skip the cache promotion than to crash on disk-full.
_THUMB_CACHE_MIN_FREE_BYTES = 500 * 1024 * 1024  # 500 MB
_thumb_cache_full_warn_lock = threading.Lock()
_thumb_cache_full_last_warn: float = 0.0


def _thumb_cache_has_space() -> bool:
    """Return True if THUMB_CACHE_DIR has enough free space to write a thumb.

    Logs a WARN at most once per hour when the cache is full so we notice
    the problem without flooding logs on every request.
    """
    global _thumb_cache_full_last_warn
    try:
        free = shutil.disk_usage(THUMB_CACHE_DIR).free
    except OSError:
        # If we can't stat the cache (e.g. SSHFS dropped), assume it's fine
        # and let the write attempt surface a clearer error.
        return True
    if free >= _THUMB_CACHE_MIN_FREE_BYTES:
        return True
    now = time.monotonic()
    with _thumb_cache_full_warn_lock:
        if now - _thumb_cache_full_last_warn > 3600:
            logger.warning(
                "Thumb cache low on space (%.1f MB free); skipping writes",
                free / (1024 * 1024),
            )
            _thumb_cache_full_last_warn = now
    return False


def _thumb_cache_path(host_key: str, cam: str, date: str, filename: str) -> Path:
    return THUMB_CACHE_DIR / host_key / cam / date / filename


# BW FF thumbnails, one per 20 s chunk (~1.7 KB, ~180 per camera per hour).
# They are kept on the VPS disk and never go through the blob spool: one
# Storage Box write per file drained ~116 files/min, so on 2026-11-05 seven
# stations' thumbnails held every colour stack in the spool for 40 min.
FFTHUMB_DIR = Path(os.environ.get("ROVIMEN_FFTHUMB_DIR", "/opt/rovimen/ffthumbs"))
FFTHUMB_DAYS = int(os.environ.get("ROVIMEN_FFTHUMB_DAYS", "14"))


def ffthumb_local_path(cam: str, date: str, filename: str) -> Path:
    return FFTHUMB_DIR / cam / date / filename


def _prune_ffthumbs(cutoff_date: str) -> int:
    """Remove FFTHUMB_DIR night dirs older than *cutoff_date*; returns the count."""
    removed = 0
    try:
        cam_dirs = [d for d in FFTHUMB_DIR.iterdir() if d.is_dir()]
    except OSError:
        return 0
    for cam_dir in cam_dirs:
        try:
            nights = [d for d in cam_dir.iterdir() if d.is_dir()]
        except OSError:
            continue
        for night in nights:
            if re.fullmatch(r"\d{8}", night.name) and night.name < cutoff_date:
                shutil.rmtree(night, ignore_errors=True)
                removed += 1
    return removed


# A branded-stack night dir goes once nothing in it was rendered for this
# long. Its source is on the box, so a later request costs one re-render; the
# route already tells browsers to keep the image 30 days (max-age=2592000).
BRANDED_STACK_DAYS = 30


def _prune_branded_stacks(now: float | None = None) -> int:
    """Remove BRANDED_STACK_DIR night dirs whose newest file is older than
    BRANDED_STACK_DAYS; returns the count removed.

    Aged by render time, not by the night's date: only stacks from before the
    station overlay (nights before August 2026) need a render, so a night-date
    bound would empty the cache on every pass.
    """
    cutoff = (time.time() if now is None else now) - BRANDED_STACK_DAYS * 86400
    removed = 0
    for night in BRANDED_STACK_DIR.glob("*/*"):
        try:
            if not night.is_dir():
                continue
            newest = max((f.stat().st_mtime for f in night.iterdir()),
                         default=night.stat().st_mtime)
        except OSError:
            continue
        if newest < cutoff:
            shutil.rmtree(night, ignore_errors=True)
            removed += 1
    return removed


def _is_nonempty_file(path: Path) -> bool:
    """True when ``path`` is a file with size > 0.

    A 0-byte cache file must be treated as a MISS, not a hit: prefetch copies
    that hit an EOF-empty SSHFS read (stalled storage-box mount) or a 0-byte
    source can persist an empty ``.webp``, which the archive route would then
    serve as an immutable, 30-day-cached empty image — a permanently black
    thumbnail. Gating both the serve and the cache-write on this stops that.
    """
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _thumb_archive_path(host_key: str, cam: str, date: str, filename: str) -> Path | None:
    if THUMB_ARCHIVE_DIR is None:
        return None
    return THUMB_ARCHIVE_DIR / host_key / cam / date / filename


def _downsample_thumbnail_bytes(data: bytes, max_dim: int = 480) -> bytes:
    """Re-encode an image to fit within a ``max_dim``-px bounding box,
    preserving aspect ratio. WebP-encoded output at quality 80, method 4
    (size/speed knee). Returns the input unchanged when it's already
    within bounds OR when decode fails (caller must remain robust to
    pass-through). Caller is responsible for only invoking this on
    paths that are conceptually thumbnails -- full-resolution endpoints
    (e.g. /fullstack/...) must NOT call this.

    Context: some stations send a 480x300 thumbnail (~5 KB) while others
    send a 1280x720 full stack (~230 KB). Without this clamp the VPS
    cache fills with full-resolution stacks and exhausts the volume.
    """
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        if img.width <= max_dim and img.height <= max_dim:
            return data
        img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="WEBP", quality=80, method=4)
        return buf.getvalue()
    except Exception:
        return data


def _cold_tier_free_bytes() -> int | None:
    """Free bytes on the cold-tier (storage-box) volume, or None if it can't
    be stat'd. Used to skip promotion when the box is full: copying to a 100%
    volume just fails per-file over the slow sshfs mount and wedges the prune,
    so we short-circuit to a plain delete instead."""
    if THUMB_ARCHIVE_DIR is None:
        return None
    try:
        return shutil.disk_usage(THUMB_ARCHIVE_DIR).free
    except OSError:
        return None


def _thumb_cache_date_dirs() -> list[Path]:
    """All YYYYMMDD date-dirs anywhere under the thumb cache (one tree walk).

    Covers every layout: ``<host>/<cam>/<date>``,
    ``<host>/rms_plots/<cam>/<date>`` and ``<host>/sky_dome_archive/<date>``.
    """
    pattern = "[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]"
    out: list[Path] = []
    for d in THUMB_CACHE_DIR.rglob(pattern):
        try:
            if d.is_dir() and d.name.isdigit() and len(d.name) == 8:
                out.append(d)
        except OSError:
            continue
    return out


def _prune_dome_timelapses(cutoff_date: str) -> int:
    """Delete per-night sky-dome timelapse MP4s older than ``cutoff_date``.
    These are files (``<host>/sky_dome_timelapse/<date>.mp4``), not date-dirs,
    so the date-dir prune skips them. Returns the count removed."""
    removed = 0
    for mp4 in THUMB_CACHE_DIR.glob("*/sky_dome_timelapse/*.mp4"):
        try:
            if mp4.stem < cutoff_date:
                mp4.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def _prune_thumb_cache_once() -> None:
    """One prune pass: age-out old date-dirs (promoting to the cold tier when
    it has room), age-out old sky-dome timelapses and branded-stack nights
    (``_prune_branded_stacks``), then -- as a hard backstop
    against a full volume -- evict the OLDEST remaining date-dirs until the
    volume has ``THUMB_CACHE_MIN_FREE_GB`` free again.
    """
    cutoff_date = (
        datetime.now(timezone.utc) - timedelta(days=THUMB_CACHE_DAYS)
    ).strftime("%Y%m%d")
    date_dirs = _thumb_cache_date_dirs()

    # Only promote to the cold tier when it actually has room; a full box makes
    # promotion a slow no-op that would otherwise stall every prune pass.
    cold_free = _cold_tier_free_bytes()
    can_promote = (
        THUMB_ARCHIVE_DIR is not None
        and cold_free is not None
        and cold_free > _THUMB_CACHE_MIN_FREE_BYTES
    )

    promoted = 0
    remaining: list[Path] = []
    for d in date_dirs:
        try:
            if d.name >= cutoff_date:
                remaining.append(d)
                continue
            if can_promote:
                promoted += _promote_dir_to_archive(d)
            shutil.rmtree(d, ignore_errors=True)
        except Exception:
            remaining.append(d)
            continue

    _prune_ffthumbs(
        (datetime.now(timezone.utc) - timedelta(days=FFTHUMB_DAYS)).strftime("%Y%m%d"))
    dome_removed = _prune_dome_timelapses(
        (datetime.now(timezone.utc)
         - timedelta(days=THUMB_DOME_TIMELAPSE_DAYS)).strftime("%Y%m%d")
    )
    branded_removed = _prune_branded_stacks()

    evicted = _evict_thumb_cache_to_free(remaining)

    if promoted or dome_removed or branded_removed or evicted:
        logger.info(
            "thumb prune: promoted %d files, aged %d dome timelapses, "
            "aged %d branded-stack nights, evicted %d over-budget date-dirs",
            promoted, dome_removed, branded_removed, evicted,
        )


def _evict_thumb_cache_to_free(candidates: list[Path]) -> int:
    """Free-space backstop: while the thumb-cache volume has less than
    ``THUMB_CACHE_MIN_FREE_GB`` free, delete the OLDEST candidate date-dirs
    (by date name) without promoting -- the cold tier is best-effort and,
    under space pressure, is usually full anyway. Returns dirs evicted."""
    floor = int(THUMB_CACHE_MIN_FREE_GB * 1024**3)
    if floor <= 0:
        return 0
    try:
        free = shutil.disk_usage(THUMB_CACHE_DIR).free
    except OSError:
        return 0
    if free >= floor:
        return 0
    logger.warning(
        "thumb cache low on free space (%.1f GB < %.1f GB floor); evicting "
        "oldest cached date-dirs", free / 1024**3, THUMB_CACHE_MIN_FREE_GB,
    )
    evicted = 0
    for d in sorted(candidates, key=lambda p: p.name):  # oldest date first
        if free >= floor:
            break
        try:
            shutil.rmtree(d, ignore_errors=True)
            evicted += 1
            free = shutil.disk_usage(THUMB_CACHE_DIR).free
        except OSError:
            continue
    return evicted


# ---------------------------------------------------------------------------
# Orphaned build-artifact reaper
# ---------------------------------------------------------------------------
#
# The media/archive/sky-dome/compilation routes stage transcodes in temp dirs
# via ``tempfile``. The context manager cleans them up on normal exit, but a
# worker SIGKILLed mid-build (gunicorn ``--timeout``, or a deploy restart)
# never runs ``__exit__`` -- so the temp dir (holding hundreds of MB of clip
# MP4s) is orphaned and piles up (16 GB across 3 days on prod, 2026-07). This
# daemon age-gates and removes them.
#
# Every prefix below is ROVIMEN-specific, so the sweep is safe even when the
# process shares a system-wide ``/tmp`` (dev boxes, CI) -- it can only match
# our own artifacts, never another app's. The heavy ``TemporaryDirectory()``
# callers were given these prefixes precisely so this reaper needs no generic
# ``tmp*`` match (which would be unsafe outside a PrivateTmp namespace).
_TMP_ARTIFACT_PREFIXES = (
    "rovimen_dome_", "rovimen_media_", "rovimen_stitch_",
    "stitch_", "remux_", "crop_", "pub_remux_", "batchdl_",
    "rovimen_compilation_", "nightly_tg_",
)
_TMP_REAP_MAX_AGE_S = int(os.environ.get("ROVIMEN_TMP_REAP_MAX_AGE_S", "7200"))
_TMP_REAP_INTERVAL_S = 1800


def _reap_stale_tmp_once(max_age_s: int = _TMP_REAP_MAX_AGE_S) -> int:
    """Remove ROVIMEN build artifacts in the temp dir older than ``max_age_s``.
    Returns the count removed. Lock files (``*.lock``) are never touched."""
    tmp_root = Path(tempfile.gettempdir())
    now = time.time()
    reaped = 0
    try:
        entries = list(tmp_root.iterdir())
    except OSError:
        return 0
    for entry in entries:
        name = entry.name
        if name.endswith(".lock") or not name.startswith(_TMP_ARTIFACT_PREFIXES):
            continue
        try:
            if now - entry.stat().st_mtime <= max_age_s:
                continue
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink()
            reaped += 1
        except OSError:
            continue
    if reaped:
        logger.info(
            "tmp reaper: removed %d orphaned build artifacts from %s",
            reaped, tmp_root,
        )
    return reaped


def _reap_stale_tmp(should_run: Callable[[], bool] = lambda: True) -> None:
    """Daemon: periodically reap orphaned transcode/build temp artifacts.

    Every gunicorn worker starts this loop, but they share one temp dir, so a
    pass runs only while ``should_run()`` (the worker's duty-lock check) says
    this worker owns the sweep. Checked per pass, so the duty moves to another
    worker when its owner recycles."""
    while True:
        try:
            if should_run():
                _reap_stale_tmp_once()
        except Exception:
            logger.exception("tmp reaper error")
        time.sleep(_TMP_REAP_INTERVAL_S)


def _prune_thumb_cache(should_run: Callable[[], bool] = lambda: True) -> None:
    """Hourly prune of the SSD hot cache. Expired night-dirs (older than
    THUMB_CACHE_DAYS) are MOVED to the cold-tier storage box rather than
    deleted, so historical thumbnails remain available via the cold lookup
    in ``_serve_thumbnail``. A free-space backstop evicts the oldest cached
    date-dirs whenever the volume runs low, so the cache can never fill the
    disk and silently stop caching (the failure mode that motivated this).

    If THUMB_ARCHIVE_DIR is unset the old behaviour (plain delete) is
    preserved. The cold tier has unlimited retention.

    The cache is shared by every gunicorn worker, so a pass runs only while
    ``should_run()`` (the worker's duty-lock check) says this worker owns the
    prune; otherwise every worker promoted the same night-dirs over SSHFS.
    """
    while True:
        time.sleep(3600)
        try:
            if should_run():
                _prune_thumb_cache_once()
        except Exception:
            logger.exception("thumb cache prune error")


def _promote_dir_to_archive(src_dir: Path) -> int:
    """Copy all files from an SSD night-dir to the matching cold-tier path.
    Skips files that already exist on the cold tier.  Returns count of
    files copied."""
    rel = src_dir.relative_to(THUMB_CACHE_DIR)
    dst_dir = THUMB_ARCHIVE_DIR / rel
    count = 0
    try:
        dst_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return 0
    for f in src_dir.iterdir():
        if not f.is_file():
            continue
        dst = dst_dir / f.name
        if dst.exists():
            continue
        try:
            # Per-process temp, so a second promoter can never interleave
            # its copy into this one and publish a torn file.
            tmp = dst.with_name(f"{dst.name}.{os.getpid()}.part")
            shutil.copy2(f, tmp)
            tmp.replace(dst)
            count += 1
        except OSError:
            continue
    return count


class ConfigStore:
    """Owns the on-disk ``dashboard_config.yaml`` for one live ``DashboardConfig``.

    Every gunicorn worker holds its own ``DashboardConfig`` object, and the
    admin API used to mutate that object and dump it whole to disk. Two
    workers editing different cameras therefore overwrote each other: the
    second save re-wrote the first camera from its stale memory (observed on
    gmnro01, 2026-09-05 -- RO000A's IP flipped back on every other PATCH).

    This keeps the file the source of truth instead:

    * ``mutation()`` takes the cross-process lock, reloads the file into the
      live object, lets the caller mutate, then writes -- so a save always
      starts from what is on disk, not from what this worker last saw.
    * ``refresh_if_changed()`` is a one-``stat`` check for request hooks, so
      every worker adopts another worker's save on its next request instead
      of on its next restart.

    Fields are swapped on the live object by attribute assignment, never by
    mutating the ``stations`` dict in place: closures across the app hold the
    ``DashboardConfig`` instance, and a reader iterating the old dict keeps a
    consistent snapshot while the new one lands atomically.
    """

    def __init__(self, config: DashboardConfig, path: Path | None,
                 to_dict: Callable[[], dict]) -> None:
        self.config = config
        self.path = path
        self.to_dict = to_dict
        self.lock_path = (path.with_suffix(path.suffix + ".lock") if path else None)
        self._thread_lock = threading.Lock()
        self._mtime_ns: int | None = self._stat_mtime()

    # -- disk state -------------------------------------------------------
    def _stat_mtime(self) -> int | None:
        if not self.path:
            return None
        try:
            return self.path.stat().st_mtime_ns
        except OSError:
            return None

    def _adopt(self, fresh: DashboardConfig) -> None:
        for name in DashboardConfig.model_fields:
            setattr(self.config, name, getattr(fresh, name))

    def reload(self) -> bool:
        """Replace the live object's fields with the file's. False if unreadable."""
        if not self.path:
            return False
        try:
            fresh = load_config(self.path)
        except Exception as exc:  # torn write, bad YAML: keep serving what we have
            logger.warning("config reload from %s failed: %s", self.path, exc)
            return False
        self._adopt(fresh)
        self._mtime_ns = self._stat_mtime()
        return True

    def refresh_if_changed(self) -> bool:
        """Reload only when the file's mtime moved. One ``stat`` per call."""
        current = self._stat_mtime()
        if current is None or current == self._mtime_ns:
            return False
        with self._thread_lock:
            if current == self._mtime_ns:
                return False
            return self.reload()

    # -- writes -----------------------------------------------------------
    def _write_locked(self) -> None:
        assert self.path is not None
        payload = yaml.dump(self.to_dict(), default_flow_style=False, allow_unicode=True)
        write_atomic(self.path, payload, tmp=self.path.with_suffix(self.path.suffix + ".tmp"))
        self._mtime_ns = self._stat_mtime()

    @contextmanager
    def mutation(self):
        """Lock, reload from disk, yield the live config for editing, write."""
        if not self.path or self.lock_path is None:
            yield self.config
            return
        with self._thread_lock:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.lock_path, "a+") as lf:
                fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
                self.reload()
                yield self.config
                self._write_locked()

    def save(self) -> None:
        """Write the live object as-is (legacy path for callers that mutated
        before acquiring the lock). Prefer ``mutation()``."""
        if not self.path or self.lock_path is None:
            return
        with self._thread_lock:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.lock_path, "a+") as lf:
                fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
                self._write_locked()


# ---------------------------------------------------------------------------
# Archive + cache paths
# ---------------------------------------------------------------------------

ARCHIVE_PATH = Path(os.environ.get("ROVIMEN_ARCHIVE_PATH", "/srv/rovimen/archive"))
CACHE_PATH = Path(os.environ.get("ROVIMEN_CACHE_PATH", "/opt/rovimen/cache"))
# Branded copies of per-detection stacks the archive route rendered the overlay
# onto (stack_overlay.OverlaidStackCache), as <cam>/<night>/<stack>.webp.
BRANDED_STACK_DIR = CACHE_PATH / "branded_stacks"
COMPILATIONS_OUT_PATH = Path(
    os.environ.get("ROVIMEN_COMPILATIONS_OUT_PATH", "/opt/rovimen/compilations")
)
COMPILATIONS_ARCHIVE_PATH = ARCHIVE_PATH / "compilations"
INTRO_PATH = Path(os.environ.get("ROVIMEN_INTRO_PATH", "/opt/rovimen/intro.mp4"))
CACHE_TTL_HOURS = 24

# Cold tier: storage-box thumb archive (unlimited retention).  Populated by
# the hourly prune which MOVEs expired SSD thumbs here instead of deleting.
_thumb_archive_env = os.environ.get("THUMB_ARCHIVE_DIR", "")
THUMB_ARCHIVE_DIR: Path | None = (
    Path(_thumb_archive_env) if _thumb_archive_env
    else ARCHIVE_PATH / ".thumb_cache"
)


# ---------------------------------------------------------------------------
# On-disk persistence for the slow SSHFS-walk caches
# ---------------------------------------------------------------------------
#
# Without this, the dashboard cold-starts on every restart: the first user
# to open the Archive or VideoDB tab pays a 5-20 s SSHFS walk per camera --
# on a 24-cam fleet that's up to 8 min of intermittent slow loads while the
# startup pre-warm catches up. With persistence the in-memory cache is
# rehydrated from JSON the instant the process starts, so users see data
# immediately and a background refresh quietly replaces it when stale.
#
# Disk shape: {"saved_at": wall_clock_seconds, "data": <cache value>}.
# Wall-clock time is used (not monotonic) so age survives process restarts.

_CACHE_REFRESH_EXECUTOR: ThreadPoolExecutor | None = None
_CACHE_REFRESH_LOCK = threading.Lock()
_CACHE_REFRESH_INFLIGHT: dict[str, set[str]] = {}


def _disk_cache_dir(name: str) -> Path:
    return CACHE_PATH / "walk_cache" / name


def _disk_cache_path(name: str, key: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", key)
    return _disk_cache_dir(name) / f"{safe}.json"


def _disk_cache_write(name: str, key: str, data: Any) -> None:
    try:
        path = _disk_cache_path(name, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Private staging file per writer: two workers refreshing the same key
        # at once used to truncate and write one shared ``.tmp`` and whoever
        # renamed last could publish the other's half-written body.
        write_atomic(
            path, json.dumps({"saved_at": time.time(), "data": data}),
            tmp=path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"),
        )
    except Exception:
        logger.exception("disk-cache write failed for %s/%s", name, key)


def rms_night_generation(cam: str, date: str) -> int:
    """When a night's archived rms/ files last changed (mtime_ns of its
    marker), 0 if never. Caches of anything read from that rms/ dir compare
    against it, so a re-upload reaches every gunicorn worker at once."""
    try:
        return _disk_cache_path("rms_night_gen", f"{cam}_{date}").stat().st_mtime_ns
    except OSError:
        return 0


def invalidate_rms_night(cam: str, date: str, filename: str) -> None:
    """A station replaced ``<cam>/<date>/rms/<filename>`` on the archive (a
    reprocess with a new platepar rewrites FTPdetectinfo and the calibration
    plots under the same names). Drop the caches that hold the old version:
    the plot bytes cached by filename, the parsed detection rows, and bump the
    night's generation marker for the in-memory copies."""
    try:
        hosts = list(THUMB_CACHE_DIR.iterdir()) if THUMB_CACHE_DIR.is_dir() else []
    except OSError:
        hosts = []
    for host in hosts:
        try:
            (host / "rms_plots" / cam / date / filename).unlink(missing_ok=True)
        except OSError:
            logger.warning("could not drop cached %s/%s/%s under %s", cam, date, filename, host)
    try:
        _disk_cache_path("rms_detections", f"{cam}_{date}").unlink(missing_ok=True)
        marker = _disk_cache_path("rms_night_gen", f"{cam}_{date}")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"saved_at": time.time(), "data": filename}))
    except OSError:
        logger.exception("could not invalidate rms caches for %s/%s", cam, date)


def _disk_cache_load_into(
    name: str, target: dict[str, tuple[float, Any]]
) -> int:
    """Rehydrate the in-memory cache `target` from disk. Returns the count
    of entries loaded. The monotonic timestamp is back-dated by the file's
    wall-clock age so the existing fresh/stale TTL logic treats the entry
    exactly as it would have if populated at that moment."""
    base = _disk_cache_dir(name)
    if not base.is_dir():
        return 0
    now_wall = time.time()
    now_mono = time.monotonic()
    loaded = 0
    for f in base.glob("*.json"):
        try:
            blob = json.loads(f.read_text())
            saved_at = float(blob["saved_at"])
            data = blob["data"]
        except Exception:
            continue
        age = max(0.0, now_wall - saved_at)
        target[f.stem] = (now_mono - age, data)
        loaded += 1
    return loaded


def _kick_cache_refresh(name: str, key: str, refresh_fn) -> None:
    """Run refresh_fn() in a background thread, coalescing concurrent
    kicks for the same (name, key) pair. The first request that finds a
    stale entry triggers the refresh; subsequent requests during the
    refresh window keep serving the stale entry rather than piling on.
    """
    global _CACHE_REFRESH_EXECUTOR
    with _CACHE_REFRESH_LOCK:
        if _CACHE_REFRESH_EXECUTOR is None:
            _CACHE_REFRESH_EXECUTOR = ThreadPoolExecutor(
                max_workers=4, thread_name_prefix="cache-refresh"
            )
        inflight = _CACHE_REFRESH_INFLIGHT.setdefault(name, set())
        if key in inflight:
            return
        inflight.add(key)

    def _run() -> None:
        try:
            refresh_fn()
        except Exception:
            logger.exception("background cache refresh failed for %s/%s", name, key)
        finally:
            with _CACHE_REFRESH_LOCK:
                _CACHE_REFRESH_INFLIGHT.get(name, set()).discard(key)

    _CACHE_REFRESH_EXECUTOR.submit(_run)


def _drop_future_dates(dates: list[str]) -> list[str]:
    """Filter a list of YYYYMMDD strings to drop any > today's UTC date.

    Captured nights cannot exist in the future. Defensive filter so that any
    stray future-dated directory on disk (clock skew during a restore, a
    manually-created test dir, etc.) never reaches the UI as a selectable
    date. The client also applies a `max=today` attribute on date pickers --
    this is the server-side belt to its client-side suspenders.
    """
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    return [d for d in dates if isinstance(d, str) and d <= today]


# ---------------------------------------------------------------------------
# Archive index -- in-memory mirror of the storagebox directory tree
# ---------------------------------------------------------------------------

_CAM_RE = re.compile(r"^[A-Z0-9]+$", re.IGNORECASE)
_DATE_RE = re.compile(r"^\d{8}$")


@dataclass(slots=True)
class _NightIndex:
    """Pre-scanned file listing for one camera/date in the archive."""
    meteor_files: tuple[str, ...]
    stack_files: frozenset[str]
    timelapse_files: tuple[str, ...]
    state_chunks: dict = None
    ffthumb_files: frozenset[str] = frozenset()
    rms_files: frozenset[str] = frozenset()
    # state.json's top-level rms_complete, read in the same pass as the
    # chunks so the Detections "Process night" flag needs no second read.
    # None = unknown: no readable state.json (state_chunks is None too), or a
    # listing restored from a snapshot written before this field existed.
    rms_complete: bool | None = None
    # state.json's morning_done (dawn finished the night), read the same way.
    morning_done: bool | None = None
    # state.json's stage_times (when RMS, dawn and uploads finished), or None.
    stage_times: dict | None = None
    # state.json's timelapse_done, read the same way as morning_done.
    timelapse_done: bool | None = None
    # What the night looked like when it was listed (see _night_signature) and
    # when (wall clock). None = unknown, so the next refresh lists it again.
    sig: tuple[int, ...] | None = None
    scanned_at: float = 0.0

    def state_mtime(self) -> float | None:
        """When the station last pushed this night's state.json (epoch s),
        from the listing's signature; None if unknown or absent."""
        if not self.sig or len(self.sig) <= len(_NIGHT_SUBDIRS) or not self.sig[len(_NIGHT_SUBDIRS)]:
            return None
        return self.sig[len(_NIGHT_SUBDIRS)] / 1e9


# Subdirectories one night listing covers, in signature order.
_NIGHT_SUBDIRS = ("meteors", "stacks", "timelapse", "ffthumbs", "rms")
_SIG_TIMELAPSE = _NIGHT_SUBDIRS.index("timelapse")
# A listing whose directories changed within this many seconds of the scan may
# have raced a same-second write (SFTP mtimes are whole seconds), so it is not
# trusted for revalidation until a later rescan settles it.
_SIG_SETTLE_S = 2.0
# Upper bound on how long a night is served on "signature unchanged" alone
# before it is listed again anyway. Covers anything the mtime check can't see
# (clock skew between the VPS and the box, a filesystem that doesn't bump a
# directory's mtime) with bounded staleness instead of none.
_SIG_MAX_AGE_S = float(os.environ.get("ROVIMEN_ARCHIVE_SIG_MAX_AGE_S", "600"))
# The same bound for a night outside the hot window. Such a night changes only
# when a clip is pruned, re-uploaded or locked late, and each of those moves a
# directory mtime or state.json, so its listing is trusted on the signature
# for a week; an hour's bound meant relisting every old night of a camera
# (1.5 s each over sshfs, stacks/ alone ~1,900 entries) on every visit.
_SIG_MAX_AGE_OLD_S = float(os.environ.get("ROVIMEN_ARCHIVE_SIG_MAX_AGE_OLD_S", str(7 * 86400)))


def _night_signature(night_path: str) -> tuple[int, ...]:
    """Cheap fingerprint of a night directory: the mtime of each listed
    subdirectory plus state.json's mtime and size (0 when absent).

    A directory's mtime moves whenever an entry is added, removed or renamed
    into it, so an unchanged signature means an unchanged listing -- six
    ``stat`` calls instead of five ``scandir`` walks over directories holding
    ~1,400 entries each on the sshfs Storage Box.
    """
    sig: list[int] = []
    for sub in _NIGHT_SUBDIRS:
        try:
            sig.append(os.stat(os.path.join(night_path, sub)).st_mtime_ns)
        except OSError:
            sig.append(0)
    try:
        st = os.stat(os.path.join(night_path, "state.json"))
        sig.extend((st.st_mtime_ns, st.st_size))
    except OSError:
        sig.extend((0, 0))
    return tuple(sig)


def _signature_trusted(ni: "_NightIndex", now: float,
                       max_age_s: float | None = None) -> bool:
    """Whether ``ni`` may be kept on an unchanged signature, i.e. its listing
    was taken after its directories last changed and is not too old
    (``max_age_s``, default ``_SIG_MAX_AGE_S``)."""
    if max_age_s is None:
        max_age_s = _SIG_MAX_AGE_S
    if not ni.sig or now - ni.scanned_at >= max_age_s:
        return False
    newest_s = max(ni.sig[: len(_NIGHT_SUBDIRS) + 1]) / 1e9
    return newest_s < ni.scanned_at - _SIG_SETTLE_S


def _parse_sig(raw: Any) -> tuple[int, ...] | None:
    expected = len(_NIGHT_SUBDIRS) + 2
    if isinstance(raw, list) and len(raw) == expected and all(isinstance(v, int) for v in raw):
        return tuple(raw)
    return None


def _night_entry(ni: _NightIndex) -> dict[str, Any]:
    """One night listing as stored on disk (archive_index.json, and the
    per-night files of old nights)."""
    entry: dict[str, Any] = {
        "m": list(ni.meteor_files),
        "s": sorted(ni.stack_files),
        "t": list(ni.timelapse_files),
    }
    if ni.state_chunks is not None:
        entry["c"] = ni.state_chunks
    if ni.rms_complete is not None:
        entry["rc"] = ni.rms_complete
    if ni.morning_done is not None:
        entry["md"] = ni.morning_done
    if ni.stage_times:
        entry["st"] = ni.stage_times
    if ni.timelapse_done is not None:
        entry["td"] = ni.timelapse_done
    if ni.ffthumb_files:
        entry["f"] = sorted(ni.ffthumb_files)
    # RMS plots. Omitted here until 2026-09-04, which meant every
    # night rehydrated from disk came back claiming it had NO
    # plots -- and night_files() serves that cached entry without
    # rescanning, so after any restart the whole index reported
    # "no RMS plots" until a full rebuild happened to repopulate
    # it. That is what makes plot panels render empty for a
    # camera whose plots are sitting in the archive.
    if ni.rms_files:
        entry["r"] = sorted(ni.rms_files)
    # Signature + scan time, so a restarted worker revalidates
    # this night with six stats instead of relisting it.
    if ni.sig:
        entry["g"] = list(ni.sig)
        entry["a"] = ni.scanned_at
    return entry


def _night_disk_path(cam: str, date: str) -> Path:
    return CACHE_PATH / "archive_nights" / cam / f"{date}.json.gz"


def _read_night_disk(cam: str, date: str) -> _NightIndex | None:
    """An old night's listing as a worker last saved it, or None. Only a
    signed listing is returned: it is kept only after a signature check."""
    if not (_CAM_RE.match(cam) and _DATE_RE.match(date)):
        return None
    try:
        entry = json.loads(gzip.decompress(_night_disk_path(cam, date).read_bytes()))
        ni = _night_from_entry(entry)
    except (OSError, ValueError, TypeError, AttributeError, EOFError):
        return None
    return ni if ni.sig else None


def _write_night_disk(cam: str, date: str, ni: _NightIndex) -> None:
    """Save an old night's listing (gzip'd JSON, ~10 KB) for every worker and
    the next restart. Private temp name per writer, then an atomic rename."""
    if not ni.sig or not (_CAM_RE.match(cam) and _DATE_RE.match(date)):
        return
    path = _night_disk_path(cam, date)
    try:
        write_atomic(
            path,
            gzip.compress(json.dumps(_night_entry(ni), separators=(",", ":")).encode(),
                          compresslevel=5),
            tmp=path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"),
            mkdir=True,
        )
    except OSError:
        logger.warning("archive index: failed to save night %s/%s", cam, date)


def _night_from_entry(entry: dict[str, Any]) -> _NightIndex:
    """Inverse of ``_night_entry``."""
    scanned_at = entry.get("a")
    return _NightIndex(
        meteor_files=tuple(entry.get("m", ())),
        stack_files=frozenset(entry.get("s", ())),
        timelapse_files=tuple(entry.get("t", ())),
        state_chunks=entry.get("c"),
        ffthumb_files=frozenset(entry.get("f", ())),
        rms_files=frozenset(entry.get("r", ())),
        sig=_parse_sig(entry.get("g")),
        scanned_at=float(scanned_at) if isinstance(scanned_at, (int, float)) else 0.0,
        rms_complete=(bool(entry["rc"]) if isinstance(entry.get("rc"), bool) else None),
        morning_done=(entry["md"] if isinstance(entry.get("md"), bool) else None),
        stage_times=(entry["st"] if isinstance(entry.get("st"), dict) else None),
        timelapse_done=(entry["td"] if isinstance(entry.get("td"), bool) else None),
    )


_ARCHIVE_HOT_DAYS = 3
_ARCHIVE_ONDEMAND_TTL = 120  # seconds
# How long an expired on-demand entry is kept for signature revalidation.
_ARCHIVE_ONDEMAND_RETAIN_S = 3600
# How long a "this night has no files" answer is trusted. Short, because a
# night in progress starts empty and fills, but non-zero: without it every
# request for a camera-night the box has nothing for rescans the box, and a
# fan-out across 50+ cameras turns one cold night into a scan storm.
_ARCHIVE_NEGATIVE_TTL = 45  # seconds
# Budget for one on-demand night scan. _scan_night walks five subdirectories
# (ffthumbs alone is one file per FF), so 5 s was not enough for a busy night
# while the fleet's dawn uploads were hitting the same Storage Box -- and a
# scan that overran came back indistinguishable from an empty night.
_ARCHIVE_SCAN_TIMEOUT_S = 12.0

# Sentinel for "the storage box did not answer", as distinct from "this night
# genuinely holds no files". _with_sshfs_timeout returns its `default` on a
# timeout, on a shed call (SSHFS concurrency limit) and on an open circuit
# breaker; with default=None all three read as an empty night, which is how a
# slow box used to turn a whole camera's detections into "clip pending".
_SCAN_UNAVAILABLE = object()
# A camera that stopped uploading keeps the newest night that DID carry RMS
# plots resolved here, so the plot surfaces can still show it. Without it the
# only plot lookup a camera dark longer than _ARCHIVE_HOT_DAYS can serve is an
# on-demand storage-box walk per night per request -- which the surfaces
# (rightly) refuse to do, so they rendered "no data" instead of last week's
# plots. Re-resolved at most every _LATEST_RMS_TTL: the answer only changes
# when the camera resumes, and a resumed camera repopulates it from the hot
# window for free, so a slow TTL keeps the periodic refresher off the box.
_LATEST_RMS_TTL = 1800.0
# A night counts as plot-bearing only if it holds a renderable plot. RMS also
# writes FTPdetectinfo_*.txt and *_radiants.txt into the same dir, and a night
# that uploaded only those (a station that died mid-processing, or one running
# a bundle older than the fix that mirrors the whole check-plot set) has
# nothing any plot surface can display -- recording it would park the walk on a
# night that renders as blank, which is the failure this is here to prevent.
_RMS_PLOT_EXTS = (".png", ".jpg", ".jpeg", ".webp")
# Nights one resolution may scandir. Matches the resolvers' 30-night lookback
# in routes/archive.py -- a camera dark for months stops the walk rather than
# reading its whole history. Only rms/ is listed (a handful of files), and only
# for cameras with nothing in the hot window.
_LATEST_RMS_SCAN_NIGHTS = 30


def _has_plot_image(files) -> bool:
    """True when *files* holds at least one renderable RMS plot."""
    return any(str(f).lower().endswith(_RMS_PLOT_EXTS) for f in files)


class _ArchiveIndex:
    """Thread-safe in-memory index of the storagebox directory tree.

    Only the last ``_ARCHIVE_HOT_DAYS`` days keep full file listings
    in RAM. Older nights are recorded as date-only entries (no file
    lists) and scanned from SSHFS on demand with a short TTL cache.
    This keeps steady-state memory under ~20 MB instead of ~300 MB.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # Hot data: full file listings for recent nights only
        self._data: dict[str, dict[str, _NightIndex]] = {}
        # Lightweight date catalogue: cam -> set of all known date strings
        self._dates: dict[str, set[str]] = {}
        # On-demand cache for older nights: (cam, date) -> (expiry, _NightIndex)
        self._ondemand: dict[tuple[str, str], tuple[float, _NightIndex]] = {}
        # Tracks which dates have meteors (for nights_with_meteors)
        self._has_meteors: dict[str, set[str]] = {}
        # Newest plot-bearing night per camera, for cameras whose plots have
        # aged out of the hot window: cam -> (resolved_at, date, rms files).
        # ("" , frozenset()) is a resolved negative, TTL'd the same way so a
        # camera that has genuinely never uploaded plots is not rewalked on
        # every refresh tick.
        self._latest_rms: dict[str, tuple[float, str, frozenset[str]]] = {}
        # Timelapse files of EVERY night we have listed, hot or not:
        # cam -> date -> (timelapse/ mtime_ns, files). Tiny (a file or two per
        # night) and persisted, so the timelapses payload never has to scan
        # hundreds of old nights over sshfs after a restart; old nights are
        # revalidated by one stat of timelapse/ in the hourly build_full.
        self._timelapses: dict[str, dict[str, tuple[int, tuple[str, ...]]]] = {}
        self.ready = False
        # Bumped whenever night_files could not reach the box and answered
        # from a stale listing or not at all. A payload built across a bump
        # may show archived clips as pending, so it must not be kept.
        self.scan_unavailable = 0

    @staticmethod
    def _hot_cutoff() -> str:
        return (datetime.now(timezone.utc) - timedelta(days=_ARCHIVE_HOT_DAYS)).strftime("%Y%m%d")

    def in_hot_window(self, date: str) -> bool:
        """True when ``date`` is recent enough that its listings stay in
        memory; older nights are listed on demand over sshfs."""
        return date >= self._hot_cutoff()

    # -- read accessors (called from request threads) ---------------------

    def cameras(self) -> list[str]:
        with self._lock:
            return sorted(
                c for c in self._dates if _CAM_RE.match(c)
            )

    def nights(self, cam: str) -> list[str]:
        with self._lock:
            dates = self._dates.get(cam)
            if not dates:
                return []
            return sorted(dates, reverse=True)

    def nights_with_meteors(self, cam: str) -> list[str]:
        with self._lock:
            dates = self._has_meteors.get(cam)
            if not dates:
                return []
            return sorted(dates, reverse=True)

    def night_files(self, cam: str, date: str) -> _NightIndex | None:
        with self._lock:
            dates = self._data.get(cam)
            if dates:
                ni = dates.get(date)
                if ni is not None:
                    return ni
            # Check on-demand cache
            key = (cam, date)
            cached = self._ondemand.get(key)
            if cached and cached[0] > time.time():
                return cached[1]
            prev = cached[1] if cached else None
        # An old night this worker has not listed yet may have been listed by
        # another worker or before a restart: start from that copy.
        old = not self.in_hot_window(date)
        if prev is None and old:
            prev = _read_night_disk(cam, date)
        # Not in hot data or on-demand cache — revalidate (or scan) from SSHFS
        # with timeout. An expired entry whose signature still matches is
        # re-armed after six stats instead of a full listing.
        from station_client import _with_sshfs_timeout
        night_path = ARCHIVE_PATH / cam / date
        max_age_s = _SIG_MAX_AGE_OLD_S if old else _SIG_MAX_AGE_S
        result = _with_sshfs_timeout(
            lambda p=str(night_path): (
                self._revalidate_night(p, prev, max_age_s) if Path(p).is_dir() else None
            ),
            timeout=_ARCHIVE_SCAN_TIMEOUT_S,
            default=_SCAN_UNAVAILABLE,
        )
        if result is _SCAN_UNAVAILABLE:
            with self._lock:
                self.scan_unavailable += 1
            # Serve the last listing we had for this night rather than an
            # empty one. A stale listing costs at worst a clip that landed in
            # the last couple of minutes; an empty one tells every caller the
            # night is unarchived, which is what painted walls of "clip
            # pending" over nights whose clips were sitting on the box.
            with self._lock:
                stale = self._ondemand.get(key)
            return stale[1] if stale is not None else prev
        ni = result
        ttl = _ARCHIVE_ONDEMAND_TTL if ni is not None else _ARCHIVE_NEGATIVE_TTL
        with self._lock:
            self._ondemand[(cam, date)] = (time.time() + ttl, ni)
            if ni is not None:
                self._remember_timelapses_locked(cam, date, ni)
        if old and ni is not None and ni is not prev:
            _write_night_disk(cam, date, ni)
        return ni

    def night_files_for_listing(self, cam: str, date: str) -> _NightIndex | None:
        """``night_files`` for walks over a camera's whole history: the last
        known listing as is (memory, an expired on-demand entry, or an old
        night's disk copy), with no Storage Box re-check; only a night never
        listed before is scanned.

        One re-check is six stats (~0.25 s over SSHFS). nights_full walks every
        night, ~200 of them old, so the re-checks alone used up its 60 s budget
        and the Archive tab came up empty. An old night changes only when the
        janitor prunes it, and ``night_files`` still re-checks it for anything
        that opens that one night.
        """
        with self._lock:
            ni = (self._data.get(cam) or {}).get(date)
            if ni is None:
                cached = self._ondemand.get((cam, date))
                ni = cached[1] if cached else None
        if ni is None and not self.in_hot_window(date):
            ni = _read_night_disk(cam, date)
        return ni if ni is not None else self.night_files(cam, date)

    def all_night_files(self, cam: str, date: str) -> set[str]:
        ni = self.night_files(cam, date)
        if ni is None:
            return set()
        return set(ni.meteor_files) | set(ni.stack_files) | set(ni.timelapse_files)

    def rms_files_cached(self, cam: str, date: str) -> frozenset[str] | None:
        """RMS plot filenames for cam/date from MEMORY ONLY, else None.

        Unlike ``night_files``, this never falls through to an on-demand SSHFS
        scan. Walking back over nights to find the newest one with plots is a
        per-camera loop, and a page that renders every camera at once turns
        each miss into a storage-box round-trip -- which is how a single page
        load saturates the SSHFS concurrency guard and 504s. Callers use this
        for the walk and pay for at most one uncached lookup themselves.

        None means "unknown, not indexed" and is deliberately distinct from
        an empty set ("indexed, has no plots").
        """
        ni = self.night_files_cached(cam, date)
        return None if ni is None else ni.rms_files

    def night_files_cached(self, cam: str, date: str) -> _NightIndex | None:
        """The listing for cam/date from MEMORY ONLY (hot window or a live
        on-demand entry), else None. Never scans the box, for the same reason
        as ``rms_files_cached``: per-camera walks must stay in RAM."""
        with self._lock:
            ni = (self._data.get(cam) or {}).get(date)
            if ni is None:
                cached = self._ondemand.get((cam, date))
                if cached and cached[0] > time.time():
                    ni = cached[1]
            return ni

    def timelapse_files_known(self, cam: str, date: str) -> tuple[str, ...] | None:
        """Timelapse filenames for cam/date from MEMORY ONLY, else None
        (never listed). Unlike ``night_files`` this never scans the box, so a
        per-camera walk over every night stays in RAM."""
        with self._lock:
            ni = (self._data.get(cam) or {}).get(date)
            if ni is not None:
                return ni.timelapse_files
            entry = (self._timelapses.get(cam) or {}).get(date)
            return entry[1] if entry is not None else None

    def _remember_timelapses_locked(self, cam: str, date: str, ni: "_NightIndex") -> None:
        mtime = ni.sig[_SIG_TIMELAPSE] if ni.sig else 0
        self._timelapses.setdefault(cam, {})[date] = (mtime, ni.timelapse_files)

    def latest_rms_night(self, cam: str) -> tuple[str, frozenset[str]] | None:
        """Newest archived night that carries RMS plots for *cam*, or None.

        Memory only -- the walk that produced this answer ran in the index
        writers (``build_full`` / ``refresh_recent``), which are background
        threads, so the request path pays nothing. This is the last resort of
        the plot resolvers in ``routes/archive.py``: for a camera dark longer
        than ``_ARCHIVE_HOT_DAYS`` every candidate night is absent from
        ``rms_files_cached``, and without this they concluded "no data" while
        the plots sat in the archive.

        Deliberately NOT TTL-expired on read. An answer past its TTL is still
        the last plot night we know of, and dropping it would recreate exactly
        the blank card this exists to prevent; the TTL only decides when the
        writers may walk the box again.
        """
        with self._lock:
            entry = self._latest_rms.get(cam)
        if not entry:
            return None
        _resolved_at, date, files = entry
        if not date or not files:
            return None
        return date, files

    def rms_plot_file(self, cam: str, date: str, suffix: str) -> str | None:
        """Filename of an RMS plot (e.g. '_ff_intervals.png',
        '_calibration_variation.png') for cam/date, or None if not (yet)
        archived. Archive-local lookup only -- no station round-trip."""
        ni = self.night_files(cam, date)
        if ni is None:
            return None
        return next((f for f in ni.rms_files if f.endswith(suffix)), None)

    def ff_intervals_file(self, cam: str, date: str) -> str | None:
        return self.rms_plot_file(cam, date, '_ff_intervals.png')

    def is_built(self) -> bool:
        with self._lock:
            return len(self._dates) > 0

    # -- writers (called from background threads) -------------------------

    def build_full(self) -> int:
        """Full scan of ARCHIVE_PATH. Returns count of nights indexed."""
        if not ARCHIVE_PATH.exists():
            return 0
        try:
            with os.scandir(ARCHIVE_PATH) as it:
                cam_entries = [
                    e.name for e in it
                    if e.is_dir(follow_symlinks=False)
                    and _CAM_RE.match(e.name)
                    and e.name != "compilations"
                ]
        except OSError:
            logger.warning("archive index: cannot scan %s", ARCHIVE_PATH)
            return 0

        cutoff = self._hot_cutoff()
        new_data: dict[str, dict[str, _NightIndex]] = {}
        new_dates: dict[str, set[str]] = {}
        new_has_meteors: dict[str, set[str]] = {}
        new_timelapses: dict[str, dict[str, tuple[int, tuple[str, ...]]]] = {}
        total = 0

        def _scan_camera(cam: str) -> tuple[
            str, dict[str, _NightIndex], set[str], set[str],
            dict[str, tuple[int, tuple[str, ...]]],
        ]:
            cam_dir = ARCHIVE_PATH / cam
            hot: dict[str, _NightIndex] = {}
            all_dates: set[str] = set()
            meteor_dates: set[str] = set()
            with self._lock:
                prev_hot = dict(self._data.get(cam) or {})
                prev_tl = dict(self._timelapses.get(cam) or {})
            timelapses: dict[str, tuple[int, tuple[str, ...]]] = {}
            try:
                with os.scandir(cam_dir) as it:
                    for entry in it:
                        if not (entry.is_dir(follow_symlinks=False)
                                and _DATE_RE.match(entry.name)):
                            continue
                        all_dates.add(entry.name)
                        if entry.name >= cutoff:
                            ni = self._revalidate_night(
                                entry.path, prev_hot.get(entry.name)
                            )
                            if ni is not None:
                                hot[entry.name] = ni
                                timelapses[entry.name] = (
                                    ni.sig[_SIG_TIMELAPSE] if ni.sig else 0,
                                    ni.timelapse_files,
                                )
                                if any(f.endswith((".mkv", ".mp4"))
                                       for f in ni.meteor_files):
                                    meteor_dates.add(entry.name)
                        else:
                            known = self._old_night_timelapses(
                                entry.path, prev_tl.get(entry.name)
                            )
                            if known is not None:
                                timelapses[entry.name] = known
                            # Older night: just check if meteors/ has files
                            meteors_dir = os.path.join(entry.path, "meteors")
                            try:
                                with os.scandir(meteors_dir) as mit:
                                    for f in mit:
                                        if (f.is_file(follow_symlinks=False)
                                                and f.name.endswith(
                                                    (".mkv", ".mp4"))):
                                            meteor_dates.add(entry.name)
                                            break
                            except OSError:
                                pass
            except OSError:
                pass
            self._index_latest_rms(cam, hot, all_dates)
            return cam, hot, all_dates, meteor_dates, timelapses

        with ThreadPoolExecutor(max_workers=4) as pool:
            for cam, hot, all_dates, meteor_dates, timelapses in pool.map(
                lambda c: _scan_camera(c), cam_entries
            ):
                new_data[cam] = hot
                new_dates[cam] = all_dates
                new_has_meteors[cam] = meteor_dates
                new_timelapses[cam] = timelapses
                total += len(all_dates)

        with self._lock:
            self._data = new_data
            self._dates = new_dates
            self._has_meteors = new_has_meteors
            self._timelapses = new_timelapses
            self._ondemand.clear()
            if total:
                self.ready = True
        return total

    def refresh_recent(self, days: int = 3) -> int:
        """Re-scan only dates >= cutoff. Also discovers new cameras and dates.
        Parallelized across cameras like build_full. Updates applied atomically
        per batch so readers never see partial state."""
        if not ARCHIVE_PATH.exists():
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y%m%d")
        try:
            with os.scandir(ARCHIVE_PATH) as it:
                cam_entries = [
                    e.name for e in it
                    if e.is_dir(follow_symlinks=False)
                    and _CAM_RE.match(e.name)
                    and e.name != "compilations"
                ]
        except OSError:
            return 0

        def _refresh_cam(cam: str) -> tuple[
            dict[str, _NightIndex], set[str], set[str]
        ]:
            cam_dir = ARCHIVE_PATH / cam
            updates: dict[str, _NightIndex] = {}
            new_dates: set[str] = set()
            meteor_dates: set[str] = set()
            with self._lock:
                prev_hot = dict(self._data.get(cam) or {})
            try:
                with os.scandir(cam_dir) as it:
                    for entry in it:
                        if not (entry.is_dir(follow_symlinks=False)
                                and _DATE_RE.match(entry.name)):
                            continue
                        new_dates.add(entry.name)
                        if entry.name < cutoff:
                            continue
                        ni = self._revalidate_night(
                            entry.path, prev_hot.get(entry.name)
                        )
                        if ni is not None:
                            updates[entry.name] = ni
                            if any(f.endswith((".mkv", ".mp4"))
                                   for f in ni.meteor_files):
                                meteor_dates.add(entry.name)
            except OSError:
                pass
            # Keeps a camera that stopped uploading mid-week resolvable: the
            # refresher only rescans the hot window, so once its last plots
            # age out of that window this memo is the only record of them.
            self._index_latest_rms(cam, updates, new_dates)
            return updates, new_dates, meteor_dates

        all_updates: dict[str, tuple[
            dict[str, _NightIndex], set[str], set[str]
        ]] = {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            future_to_cam = {pool.submit(_refresh_cam, c): c for c in cam_entries}
            for fut in as_completed(future_to_cam):
                cam = future_to_cam[fut]
                try:
                    result = fut.result()
                    all_updates[cam] = result
                except Exception:
                    pass

        count = 0
        hot_cutoff = self._hot_cutoff()
        with self._lock:
            for cam, (updates, new_dates, meteor_dates) in all_updates.items():
                if cam not in self._data:
                    self._data[cam] = {}
                if cam not in self._dates:
                    self._dates[cam] = set()
                if cam not in self._has_meteors:
                    self._has_meteors[cam] = set()
                self._data[cam].update(updates)
                self._dates[cam] |= new_dates
                self._has_meteors[cam] |= meteor_dates
                for date, ni in updates.items():
                    self._remember_timelapses_locked(cam, date, ni)
                count += len(updates)
            # Evict stale hot entries
            for cam in list(self._data):
                self._data[cam] = {
                    d: ni for d, ni in self._data[cam].items()
                    if d >= hot_cutoff
                }
            # Evict on-demand entries once past their retention (not their
            # TTL): an expired entry is no longer served, but its signature
            # lets the next lookup re-arm it with stats instead of a listing.
            keep_after = time.time() - _ARCHIVE_ONDEMAND_RETAIN_S
            self._ondemand = {
                k: v for k, v in self._ondemand.items() if v[0] > keep_after
            }
        return count

    # -- internal helpers -------------------------------------------------

    @staticmethod
    def _scan_rms_dir(night_path: str) -> frozenset[str]:
        """List just ``rms/`` under a night dir. Empty set when absent.

        Deliberately narrower than ``_scan_night``: resolving a dark camera's
        newest plot night must not pay for listing that night's ``meteors/``
        and ``stacks/`` dirs, which hold ~1400 entries each.
        """
        try:
            with os.scandir(os.path.join(night_path, "rms")) as it:
                return frozenset(
                    f.name for f in it if f.is_file(follow_symlinks=False)
                )
        except OSError:
            return frozenset()

    def _index_latest_rms(
        self, cam: str, hot: dict[str, _NightIndex], all_dates: set[str]
    ) -> None:
        """Resolve and record ``cam``'s newest plot-bearing night.

        Called from the index writers only (background threads). Free when the
        camera is uploading -- the answer is already in the freshly scanned hot
        nights. It only reaches the storage box for a camera whose plots have
        aged out of the hot window, and then at most once per
        ``_LATEST_RMS_TTL`` and at most ``_LATEST_RMS_SCAN_NIGHTS`` cheap
        ``rms/`` listings.
        """
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        hottest = max(
            (d for d, ni in hot.items()
             if d <= today and _has_plot_image(ni.rms_files)),
            default="",
        )
        if hottest:
            with self._lock:
                self._latest_rms[cam] = (
                    time.time(), hottest, hot[hottest].rms_files,
                )
            return

        with self._lock:
            entry = self._latest_rms.get(cam)
            if entry is not None:
                resolved_at, date, _files = entry
                # A resolution still inside its TTL stands. Nothing in the hot
                # window can beat it (there are no plots there at all), and
                # rewalking a dark camera on every 60 s refresh tick is exactly
                # the storage-box churn the memo exists to avoid. A recorded
                # night that has since been pruned off the box is the one case
                # worth re-resolving early.
                if (time.time() - resolved_at < _LATEST_RMS_TTL
                        and (not date or date in all_dates)):
                    return

        for date in sorted(
            (d for d in all_dates if d <= today), reverse=True
        )[:_LATEST_RMS_SCAN_NIGHTS]:
            files = self._scan_rms_dir(str(ARCHIVE_PATH / cam / date))
            if _has_plot_image(files):
                with self._lock:
                    self._latest_rms[cam] = (time.time(), date, files)
                return

        with self._lock:
            self._latest_rms[cam] = (time.time(), "", frozenset())

    def _revalidate_night(
        self, night_path: str, prev: _NightIndex | None,
        max_age_s: float | None = None,
    ) -> _NightIndex | None:
        """``prev`` if the night provably has not changed since it was listed
        (trusted, identical signature), else a fresh listing."""
        if prev is not None and _signature_trusted(prev, time.time(), max_age_s):
            if _night_signature(night_path) == prev.sig:
                return prev
        return self._scan_night(night_path)

    @staticmethod
    def _old_night_timelapses(
        night_path: str, prev: tuple[int, tuple[str, ...]] | None
    ) -> tuple[int, tuple[str, ...]] | None:
        """(mtime, files) of an old night's timelapse/ dir: one stat when it is
        unchanged since ``prev``, one small listing when not. None only when
        the box did not answer and nothing was known before."""
        path = os.path.join(night_path, "timelapse")
        try:
            mtime = os.stat(path).st_mtime_ns
        except FileNotFoundError:
            return (0, ())
        except OSError:
            return prev
        if prev is not None and prev[0] == mtime:
            return prev
        try:
            with os.scandir(path) as it:
                files = tuple(sorted(
                    f.name for f in it if f.is_file(follow_symlinks=False)
                ))
        except OSError:
            return prev
        return (mtime, files)

    @staticmethod
    def _scan_night(night_path: str) -> _NightIndex | None:
        """Scan meteors/, stacks/, timelapse/, ffthumbs/ under a night dir.

        The signature is taken BEFORE listing, so a write that lands during
        the listing moves the mtime past the recorded one and the next
        revalidation lists the night again."""
        scanned_at = time.time()
        sig = _night_signature(night_path)
        meteor_files: list[str] = []
        stack_files: list[str] = []
        timelapse_files: list[str] = []
        ffthumb_files: list[str] = []
        rms_files: list[str] = []
        for subdir, target in zip(_NIGHT_SUBDIRS, (
            meteor_files, stack_files, timelapse_files, ffthumb_files, rms_files,
        )):
            path = os.path.join(night_path, subdir)
            try:
                with os.scandir(path) as it:
                    for f in it:
                        if f.is_file(follow_symlinks=False):
                            target.append(f.name)
            except OSError:
                continue
        if (not meteor_files and not stack_files and not timelapse_files
                and not ffthumb_files and not rms_files):
            return None
        state_chunks: dict | None = None
        rms_complete: bool | None = None
        morning_done: bool | None = None
        stage_times: dict | None = None
        timelapse_done: bool | None = None
        state_path = os.path.join(night_path, "state.json")
        try:
            with open(state_path) as fh:
                state = json.load(fh)
            state_chunks = state.get("chunks", {})
            # Same reading as routes.detections._night_rms_complete: a
            # state.json without the key counts as not complete.
            rms_complete = bool(state.get("rms_complete"))
            morning_done = bool(state.get("morning_done"))
            timelapse_done = bool(state.get("timelapse_done"))
            if isinstance(state.get("stage_times"), dict):
                stage_times = state["stage_times"]
        except (OSError, json.JSONDecodeError, KeyError, AttributeError):
            pass
        return _NightIndex(
            meteor_files=tuple(sorted(meteor_files)),
            stack_files=frozenset(stack_files),
            timelapse_files=tuple(sorted(timelapse_files)),
            state_chunks=state_chunks,
            ffthumb_files=frozenset(ffthumb_files),
            rms_files=frozenset(rms_files),
            sig=sig,
            scanned_at=scanned_at,
            rms_complete=rms_complete,
            morning_done=morning_done,
            stage_times=stage_times,
            timelapse_done=timelapse_done,
        )

    def save_to_disk(self, path: Path) -> None:
        """Persist only hot data + date catalogue for fast startup."""
        data: dict[str, dict[str, dict[str, list[str]]]] = {}
        with self._lock:
            for cam, nights in self._data.items():
                data[cam] = {}
                for date, ni in nights.items():
                    data[cam][date] = _night_entry(ni)
            # Persist date catalogue and meteor flags as lightweight entries
            for cam, dates in self._dates.items():
                if cam not in data:
                    data[cam] = {}
                meteor_dates = self._has_meteors.get(cam, set())
                known_tl = self._timelapses.get(cam, {})
                for d in dates:
                    if d not in data[cam]:
                        entry: dict[str, Any] = {"m": [], "s": [], "t": []}
                        if d in meteor_dates:
                            entry["_meteor"] = []
                        # Old night's timelapse listing ("tg" = its mtime).
                        # Absent "tg" means never listed, distinct from "none".
                        tl = known_tl.get(d)
                        if tl is not None:
                            entry["t"] = list(tl[1])
                            entry["tg"] = tl[0]
                        data[cam][d] = entry
            # Newest plot-bearing night per dark camera. Without persisting it,
            # a warm boot (which skips build_full entirely) starts with no
            # record of the plots of every camera that stopped uploading more
            # than _ARCHIVE_HOT_DAYS ago -- i.e. the plot surfaces go back to
            # "no data" after every restart, which is how they got there. Only
            # positives are written; a negative costs one bounded rewalk.
            latest_rms = {
                cam: {"d": date, "r": sorted(files)}
                for cam, (_ts, date, files) in self._latest_rms.items()
                if date and files
            }
        # Per-process temp name. The save is leader-gated to one worker, but a
        # shared ".tmp" is only safe while that stays true — two writers on one
        # temp path corrupt each other's file and surface as "failed to save".
        # Making the name unique means the rename stays atomic per writer
        # regardless, and a crashed save leaves a stale temp rather than a
        # half-written index.
        tmp = path.with_suffix(f".tmp.{os.getpid()}")
        # Reserved top-level key: camera names are [A-Z0-9]+, so it can never
        # collide with one, and load_from_disk pops it before walking cameras.
        payload: dict[str, Any] = dict(data)
        if latest_rms:
            payload["__latest_rms__"] = latest_rms
        try:
            write_atomic(path, json.dumps(payload, separators=(",", ":")), tmp=tmp, mkdir=True)
        except OSError:
            logger.warning("archive index: failed to save to %s", path)

    def load_from_disk(self, path: Path) -> int:
        """Load index from JSON. Only recent nights get full file lists."""
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return 0
        cutoff = self._hot_cutoff()
        new_data: dict[str, dict[str, _NightIndex]] = {}
        new_dates: dict[str, set[str]] = {}
        new_has_meteors: dict[str, set[str]] = {}
        new_timelapses: dict[str, dict[str, tuple[int, tuple[str, ...]]]] = {}
        total = 0
        raw_latest = raw.pop("__latest_rms__", None) if isinstance(raw, dict) else None
        new_latest_rms: dict[str, tuple[float, str, frozenset[str]]] = {}
        if isinstance(raw_latest, dict):
            now = time.time()
            for cam, entry in raw_latest.items():
                if not isinstance(entry, dict):
                    continue
                date, files = entry.get("d"), entry.get("r")
                if isinstance(date, str) and isinstance(files, list) and files:
                    new_latest_rms[cam] = (now, date, frozenset(files))
        for cam, nights in raw.items():
            new_data[cam] = {}
            new_dates[cam] = set()
            new_has_meteors[cam] = set()
            cam_tl: dict[str, tuple[int, tuple[str, ...]]] = {}
            new_timelapses[cam] = cam_tl
            for date, files in nights.items():
                new_dates[cam].add(date)
                total += 1
                has_meteor_flag = "_meteor" in files
                m_files = files.get("m", ())
                if has_meteor_flag or any(
                    f.endswith((".mkv", ".mp4")) for f in m_files
                ):
                    new_has_meteors[cam].add(date)
                # "r" (RMS plots) counts toward loading a night, not just toward
                # populating one: a quiet night can have plots and nothing else,
                # and leaving it out of _data entirely is the same failure as
                # loading it with an empty plot set.
                sig = _parse_sig(files.get("g"))
                if sig is not None:
                    cam_tl[date] = (sig[_SIG_TIMELAPSE], tuple(files.get("t", ())))
                elif isinstance(files.get("tg"), int):
                    cam_tl[date] = (files["tg"], tuple(files.get("t", ())))
                if date >= cutoff and (m_files or files.get("s") or files.get("t")
                                       or files.get("f") or files.get("r")):
                    new_data[cam][date] = _night_from_entry(files)
        with self._lock:
            self._data = new_data
            self._dates = new_dates
            self._has_meteors = new_has_meteors
            self._timelapses = new_timelapses
            # Restored entries are treated as freshly resolved: they are
            # already correct, and a camera that resumed while the dashboard
            # was down overwrites its entry for free on the next refresh
            # (its plots land back in the hot window).
            self._latest_rms.update(new_latest_rms)
            if total:
                self.ready = True
        return total
