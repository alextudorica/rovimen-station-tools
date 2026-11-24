"""Per-station GMN orbit counts: last 30 days and all time.

An orbit counts once for a station however many of its cameras took part,
and a camera belongs to the station that lists it in the registry today.

Two sources, split at the edge of the live window (the months touched by the
last 366 days, the same window ``gmn_data.orbit_counts`` keeps fresh):

- **Live window**: the cached monthly files through ``gmn_data._month_index``,
  re-read when GMN republishes a month.
- **Older months**: GMN's yearly files, streamed once per closed year (never
  stored; 2025 alone is ~860 MB) and reduced to per-month counts of the set
  of our camera codes in each orbit. That reduction is persisted as
  ``orbit_history.v1.json`` in the GMN cache dir, so a camera moved between
  stations is re-attributed without re-downloading anything.

Everything is computed in a background thread; ``station_orbit_counts``
never blocks a request.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import requests

from atomic_write import write_atomic
import gmn_data

logger = logging.getLogger(__name__)

_HISTORY_FORMAT = 1
FIRST_YEAR = 2018          # GMN's first trajectory file
REFRESH_SECONDS = 3600     # matches the open-month TTL of the monthly cache
_ID_YYYYMM_RE = re.compile(r"^(\d{6})\d{2}")


def _history_path() -> Path:
    return gmn_data.CACHE_DIR / f"orbit_history.v{_HISTORY_FORMAT}.json"


def _yearly_url(year: int) -> str:
    return f"{gmn_data.BASE_URL}/traj_summary_yearly_{year:04d}.txt"


def _ours(code: str, extra: frozenset[str]) -> bool:
    return (
        code.startswith(gmn_data._OUR_PREFIXES)
        or code in gmn_data.DE_STATION_CODES
        or code in gmn_data._INDEX_EXTRA_CODES
        or code in extra
    )


def _combo(stations: Iterable[str], extra: frozenset[str]) -> str:
    """Sorted, comma-joined set of our camera codes in one orbit ('' if none)."""
    return ",".join(sorted({s.strip().upper() for s in stations if _ours(s.strip().upper(), extra)}))


def reduce_lines(lines: Iterable[str], extra: frozenset[str]) -> dict[str, dict[str, int]]:
    """Reduce traj_summary rows to {YYYYMM: {camera combo: orbits}}.

    Only the ID (first column) and the station list (last column) are read,
    so a yearly file costs one ``split`` per row.
    """
    out: dict[str, Counter] = {}
    for line in lines:
        if not line or line[0] == "#":
            continue
        m = _ID_YYYYMM_RE.match(line)
        if not m:
            continue
        combo = _combo(gmn_data._split_stations(line.rstrip("\n").rsplit(";", 1)[-1]), extra)
        if combo:
            out.setdefault(m.group(1), Counter())[combo] += 1
    return {k: dict(v) for k, v in out.items()}


# ── History (closed years, from the yearly files) ──────────────────────

def _load_history() -> dict[str, Any]:
    try:
        with open(_history_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {"format": _HISTORY_FORMAT, "years": {}}
    if not isinstance(data, dict) or data.get("format") != _HISTORY_FORMAT:
        return {"format": _HISTORY_FORMAT, "years": {}}
    return data


def _save_history(data: dict[str, Any]) -> None:
    path = _history_path()
    write_atomic(path, json.dumps(data, separators=(",", ":")),
                 tmp=path.with_name(f"{path.name}.{os.getpid()}.tmp"), encoding="utf-8")


def _fetch_year(year: int, extra: frozenset[str]) -> dict[str, dict[str, int]]:
    url = _yearly_url(year)
    logger.info("station_orbits: streaming %s", url)
    t0 = time.monotonic()
    with requests.get(
        url, headers={"User-Agent": gmn_data.USER_AGENT},
        timeout=gmn_data.HTTP_TIMEOUT, stream=True,
    ) as resp:
        resp.raise_for_status()
        lines = (ln.decode("utf-8", errors="replace") for ln in resp.iter_lines(chunk_size=1 << 20))
        months = reduce_lines(lines, extra)
    logger.info("station_orbits: %d reduced in %.0fs (%d orbits of ours)", year,
                time.monotonic() - t0, sum(sum(c.values()) for c in months.values()))
    return months


def ensure_history(years: Iterable[int], extra: frozenset[str]) -> dict[str, Any]:
    """Make sure every year in ``years`` is in the history file, streaming the
    missing ones from GMN. Serialised across gunicorn workers on a file lock,
    and saved after each year, so a recycled worker resumes where it stopped.
    A year that fails to download is left out and retried next refresh."""
    path = _history_path()
    data = _load_history()
    if os.environ.get("ROVIMEN_STATION_ORBITS_HISTORY", "1") == "0":
        return data  # tests: never stream GBs of yearly files
    missing = [y for y in years if str(y) not in data["years"]]
    if not missing:
        return data
    gmn_data.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with gmn_data._download_gate(path):
        data = _load_history()  # another worker may have filled it meanwhile
        for year in missing:
            if str(year) in data["years"]:
                continue
            try:
                data["years"][str(year)] = _fetch_year(year, extra)
            except (requests.RequestException, OSError) as exc:
                logger.warning("station_orbits: yearly %d unavailable: %s", year, exc)
                continue
            _save_history(data)
    return data


# ── Live window (from the monthly cache) ───────────────────────────────

# month key -> (source_key, {YYYYMMDD: {combo: orbits}})
_window_cache: dict[str, tuple[str, dict[str, dict[str, int]]]] = {}


def _month_by_day(year: int, month: int, extra: frozenset[str]) -> dict[str, dict[str, int]]:
    key = f"{year:04d}{month:02d}"
    source_key = gmn_data._month_source_key(year, month)
    if source_key is None:
        return {}
    cached = _window_cache.get(key)
    if cached and cached[0] == source_key:
        return cached[1]
    index = gmn_data._month_index(year, month)
    if not index:
        return {}
    by_day: dict[str, dict[str, int]] = {}
    for date_key, lists in (index.get("station_lists") or {}).items():
        c: Counter = Counter()
        for stations in lists:
            combo = _combo(stations, extra)
            if combo:
                c[combo] += 1
        if c:
            by_day[str(date_key)] = dict(c)
    _window_cache[key] = (source_key, by_day)
    return by_day


def _window_months(today) -> list[tuple[int, int]]:
    months: set[tuple[int, int]] = set()
    d = today
    for _ in range(366):
        months.add((d.year, d.month))
        d -= timedelta(days=1)
    return sorted(months)


# ── Aggregation ─────────────────────────────────────────────────────────

def aggregate(
    cam_to_station: dict[str, str],
    history_months: dict[str, dict[str, int]],
    window_days: dict[str, dict[str, int]],
    cutoff_30d: str,
) -> dict[str, dict[str, Any]]:
    """Per-station counts from combo maps.

    ``history_months`` is {YYYYMM: {combo: n}} for months outside the live
    window, ``window_days`` {YYYYMMDD: {combo: n}} inside it.
    """
    out: dict[str, dict[str, Any]] = {}

    def add(combo_counts: dict[str, int], period: str, recent: bool) -> None:
        for combo, n in combo_counts.items():
            for st in {cam_to_station[c] for c in combo.split(",") if c in cam_to_station}:
                rec = out.setdefault(st, {"orbits_30d": 0, "orbits_all": 0, "first_month": None})
                rec["orbits_all"] += n
                if recent:
                    rec["orbits_30d"] += n
                if rec["first_month"] is None or period[:6] < rec["first_month"]:
                    rec["first_month"] = period[:6]

    for ym, combos in history_months.items():
        add(combos, ym, False)
    for day, combos in window_days.items():
        add(combos, day, day >= cutoff_30d)
    return out


# ── Background state ────────────────────────────────────────────────────

_state_lock = threading.Lock()
_result: dict[str, Any] | None = None
_computed_at = 0.0
_computing = False


def _compute(cam_to_station: dict[str, str]) -> None:
    global _result, _computed_at, _computing
    try:
        today = datetime.now(timezone.utc).date()
        extra = frozenset(cam_to_station)
        window = _window_months(today)
        for y, m in window:
            try:
                gmn_data.ensure_monthly_cached(y, m)
            except Exception:
                pass
        window_keys = {f"{y:04d}{m:02d}" for y, m in window}
        window_days: dict[str, dict[str, int]] = {}
        for y, m in window:
            window_days.update(_month_by_day(y, m, extra))

        # Closed years with at least one month older than the window.
        first_window = min(window_keys)
        years = [y for y in range(FIRST_YEAR, today.year) if f"{y:04d}01" < first_window]
        history = ensure_history(years, extra)
        history_months = {
            ym: combos
            for months in history["years"].values()
            for ym, combos in months.items()
            if ym not in window_keys
        }
        complete = all(str(y) in history["years"] for y in years)

        cutoff_30d = (today - timedelta(days=30)).strftime("%Y%m%d")
        stations = aggregate(cam_to_station, history_months, window_days, cutoff_30d)
        with _state_lock:
            _result = {"stations": stations, "history_complete": complete,
                       "computed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            _computed_at = time.monotonic()
        logger.info("station_orbits: computed for %d stations (history complete: %s)",
                    len(stations), complete)
    except Exception:
        logger.exception("station_orbits: computation failed")
    finally:
        with _state_lock:
            _computing = False


def station_orbit_counts(cam_to_station: dict[str, str]) -> dict[str, Any] | None:
    """Latest per-station counts, or ``None`` before the first computation.

    Starts a background refresh when there is no result yet, when the last
    one is over an hour old, or when it predates a history that was still
    incomplete (retry after 10 min).
    """
    global _computing
    with _state_lock:
        age = time.monotonic() - _computed_at
        stale = _result is None or age > REFRESH_SECONDS or (
            not _result.get("history_complete") and age > 600)
        if stale and not _computing:
            _computing = True
            threading.Thread(target=_compute, args=(dict(cam_to_station),),
                             daemon=True, name="station-orbits").start()
        return _result
