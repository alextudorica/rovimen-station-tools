"""GMN meteor-trajectory data fetcher — replaces the Datasette SQL endpoint.

Background
==========
The previous integration hit ``https://explore.globalmeteornetwork.org/
gmn_data_store/-/query.json?sql=…`` (Datasette over a 12 GB SQLite). Every
query started timing out for us in May 2026 — even ``SELECT 1``. The
Datasette homepage stays 200, so it's a backend / lock-contention issue
on the table-query path, not a ban or a network problem.

The GMN's actual documented public API is a tree of static text files at
``https://globalmeteornetwork.org/data/traj_summary_data/``, listed on
the public Data page. The schema is fully documented in
``GMN_orbit_data_columns.pdf``. Files are semicolon-separated, with
``#``-prefixed header lines; column 0 is the trajectory identifier, the
last column is the comma-separated participating-station list, and the
IAU shower code is inline (no secondary lookup table needed).

This module:

1. Maintains a local on-disk cache at ``ROVIMEN_GMN_CACHE_DIR`` (default
   ``/opt/rovimen/gmn_cache/``) of recent monthly + yearly files.
2. Re-fetches with ``If-Modified-Since`` (so we get a 304 most of the
   time) up to once an hour for a month that is still gaining nights,
   and once a day for one that is settled. One download per month per
   host: writers take an ``flock`` and stage into a private temp file.
3. Parses the semicolon format into the slim event dict the dashboard
   already consumes, tagged with ``has_ro`` (RO* witness) and ``de_only``
   (only our DEZ0DQ/DEZ0S7/DEZ132 witnesses) so the overview overlay keeps working.

The output dict shape is bit-identical to the old Datasette-based
``_gmn_fetch_multistation()`` so the rest of the dashboard is unchanged.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import requests

from atomic_write import write_atomic

try:
    import fcntl
except ImportError:  # non-POSIX host — the download gate degrades to
    fcntl = None     # per-process locking only

logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────

BASE_URL = "https://globalmeteornetwork.org/data/traj_summary_data"
CACHE_DIR = Path(os.environ.get("ROVIMEN_GMN_CACHE_DIR", "/opt/rovimen/gmn_cache"))
# The current month (and, while GMN's batch catches up, the one before
# it) still gains nights, so they keep the hourly re-check. A month that
# is over is settled: re-validating it hourly meant every upstream
# regeneration of an old file cost a full re-download, so each worker
# recycle pulled back ~1 GB of history it already had on disk.
CACHE_TTL_SECONDS = 3600          # open months: re-check upstream every hour
CLOSED_MONTH_TTL_SECONDS = 86400  # settled months: once a day is plenty
HTTP_TIMEOUT = (10, 60)   # (connect, read)
USER_AGENT = "rovimen-dashboard/1.0 (+https://dashboard.rovimen.org)"

# Only our German stations get the dashboard's "de_only" tag — listing
# them explicitly so the overlay keeps focusing on Berlin gear rather
# than picking up the thousands of meteors a night from unrelated DE
# sites. RO* stays a wildcard since we operate the whole RO prefix.
DE_STATION_CODES = frozenset(("DE0018", "DE001B", "DE001C", "DE001D"))

_fetch_locks: dict[str, threading.Lock] = {}
_fetch_locks_guard = threading.Lock()


def _lock_for_month(month_key: str) -> threading.Lock:
    with _fetch_locks_guard:
        if month_key not in _fetch_locks:
            _fetch_locks[month_key] = threading.Lock()
        return _fetch_locks[month_key]


# ── Column layout (zero-based) ─────────────────────────────────────────
# Indices verified against a live 2026-05-20 row and the header banner
# documented in ``GMN_orbit_data_columns.pdf``.

_COL_ID         = 0   # Unique trajectory identifier (e.g. 20260520084811_0vpb5)
_COL_JD         = 1   # Julian date (Beginning)
_COL_TIME_UTC   = 2   # 'YYYY-MM-DD HH:MM:SS.ffffff' (Beginning UTC)
_COL_IAU_NO     = 3
_COL_IAU_CODE   = 4   # Shower 3-letter code or "..." when unidentified
_COL_SOL_LON    = 5   # Solar longitude (deg)
_COL_RA_GEO     = 7   # Geocentric RA of radiant (deg)
_COL_DEC_GEO    = 9   # Geocentric Dec of radiant (deg)
_COL_VGEO       = 15  # Vgeo km/s
_COL_VHEL       = 21  # Vhel km/s (heliocentric)
_COL_A          = 23  # Semi-major axis (AU)
_COL_E          = 25  # Eccentricity
_COL_I          = 27  # Inclination (deg)
_COL_PERI       = 29  # Argument of perihelion (deg)
_COL_NODE       = 31  # Longitude of ascending node (deg)
_COL_Q          = 37  # Perihelion distance (AU)
_COL_Q_APH      = 43  # Aphelion distance Q (AU)
_COL_TISSERAND  = 49  # Tisserand parameter w.r.t Jupiter
_COL_VINIT      = 59  # Initial (pre-atmospheric) velocity km/s
_COL_VAVG       = 61  # Average velocity along the trajectory km/s
_COL_LAT_BEG    = 63
_COL_LON_BEG    = 65
_COL_HT_BEG     = 67  # km
_COL_LAT_END    = 69
_COL_LON_END    = 71
_COL_HT_END     = 73  # km
_COL_DURATION   = 75  # Duration (sec)
_COL_PEAK_MAG   = 76  # Peak absolute magnitude
_COL_PEAK_HT    = 77  # Height of peak brightness (km)
_COL_F_PARAM    = 78  # F parameter (where along the path the meteor peaked)
_COL_MASS       = 79  # Photometric mass (kg, tau=0.7%)
_COL_QC         = 80  # Convergence angle of the trajectory solution (deg)
_COL_NUM_STAT   = 84
_COL_STATIONS   = 85  # comma-separated participating station list

_EXPECTED_COL_COUNT = 86

# Trajectory IDs are stamped 'YYYYMMDD<rest>'. That lets us cheaply
# bucket rows by night from the ID prefix without parsing each row's UTC
# timestamp.
_ID_DATE_PREFIX_RE = re.compile(r"^(\d{8})")


# ── Fetch + cache ──────────────────────────────────────────────────────

def _cache_path_for_month(year: int, month: int) -> Path:
    return CACHE_DIR / f"traj_summary_monthly_{year:04d}{month:02d}.txt"


def _monthly_url(year: int, month: int) -> str:
    return f"{BASE_URL}/monthly/traj_summary_monthly_{year:04d}{month:02d}.txt"


def _is_fresh(path: Path, ttl: float = CACHE_TTL_SECONDS) -> bool:
    """True if ``path`` exists and was fetched/refreshed within ``ttl``."""
    try:
        return path.exists() and (time.time() - path.stat().st_mtime) < ttl
    except OSError:
        return False


def _ttl_for_month(year: int, month: int, now: datetime | None = None) -> float:
    """Freshness budget for one monthly file — see CACHE_TTL_SECONDS."""
    now = now or datetime.now(timezone.utc)
    prev = now.replace(day=1) - timedelta(days=1)
    open_months = {(now.year, now.month), (prev.year, prev.month)}
    if (year, month) in open_months:
        return CACHE_TTL_SECONDS
    return CLOSED_MONTH_TTL_SECONDS


def _lock_path_for(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


def _tmp_download_path(path: Path) -> Path:
    """Private staging path for one writer's download.

    Every writer used to stage into the same ``<name>.tmp``; with three
    gunicorn workers warming the cache at once their writes interleaved
    into that single file and whoever renamed last published the mixture.
    pid + thread id gives each writer its own staging file.
    """
    return path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")


# A download that is killed mid-stream (a deploy restarting the service is
# the common case) cannot clean up after itself, so its staging file is
# orphaned. They are per-writer now, so they accumulate one per interrupted
# attempt instead of being overwritten — on a 4 GB VPS a handful of
# half-written monthly files is real disk.
_STAGING_ORPHAN_AGE_S = 600


def _sweep_orphaned_staging(path: Path) -> None:
    """Remove staging files left by writers that died mid-download.

    Only ever called while holding the download gate, so no other process is
    staging this month; the age floor is the belt to that braces, and covers
    the host where ``fcntl`` is unavailable. Also reclaims the pre-rename
    ``<stem>.tmp`` files left behind by the shared-staging era.
    """
    cutoff = time.time() - _STAGING_ORPHAN_AGE_S
    candidates = list(path.parent.glob(path.name + ".*.tmp"))
    candidates.append(path.with_suffix(".tmp"))   # legacy shared staging name
    for stale in candidates:
        try:
            if stale.exists() and stale.stat().st_mtime < cutoff:
                stale.unlink()
                logger.info("GMN: reclaimed orphaned staging file %s", stale.name)
        except OSError:
            continue   # racing cleanup or a permission wrinkle — not worth failing over


@contextmanager
def _download_gate(path: Path) -> Iterator[None]:
    """Hold an exclusive inter-process lock on one monthly file.

    Threads within a worker are already serialised by ``_lock_for_month``;
    this is the cross-worker half, so a set of gunicorn workers starting
    together downloads a given month once and the rest wait and reuse it
    instead of each pulling the same ~110 MB.
    """
    if fcntl is None:
        yield
        return
    with open(_lock_path_for(path), "a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def ensure_monthly_cached(year: int, month: int) -> Path:
    """Return a local path to the monthly traj_summary file, fetching if
    the cached copy is missing or older than this month's TTL (see
    ``_ttl_for_month``).

    Uses ``If-Modified-Since`` so subsequent fetches that are within the
    TTL but the upstream hasn't republished only burn a HEAD-like 304 —
    much friendlier than re-downloading 45 MB every hour.

    Raises ``requests.RequestException`` on hard upstream failure (so
    callers can decide whether to surface "GMN data unavailable" to the
    UI). Returns the local file path on success.
    """
    path = _cache_path_for_month(year, month)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    ttl = _ttl_for_month(year, month)
    if _is_fresh(path, ttl):
        return path

    url = _monthly_url(year, month)
    month_key = f"{year:04d}{month:02d}"
    with _lock_for_month(month_key), _download_gate(path):
        # Recheck under both locks: the sibling thread — or the sibling
        # gunicorn worker — we queued behind may have just fetched it.
        if _is_fresh(path, ttl):
            return path
        headers = {"User-Agent": USER_AGENT}
        if path.exists():
            # Conditional GET — server returns 304 if no new meteors since.
            # Read the mtime here rather than before the gate: whoever held
            # it may have replaced the file while we waited.
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            headers["If-Modified-Since"] = mtime.strftime("%a, %d %b %Y %H:%M:%S GMT")
        logger.info("GMN: fetching %s", url)
        resp = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT, stream=True)
        if resp.status_code == 304:
            os.utime(path, None)  # bump mtime so TTL resets
            logger.info("GMN: 304 not modified — cache reused")
            return path
        resp.raise_for_status()
        _sweep_orphaned_staging(path)
        tmp = _tmp_download_path(path)
        try:
            with open(tmp, "wb") as f:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        f.write(chunk)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
        logger.info("GMN: downloaded %s (%d bytes)", path.name, path.stat().st_size)
        return path


# ── Parse ──────────────────────────────────────────────────────────────

_FLOAT_NA = (None, "", "nan", "NaN", "...")


def _maybe_float(s: str) -> float | None:
    s = (s or "").strip()
    if s in _FLOAT_NA:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _split_stations(raw: str) -> list[str]:
    if not raw:
        return []
    return [s.strip() for s in raw.replace(";", ",").split(",") if s.strip()]


def parse_traj_summary(path: Path) -> Iterator[dict[str, Any]]:
    """Yield one slim event dict per data row in a GMN traj_summary file.

    Skips comment lines (``#``-prefixed) and any row whose column count
    doesn't match the documented schema (silently — the GMN file format
    is stable, so a mismatch is almost always corrupt data).

    This is the raw, whole-file walk. Anything that runs per request or per
    worker boot must go through :func:`_month_index` instead, which parses
    each monthly file once and shares the result across workers on disk.
    """
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line or line[0] == "#":
                continue
            cells = [c.strip() for c in line.rstrip("\n").split(";")]
            if len(cells) < _EXPECTED_COL_COUNT:
                continue  # malformed row
            stations = _split_stations(cells[_COL_STATIONS])
            if len(stations) < 2:
                continue  # single-station rows aren't of dashboard interest
            yield _row_to_event(cells, stations)


def _row_to_event(cells: list[str], stations: list[str]) -> dict[str, Any]:
    """Project one already-split, already-stripped traj_summary row onto the
    slim event dict the dashboard uses everywhere."""
    iau_code = cells[_COL_IAU_CODE].strip()
    if iau_code in ("...", ""):
        iau_code = None
    return {
        "id":             cells[_COL_ID],
        "time":           cells[_COL_TIME_UTC],
        "lat_begin":      _maybe_float(cells[_COL_LAT_BEG]),
        "lon_begin":      _maybe_float(cells[_COL_LON_BEG]),
        "lat_end":        _maybe_float(cells[_COL_LAT_END]),
        "lon_end":        _maybe_float(cells[_COL_LON_END]),
        "altitude_begin": _maybe_float(cells[_COL_HT_BEG]),
        "altitude_end":   _maybe_float(cells[_COL_HT_END]),
        "peak_mag":       _maybe_float(cells[_COL_PEAK_MAG]),
        "shower":         iau_code,
        "velocity":       _maybe_float(cells[_COL_VGEO]),
        # Velocity trio: what it hit the atmosphere at (Vinit), what it
        # averaged over the visible path (Vavg), and the two derived
        # frames (Vgeo above, Vhel below).
        "velocity_init":  _maybe_float(cells[_COL_VINIT]),
        "velocity_avg":   _maybe_float(cells[_COL_VAVG]),
        "velocity_hel":   _maybe_float(cells[_COL_VHEL]),
        "peak_height":    _maybe_float(cells[_COL_PEAK_HT]),
        "f_param":        _maybe_float(cells[_COL_F_PARAM]),
        "mass_kg":        _maybe_float(cells[_COL_MASS]),
        "convergence":    _maybe_float(cells[_COL_QC]),
        "duration_s":     _maybe_float(cells[_COL_DURATION]),
        "sol_lon":        _maybe_float(cells[_COL_SOL_LON]),
        "ra_geo":         _maybe_float(cells[_COL_RA_GEO]),
        "dec_geo":        _maybe_float(cells[_COL_DEC_GEO]),
        # Orbital elements (heliocentric)
        "orbit_a":        _maybe_float(cells[_COL_A]),
        "orbit_e":        _maybe_float(cells[_COL_E]),
        "orbit_i":        _maybe_float(cells[_COL_I]),
        "orbit_peri":     _maybe_float(cells[_COL_PERI]),
        "orbit_node":     _maybe_float(cells[_COL_NODE]),
        "orbit_q":        _maybe_float(cells[_COL_Q]),
        "orbit_q_aph":    _maybe_float(cells[_COL_Q_APH]),
        "tisserand":      _maybe_float(cells[_COL_TISSERAND]),
        "stations":       stations,
    }


# ── Per-month derived index: parse each monthly file ONCE ──────────────
#
# Everything the dashboard wants from a monthly traj_summary file is a small
# projection of it: which nights it covers, how many multi-station orbits
# each night has, how many of those an RO camera witnessed, and the full
# event dicts for the orbits one of OUR cameras took part in. The file itself
# is 50-110 MB of semicolon text and a pure-Python walk of it costs tens of
# seconds of CPU. Until 2026-09 every consumer walked it independently — the
# poller once per backfilled DATE (33 walks per worker boot), the orbit
# counter once per month, the trajectory join once per cold request — and
# with three gunicorn workers recycling every few minutes the VPS spent most
# of its CPU re-parsing the same bytes (measured: ~160 s CPU per worker boot).
#
# ``_month_index`` builds all of those projections in ONE pass, keeps the
# result in memory keyed on the file's content, and persists it as JSON next
# to the monthly file so the other workers — and every recycled worker after
# them — load a few MB of JSON instead of parsing 110 MB of text. A settled
# month is therefore parsed once per VPS, ever; the open month once per
# upstream regeneration. Cross-worker builds are serialised on a file lock
# (the same pattern ``ensure_monthly_cached`` uses for downloads).

_INDEX_FORMAT = 1
# Station-code prefixes that make a trajectory "ours" for indexing purposes.
# RO is the ROVIMEN fleet; MD is Cahul (gmnmd01). Berlin cameras are listed
# explicitly in DE_STATION_CODES so the thousands of unrelated German
# meteors a night stay out (see that constant).
_OUR_PREFIXES = ("RO", "MD")
# Non-RO/MD cameras of ours that are NOT in DE_STATION_CODES (that set drives
# the overlay's de_only flag and is deliberately narrow). They still need
# their trajectories indexed for the join and the public orbits feed. Add a
# code here when a camera with a new prefix joins the fleet.
_INDEX_EXTRA_CODES = frozenset({"DE001E"})
# Months held fully in memory per worker (events + station lists). Orbit
# counting touches 13 months but only needs the small per-day count maps,
# which are cached separately; the full index is only hot for the months the
# poller and the trajectory join actually read.
_MONTH_INDEX_KEEP = 3

_month_index_cache: "OrderedDict[str, tuple[str, dict[str, Any]]]" = OrderedDict()
_month_index_lock = threading.Lock()
_index_build_locks: dict[str, threading.Lock] = {}
_index_build_locks_guard = threading.Lock()


def _lock_for_index(month_key: str) -> threading.Lock:
    with _index_build_locks_guard:
        if month_key not in _index_build_locks:
            _index_build_locks[month_key] = threading.Lock()
        return _index_build_locks[month_key]


def _index_path_for_month(year: int, month: int) -> Path:
    # Next to the monthly file it derives from (not a fixed CACHE_DIR join), so
    # it follows the file wherever the resolver puts it — tests point
    # _cache_path_for_month at fixtures, and the dev tier at its own cache dir.
    src = _cache_path_for_month(year, month)
    return src.with_name(f"{src.stem}.index.v{_INDEX_FORMAT}.json")


def _source_key(path: Path) -> str:
    """Content identity of a monthly file: size + blake2b of its bytes.

    ``ensure_monthly_cached`` bumps the file's mtime on every 304 so the TTL
    resets, which made every mtime-keyed cache re-parse an unchanged file
    hourly. Hashing 110 MB costs ~0.2 s; parsing it costs ~40 s.
    """
    h = hashlib.blake2b(digest_size=16)
    size = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
            size += len(chunk)
    return f"{size}:{h.hexdigest()}"


def _is_ours(stations_upper: list[str]) -> bool:
    return any(
        s.startswith(_OUR_PREFIXES) or s in DE_STATION_CODES or s in _INDEX_EXTRA_CODES
        for s in stations_upper
    )


def _build_month_index(path: Path) -> dict[str, Any]:
    """One pass over a monthly file producing every projection we serve."""
    all_dates: set[str] = set()
    totals: dict[str, int] = {}
    ro_counts: dict[str, int] = {}
    station_lists: dict[str, list[list[str]]] = {}
    events: dict[str, list[dict[str, Any]]] = {}
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line or line[0] == "#":
                continue
            cells = [c.strip() for c in line.rstrip("\n").split(";")]
            m = _ID_DATE_PREFIX_RE.match(cells[0])
            if not m:
                continue
            date_key = m.group(1)
            all_dates.add(date_key)
            if len(cells) < _EXPECTED_COL_COUNT:
                continue  # malformed row
            stations = _split_stations(cells[_COL_STATIONS])
            if len(stations) < 2:
                continue  # single-station rows aren't of dashboard interest
            totals[date_key] = totals.get(date_key, 0) + 1
            station_lists.setdefault(date_key, []).append(stations)
            upper = [s.upper() for s in stations]
            has_ro = any(s.startswith("RO") for s in upper)
            if has_ro:
                ro_counts[date_key] = ro_counts.get(date_key, 0) + 1
            if not _is_ours(upper):
                continue
            ev = _row_to_event(cells, stations)
            has_de = bool(set(upper) & DE_STATION_CODES)
            ev["has_ro"] = has_ro
            ev["has_de"] = has_de
            ev["de_only"] = has_de and not has_ro
            events.setdefault(date_key, []).append(ev)
    return {
        "format": _INDEX_FORMAT,
        "all_dates": sorted(all_dates),
        "totals": totals,
        "ro_counts": ro_counts,
        "station_lists": station_lists,
        "events": events,
    }


def _load_index_file(index_path: Path, source_key: str) -> dict[str, Any] | None:
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("format") != _INDEX_FORMAT:
        return None
    if data.get("source_key") != source_key:
        return None
    return data


def _write_index_file(index_path: Path, index: dict[str, Any]) -> None:
    try:
        write_atomic(index_path, json.dumps(index, separators=(",", ":")),
                     tmp=index_path.with_name(f"{index_path.name}.{os.getpid()}.tmp"),
                     encoding="utf-8")
    except OSError as exc:
        logger.warning("GMN: could not persist month index %s: %s", index_path.name, exc)


# month_key -> (mtime, source_key). Kept OUTSIDE the bounded index LRU so an
# unchanged file is never re-hashed: hashing 110 MB costs ~0.2 s, and the
# derived per-month memos below (counts, traj join, night set) only need the
# source key to know they are still valid — they must not have to load the
# full index (or evict another month) to find that out.
_month_source_keys: dict[str, tuple[float, str]] = {}


def _month_source_key(year: int, month: int) -> str | None:
    """Content identity of a cached monthly file, or ``None`` if not cached.
    A stat per call; a hash only when the mtime moved."""
    key = f"{year:04d}{month:02d}"
    path = _cache_path_for_month(year, month)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    with _month_index_lock:
        entry = _month_source_keys.get(key)
        if entry and entry[0] == mtime:
            return entry[1]
    try:
        source_key = _source_key(path)
    except OSError:
        return None
    with _month_index_lock:
        _month_source_keys[key] = (mtime, source_key)
    return source_key


def _remember_month_index(key: str, source_key: str, index: dict[str, Any]) -> None:
    with _month_index_lock:
        _month_index_cache[key] = (source_key, index)
        _month_index_cache.move_to_end(key)
        while len(_month_index_cache) > _MONTH_INDEX_KEEP:
            _month_index_cache.popitem(last=False)


def _month_index(year: int, month: int) -> dict[str, Any] | None:
    """Return the derived index for a cached monthly file, or ``None`` when
    that month is not on disk. Never downloads; callers that want a fresh
    file run ``ensure_monthly_cached`` first."""
    key = f"{year:04d}{month:02d}"
    path = _cache_path_for_month(year, month)
    source_key = _month_source_key(year, month)
    if source_key is None:
        return None

    with _month_index_lock:
        cached = _month_index_cache.get(key)
        if cached and cached[0] == source_key:
            _month_index_cache.move_to_end(key)
            return cached[1]

    index_path = _index_path_for_month(year, month)
    with _lock_for_index(key):
        with _month_index_lock:
            cached = _month_index_cache.get(key)
        if cached and cached[0] == source_key:
            return cached[1]
        index = _load_index_file(index_path, source_key)
        if index is None:
            try:
                gate = _download_gate(index_path)
                gate.__enter__()
            except OSError:
                gate = None      # unwritable dir: build without the cross-worker lock
            try:
                # Another worker may have built it while we waited.
                index = _load_index_file(index_path, source_key)
                if index is None:
                    t0 = time.monotonic()
                    index = _build_month_index(path)
                    index["source_key"] = source_key
                    _write_index_file(index_path, index)
                    logger.info(
                        "GMN: indexed %s in %.1fs (%d nights, %d of our orbits)",
                        path.name, time.monotonic() - t0,
                        len(index["totals"]),
                        sum(len(v) for v in index["events"].values()),
                    )
            finally:
                if gate is not None:
                    gate.__exit__(None, None, None)
    _remember_month_index(key, source_key, index)
    return index


def events_for_month(year: int, month: int) -> list[dict[str, Any]]:
    """Every indexed (our-camera) trajectory of a cached month, oldest first.

    Copies, so callers may annotate freely. Empty when the month is not
    cached; this never downloads.
    """
    index = _month_index(year, month)
    if not index:
        return []
    out: list[dict[str, Any]] = []
    for date_key in sorted(index["events"]):
        out.extend(dict(ev) for ev in index["events"][date_key])
    return out


# ── Single-station → trajectory join ───────────────────────────────────
#
# One of our cameras contributing to a triangulated GMN trajectory means that
# meteor has a true geocentric radiant, Vg and orbit. Our per-detection record
# only ever holds single-station values, so we attach the trajectory solution
# by matching on (participating camera code, begin time). The match is exact on
# camera code and within a small time window on the begin timestamp — both
# derive from the same physical event, so they agree to well under a second;
# the window only absorbs the sub-second rounding between our FF-derived begin
# time and GMN's reported trajectory begin time.

# Two independent solvers timestamp the same meteor's begin to well under a
# second. A whole second of slack still can't collide two distinct meteors on
# one camera (RMS won't retrigger that fast), and the match is nearest-first.
_TRAJ_JOIN_TOLERANCE_S = 2.0

# Per-month index: key = "YYYYMM", value = (file_mtime, {cam_code: [(begin_dt, event)]}).
# Built lazily from the same cached monthly file the rest of the module uses and
# invalidated on mtime change, so a re-fetch that adds meteors is picked up.
_traj_index_cache: "OrderedDict[str, tuple[str, dict[str, list[tuple[datetime, dict[str, Any]]]]]]" = OrderedDict()
_traj_index_lock = threading.Lock()
# Months of join index held per worker (only OUR orbits, a few MB each).
_TRAJ_INDEX_KEEP = 6


def _parse_traj_time(time_str: str | None) -> datetime | None:
    """Parse a GMN 'YYYY-MM-DD HH:MM:SS.ffffff' begin timestamp as naive UTC."""
    if not time_str:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(time_str.strip(), fmt)
        except ValueError:
            continue
    return None


def _traj_index_for_month(
    year: int, month: int
) -> dict[str, list[tuple[datetime, dict[str, Any]]]]:
    """Return {cam_code: [(begin_dt, event), ...]} for a month, mtime-cached.

    Only trajectories with a parseable begin time are indexed; each is filed
    under every participating station code so a lookup by one of our cameras is
    an O(1) dict hit followed by a short nearest-time scan.
    """
    key = f"{year:04d}{month:02d}"
    # Keyed on the source file's content identity (not its mtime) so the
    # hourly 304 mtime bump does not rebuild it, and checked BEFORE the full
    # index is touched so a hit costs one stat. Only trajectories one of our
    # cameras took part in are indexed — those are the only ones a lookup by
    # one of our camera codes can ever hit.
    source_key = _month_source_key(year, month)
    if source_key is None:
        return {}
    with _traj_index_lock:
        cached = _traj_index_cache.get(key)
        if cached and cached[0] == source_key:
            _traj_index_cache.move_to_end(key)
            return cached[1]
    month_idx = _month_index(year, month)
    if not month_idx:
        return {}

    index: dict[str, list[tuple[datetime, dict[str, Any]]]] = {}
    for day_events in month_idx["events"].values():
        for ev in day_events:
            begin_dt = _parse_traj_time(ev.get("time"))
            if begin_dt is None:
                continue
            for st in ev.get("stations") or []:
                index.setdefault(st.upper(), []).append((begin_dt, ev))
    for entries in index.values():
        entries.sort(key=lambda pair: pair[0])

    with _traj_index_lock:
        _traj_index_cache[key] = (source_key, index)
        _traj_index_cache.move_to_end(key)
        while len(_traj_index_cache) > _TRAJ_INDEX_KEEP:
            _traj_index_cache.popitem(last=False)
    return index


def match_trajectory(
    cam_code: str, time_utc: str, tolerance_s: float = _TRAJ_JOIN_TOLERANCE_S
) -> dict[str, Any] | None:
    """Return the GMN trajectory this single-station detection contributed to.

    Matches on the participating camera code (exact) and the begin time
    (nearest within ``tolerance_s``). ``time_utc`` is the detection's begin
    timestamp, ISO ``YYYY-MM-DDTHH:MM:SS`` (as our index stores it) or the GMN
    space-separated form. Returns the trajectory event dict (geocentric
    radiant, Vg, orbital elements, heights) or ``None`` when the meteor was not
    triangulated, so callers keep single-station values in that case.

    Never raises: a missing/corrupt monthly cache yields ``None``.
    """
    if not cam_code or not time_utc:
        return None
    det_dt = _parse_traj_time(time_utc.replace("T", " "))
    if det_dt is None:
        return None
    cam = cam_code.upper()

    # A meteor whose begin time is a hair before midnight UTC lands in the
    # previous day; the monthly index spans the whole month, so only the two
    # candidate months around the detection need checking.
    candidates: list[dict[str, Any]] = []
    seen_months: set[tuple[int, int]] = set()
    for delta_s in (0, -tolerance_s, tolerance_s):
        probe = det_dt + timedelta(seconds=delta_s)
        ym = (probe.year, probe.month)
        if ym in seen_months:
            continue
        seen_months.add(ym)
        try:
            index = _traj_index_for_month(*ym)
        except (OSError, ValueError):
            continue
        candidates.extend(index.get(cam, []))

    best: dict[str, Any] | None = None
    best_diff = tolerance_s + 1.0
    for begin_dt, ev in candidates:
        diff = abs((begin_dt - det_dt).total_seconds())
        if diff <= tolerance_s and diff < best_diff:
            best_diff, best = diff, ev
    return best


def trajectory_for_detection(cam_code: str, time_utc: str) -> dict[str, Any] | None:
    """Public projection of :func:`match_trajectory` for a detection row.

    Returns a flat dict of the multi-station science fields (geocentric radiant,
    geocentric velocity, begin/end heights, orbital elements, trajectory id) or
    ``None`` when the detection was not part of a GMN trajectory. Field names
    mirror the ``orbit``/``trajectory`` blocks the events endpoint already emits
    so the public schema stays consistent.
    """
    ev = match_trajectory(cam_code, time_utc)
    if ev is None:
        return None
    return project_trajectory(ev)


def project_trajectory(ev: dict[str, Any]) -> dict[str, Any]:
    """Flatten one parsed trajectory event onto the public field names."""
    return {
        "trajectory_id": ev.get("id"),
        "shower": ev.get("shower"),
        "ra_geo_deg": ev.get("ra_geo"),
        "dec_geo_deg": ev.get("dec_geo"),
        "velocity_geo_km_s": ev.get("velocity"),
        "velocity_init_km_s": ev.get("velocity_init"),
        "velocity_avg_km_s": ev.get("velocity_avg"),
        "velocity_hel_km_s": ev.get("velocity_hel"),
        "altitude_begin_km": ev.get("altitude_begin"),
        "altitude_end_km": ev.get("altitude_end"),
        "altitude_peak_km": ev.get("peak_height"),
        "peak_absolute_magnitude": ev.get("peak_mag"),
        "mass_kg": ev.get("mass_kg"),
        "f_param": ev.get("f_param"),
        "convergence_angle_deg": ev.get("convergence"),
        "duration_s": ev.get("duration_s"),
        "orbit_a_au": ev.get("orbit_a"),
        "orbit_e": ev.get("orbit_e"),
        "orbit_i_deg": ev.get("orbit_i"),
        "orbit_peri_deg": ev.get("orbit_peri"),
        "orbit_node_deg": ev.get("orbit_node"),
        "orbit_q_au": ev.get("orbit_q"),
        "orbit_q_aph_au": ev.get("orbit_q_aph"),
        "tisserand": ev.get("tisserand"),
        "stations": list(ev.get("stations") or []),
        "station_count": len(ev.get("stations") or []),
    }


# Single-station detection science that comes straight from our own RMS files.
# Whether or not the meteor was later triangulated, these are always ours.
SINGLE_STATION_PROVENANCE = "single_station"
# Fields carried up from a matched GMN trajectory. Present only when the
# detection was triangulated, so provenance flips to multi_station.
MULTI_STATION_PROVENANCE = "multi_station"


def attach_trajectory(
    out: dict[str, Any], cam_code: str | None, time_utc: str | None
) -> dict[str, Any]:
    """Attach GMN trajectory science to a detection record, in place.

    Looks up whether this single-station detection contributed to a triangulated
    GMN trajectory (match on camera code + begin time). On a hit, the geocentric
    radiant, velocities, heights, photometric mass and orbital elements land
    under ``trajectory`` and ``provenance`` flips to ``multi_station``; on a miss
    the detection keeps its single-station values and ``provenance`` stays
    ``single_station``.

    The join is best-effort by design. GMN publishes a night 4-15 days after it
    happened, so most recent detections legitimately have no trajectory, and a
    fetch or parse failure must leave the single-station record intact rather
    than fail the request that carries it.

    Returns ``out`` so callers can use it inline.
    """
    out["provenance"] = SINGLE_STATION_PROVENANCE
    out["trajectory"] = None
    if not (cam_code and time_utc):
        return out
    try:
        traj = trajectory_for_detection(cam_code, time_utc)
    except Exception:
        logger.debug("trajectory join failed for %s @ %s", cam_code, time_utc, exc_info=True)
        return out
    if traj is None:
        return out
    out["provenance"] = MULTI_STATION_PROVENANCE
    out["trajectory"] = traj
    return out


def trajectories_for_camera_night(cam_code: str, date_str: str) -> list[dict[str, Any]]:
    """Every trajectory one of our cameras contributed to over one night.

    ``date_str`` is the compact ``YYYYMMDD`` night anchor the stations use — the
    evening date — and the window is noon UTC to noon UTC, matching the
    stations' own night grouping (``station_api._night_date``), so a detection
    just after midnight belongs to the night it started in.

    Returns ``[{"time_utc": "YYYY-MM-DDTHH:MM:SS", "trajectory": {...}}, ...]``
    sorted by time. Empty when GMN has not published the night yet — which for
    anything younger than about two weeks is the normal answer, not an error.
    """
    try:
        anchor = datetime.strptime(date_str, "%Y%m%d")
    except (TypeError, ValueError):
        return []
    start = anchor + timedelta(hours=12)
    end = start + timedelta(hours=24)
    cam = (cam_code or "").upper()
    if not cam:
        return []

    out: list[dict[str, Any]] = []
    seen_months: set[tuple[int, int]] = set()
    seen_ids: set[str] = set()
    # The window straddles midnight, so it can straddle a month boundary too.
    for probe in (start, end - timedelta(seconds=1)):
        ym = (probe.year, probe.month)
        if ym in seen_months:
            continue
        seen_months.add(ym)
        try:
            index = _traj_index_for_month(*ym)
        except (OSError, ValueError):
            continue
        for begin_dt, ev in index.get(cam, []):
            if not (start <= begin_dt < end):
                continue
            traj_id = str(ev.get("id") or "")
            if traj_id and traj_id in seen_ids:
                continue
            if traj_id:
                seen_ids.add(traj_id)
            out.append({
                "time_utc": begin_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                "trajectory": project_trajectory(ev),
            })
    out.sort(key=lambda r: r["time_utc"])
    return out


def attach_trajectories(cam_code: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Run :func:`attach_trajectory` over a night's detection rows, in place.

    One night is one lookup per detection against a per-month index that is
    built once and held until the cached GMN file changes, so labelling a whole
    night costs a dict hit and a short nearest-time scan per meteor.
    """
    for row in rows:
        attach_trajectory(row, cam_code, row.get("time_utc"))
    return rows


# ── Date-filtered view used by the dashboard ───────────────────────────


# ── Newest night GMN has actually published ────────────────────────────
#
# GMN's batch publishes a night 4-15 days after it happened (median 14-15
# days through 2026), so "last night" is essentially never in the data.
# An empty event list therefore has two very different meanings — "not
# solved upstream yet" and "published, but none of our cameras were on
# it" — and the dashboard has to be able to tell them apart.

# Per-file cache: key = str(path), value = (file_mtime, {YYYYMMDD, ...})
_month_extent_cache: dict[str, tuple[float, frozenset[str]]] = {}
_month_extent_lock = threading.Lock()


def _upstream_dates_for_month(year: int, month: int) -> frozenset[str]:
    """The set of ``YYYYMMDD`` nights a cached monthly file has rows for.

    Counts every upstream trajectory, not just ours: a night no RO camera
    was on is still a published night. Only the trajectory identifier is
    read (it is stamped ``YYYYMMDD…``), so even the ~110 MB current-month
    file is a cheap scan, and the result — at most 31 entries — is
    mtime-cached per file.

    Returns the whole set rather than just its maximum so that callers can
    apply their own cut-off (a row stamped in the future is corrupt data)
    without a garbage row hiding every real night behind it.
    """
    path = _cache_path_for_month(year, month)
    source_key = _month_source_key(year, month)
    if source_key is None:
        return frozenset()
    key = str(path)
    with _month_extent_lock:
        cached = _month_extent_cache.get(key)
        if cached and cached[0] == source_key:
            return cached[1]
    index = _month_index(year, month)
    if not index:
        return frozenset()
    found = frozenset(index.get("all_dates") or ())
    with _month_extent_lock:
        _month_extent_cache[key] = (source_key, found)
    return found


def latest_available_date(now: datetime | None = None) -> str | None:
    """Newest UTC night GMN has published trajectories for, ``YYYY-MM-DD``.

    Reads only what the monthly cache already holds on disk and never
    triggers a download, so it is cheap enough to answer a user request
    with. The current and previous monthly files bracket any plausible
    publication lag. Returns ``None`` when nothing is cached yet (a cold
    worker on a cold disk); callers fall back to their own default night.
    """
    now = now or datetime.now(timezone.utc)
    prev = now.replace(day=1) - timedelta(days=1)
    horizon = now.strftime("%Y%m%d")

    published: set[str] = set()
    for year, month in ((now.year, now.month), (prev.year, prev.month)):
        published |= _upstream_dates_for_month(year, month)
    # A row stamped in the future is corrupt data, not a published night.
    candidates = [d for d in published if d <= horizon]
    if not candidates:
        return None
    newest = max(candidates)
    return f"{newest[:4]}-{newest[4:6]}-{newest[6:8]}"


def _publication_fields(date_str: str | None) -> dict[str, Any]:
    """``latest_available_date`` + ``not_published_yet`` for one night.

    ``not_published_yet`` stays ``False`` when the newest night is unknown
    — we only claim a night is unpublished when we can name a newer one.
    """
    try:
        latest = latest_available_date()
    except (OSError, ValueError):
        logger.warning("GMN: latest_available_date failed", exc_info=True)
        latest = None
    return {
        "latest_available_date": latest,
        "not_published_yet": bool(latest and date_str and date_str > latest),
    }


def events_for_date(date_str: str) -> dict[str, Any]:
    """Replacement for the old Datasette-based ``_gmn_fetch_multistation``.

    ``date_str`` is ``YYYY-MM-DD``. Returns the same payload shape:
    ``{date, total_upstream, events, has_ro_count, de_only_count,
    last_fetched_utc, latest_available_date, not_published_yet, error?}``.
    Never raises — on failure returns the payload with an ``error`` string
    so the dashboard renders a stub instead of 500-ing.

    ``latest_available_date`` / ``not_published_yet`` describe why an empty
    ``events`` list is empty: upstream has not solved this night yet, or it
    has and none of our cameras were on it.
    """
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        day = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return {
            "date": date_str, "total_upstream": 0, "events": [],
            "has_ro_count": 0, "de_only_count": 0,
            "last_fetched_utc": fetched_at,
            **_publication_fields(None),
            "error": f"invalid date {date_str!r}; expected YYYY-MM-DD",
        }
    yyyymmdd = day.strftime("%Y%m%d")

    try:
        path = ensure_monthly_cached(day.year, day.month)
    except requests.RequestException as exc:
        return {
            "date": date_str, "total_upstream": 0, "events": [],
            "has_ro_count": 0, "de_only_count": 0,
            "last_fetched_utc": fetched_at,
            **_publication_fields(date_str),
            "error": f"GMN monthly fetch failed: {exc}",
        }

    try:
        index = _month_index(day.year, day.month)
    except (FileNotFoundError, OSError) as exc:
        logger.warning("GMN: failed to read cached file %s: %s", path, exc)
        index = None
    if index is None:
        return {
            "date": date_str, "total_upstream": 0, "events": [],
            "has_ro_count": 0, "de_only_count": 0,
            "last_fetched_utc": fetched_at,
            **_publication_fields(date_str),
        }
    total = int(index["totals"].get(yyyymmdd, 0))
    # The overlay shows RO-witnessed and Berlin-only orbits; the index also
    # carries MD-witnessed ones for the trajectory join, so filter here and
    # hand out copies (callers annotate/pin these dicts).
    events: list[dict[str, Any]] = []
    for ev in index["events"].get(yyyymmdd, ()):
        if not (ev.get("has_ro") or ev.get("has_de")):
            continue
        ev = dict(ev)
        ev.pop("has_de", None)
        events.append(ev)

    return {
        "date":             date_str,
        "total_upstream":   total,
        "events":           events,
        "has_ro_count":     sum(1 for e in events if e["has_ro"]),
        "de_only_count":    sum(1 for e in events if e["de_only"]),
        "last_fetched_utc": fetched_at,
        **_publication_fields(date_str),
    }


# ── Orbit count aggregation ──────────────────────────────────────────

# Per-month cache: key = "YYYYMM", value = (file_mtime, {date_str: count})
_daily_count_cache: dict[str, tuple[float, dict[str, int]]] = {}
_daily_count_lock = threading.Lock()


def _daily_counts_for_month(year: int, month: int) -> dict[str, int]:
    """Return {YYYYMMDD: orbit_count} for RO-station orbits.

    Read off the per-month index; the small count map is memoised on its own
    (keyed on the source file's content) so orbit counting over 13 months
    does not keep 13 full indexes resident.
    """
    key = f"{year:04d}{month:02d}"
    source_key = _month_source_key(year, month)
    if source_key is None:
        return {}
    with _daily_count_lock:
        cached = _daily_count_cache.get(key)
        if cached and cached[0] == source_key:
            return cached[1]
    index = _month_index(year, month)
    if not index:
        return {}
    counts = {str(k): int(v) for k, v in (index.get("ro_counts") or {}).items()}
    with _daily_count_lock:
        _daily_count_cache[key] = (source_key, counts)
    return counts


_our_daily_count_cache: dict[str, tuple[float, dict[str, int]]] = {}
_our_daily_count_lock = threading.Lock()


def our_daily_counts_for_month(
    year: int, month: int, our_cam_codes: frozenset[str]
) -> dict[str, int]:
    """Return {YYYYMMDD: count} for orbits witnessed by at least one camera in our_cam_codes.

    Mtime-cached so repeated calls within the same process hit memory, not disk.
    The cache key includes a hash of our_cam_codes so adding/removing cameras
    from the public config invalidates the cache.
    """
    source_key = _month_source_key(year, month)
    if source_key is None:
        return {}
    codes_hash = hash(tuple(sorted(our_cam_codes)))
    cache_key = f"{year:04d}{month:02d}_{codes_hash}"
    with _our_daily_count_lock:
        cached = _our_daily_count_cache.get(cache_key)
        if cached and cached[0] == source_key:
            return cached[1]
    index = _month_index(year, month)
    if not index:
        return {}

    counts: dict[str, int] = {}
    for date_key, lists in (index.get("station_lists") or {}).items():
        n = sum(
            1 for stations in lists
            if any(str(s).upper() in our_cam_codes for s in stations)
        )
        if n:
            counts[str(date_key)] = n

    with _our_daily_count_lock:
        _our_daily_count_cache[cache_key] = (source_key, counts)
    return counts


_orbit_result: dict[str, Any] | None = None
_orbit_computing = False


def _compute_orbit_counts() -> None:
    global _orbit_result, _orbit_computing
    _orbit_computing = True
    try:
        now = datetime.now(timezone.utc)
        today = now.date()

        months_needed: set[tuple[int, int]] = set()
        d = today
        for _ in range(366):
            months_needed.add((d.year, d.month))
            d -= timedelta(days=1)

        for year, month in months_needed:
            try:
                ensure_monthly_cached(year, month)
            except Exception:
                pass

        cutoff_30d = (today - timedelta(days=30)).strftime("%Y%m%d")
        cutoff_12m = (today - timedelta(days=365)).strftime("%Y%m%d")

        total_12m = 0
        total_30d = 0
        latest_date = ""

        for year, month in sorted(months_needed):
            daily = _daily_counts_for_month(year, month)
            for date_str, count in daily.items():
                if date_str >= cutoff_12m:
                    total_12m += count
                if date_str >= cutoff_30d:
                    total_30d += count
                if date_str > latest_date:
                    latest_date = date_str

        last_night_count = 0
        last_night_date = ""
        if latest_date:
            ym = (int(latest_date[:4]), int(latest_date[4:6]))
            daily = _daily_counts_for_month(*ym)
            last_night_count = daily.get(latest_date, 0)
            last_night_date = f"{latest_date[:4]}-{latest_date[4:6]}-{latest_date[6:8]}"

        _orbit_result = {
            "last_12_months": total_12m,
            "last_30_days": total_30d,
            "last_night": last_night_count,
            "last_night_date": last_night_date,
        }
        logger.info("orbit_counts: computed (12m=%d, 30d=%d, last=%d on %s)",
                     total_12m, total_30d, last_night_count, last_night_date)
    except Exception:
        logger.exception("orbit_counts: computation failed")
    finally:
        _orbit_computing = False


def orbit_counts() -> dict[str, Any] | None:
    """Return cached orbit counts, or None if not yet computed.

    On first call, kicks off background computation. Subsequent calls
    return the cached result instantly.
    """
    global _orbit_computing
    if _orbit_result is not None:
        return _orbit_result
    if not _orbit_computing:
        threading.Thread(target=_compute_orbit_counts, daemon=True, name="orbit-counts").start()
    return None
