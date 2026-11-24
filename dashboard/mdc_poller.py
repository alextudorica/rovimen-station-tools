"""IAU Meteor Data Center shower data fetcher and background poller.

Fetches the MDC full shower list weekly, parses the pipe-delimited format,
and caches a slim dict keyed by IAU code. Routes call get_shower_data() —
they never hit upstream directly.

MDC file format: one record per observation/publication, multiple records
per shower. Records with 'M' in the Flags field are mean/averaged values
and are preferred. Solar longitudes (J2000) are turned into this year's
calendar dates from the Sun's position (Meeus, Astronomical Algorithms
ch. 25, good to minutes), and are also passed through as numbers so the
Showers page can place them in any year.
"""
import math

import datetime
import json
import logging
import os
import threading
import time
from pathlib import Path

import requests

from atomic_write import write_atomic

logger = logging.getLogger(__name__)

# ZHR at peak — from IMO Meteor Shower Calendar (not in MDC file).
# Variable showers noted with their typical range; value here is the
# typical/average peak, not storm-year maxima.
_ZHR: dict[str, int] = {
    "QUA": 120, "ACE": 6,  "GNO": 4,   "LYR": 18,  "ELY": 3,
    "ETA": 50,  "XHE": 3,  "ARI": 54,  "ZPE": 40,  "BTA": 25,
    "JBO": 2,   "JPE": 3,  "JXA": 5,   "PAU": 5,   "CAP": 5,
    "SDA": 25,  "NDA": 3,  "PER": 100, "KCG": 3,   "MIC": 10,
    "AUR": 6,   "SPE": 5,  "DSX": 14,  "SSG": 3,   "DRA": 5,
    "ORI": 20,  "EGE": 3,  "STA": 5,   "NTA": 5,   "TAU": 5,
    "LMI": 2,   "LEO": 15, "AMO": 5,   "AND": 3,   "NOO": 3,
    "NPI": 3,   "MON": 3,  "HYD": 3,   "GEM": 150, "COM": 5,
    "URS": 10,  "PHO": 3,  "ICE": 3,   "ANT": 2,   "SPO": 8,
}

# English Wikipedia article per IAU code, checked to be about that shower
# (tools/build_shower_wikipedia.py; rerun when the shower list changes).
try:
    _WIKIPEDIA: dict[str, str] = json.loads(
        (Path(__file__).resolve().parent / "shower_data" / "wikipedia.json").read_text(encoding="utf-8"))
except (OSError, ValueError):
    _WIKIPEDIA = {}

_MDC_URL_TPL = (
    "https://www.ta3.sk/IAUC22DB/MDC2022/Etc/streamfulldata{year}.txt"
)
_POLL_INTERVAL_S = 7 * 24 * 3600   # refresh weekly
_HTTP_TIMEOUT    = 30
_RETRY_AFTER_FAILURE_S = 3600   # a failed refresh is retried hourly, not weekly
# Parsed table persisted here so a recycled worker (and every sibling worker)
# starts from disk instead of pulling 1.5 MB from ta3.sk and re-parsing it.
# At the 2026-09 recycle rate that fetch ran ~600x a day against an academic
# server for data that changes once a year.
_DISK_CACHE = Path(os.environ.get("ROVIMEN_CACHE_PATH", "/opt/rovimen/cache")) / "mdc_showers.json"
# Bump when _parse() output changes. A disk copy in an older format is still
# served (anything beats an empty table) but refreshed at once instead of at
# the weekly mark; #1406 added the radiant and solar longitudes, and a week
# of the old copy meant no Moon or viewing times on the Showers page.
_CACHE_SCHEMA = 2

# Module-level state — sole writer is the poller thread.
_cache: dict[str, dict] = {}
_cache_lock = threading.Lock()


# ── Solar longitude → calendar date ───────────────────────────────────────

def _sun_lon2000(jd: float) -> float:
    """The Sun's geometric ecliptic longitude on the J2000 equinox, degrees."""
    t = (jd - 2451545.0) / 36525
    l0 = 280.46646 + 36000.76983 * t + 0.0003032 * t * t
    m = math.radians(357.52911 + 35999.05029 * t - 0.0001537 * t * t)
    c = ((1.914602 - 0.004817 * t) * math.sin(m) + (0.019993 - 0.000101 * t) * math.sin(2 * m)
         + 0.000289 * math.sin(3 * m))
    return (l0 + c - 1.396971 * t) % 360.0


def _sol_lon_to_datetime(lon: float, year: int | None = None) -> datetime.datetime:
    """The UTC instant in `year` (default: this year) when the Sun reaches
    J2000 solar longitude `lon`. The same calculation as solToDate() in
    static/dashboard-sky.js."""
    year = year or datetime.date.today().year
    jan1 = datetime.datetime(year, 1, 1, tzinfo=datetime.timezone.utc)
    jd_jan1 = jan1.timestamp() / 86400 + 2440587.5
    jd = jd_jan1 + 78.8 + (lon % 360.0) / 360.0 * 365.2422
    if jd >= jd_jan1 + 365:          # January to mid-March: start of the year
        jd -= 365.2422
    for _ in range(4):
        jd -= (((_sun_lon2000(jd) - lon) + 180.0) % 360.0 - 180.0) / 0.98565
    return datetime.datetime.fromtimestamp((jd - 2440587.5) * 86400, tz=datetime.timezone.utc)


def _sol_lon_to_date(lon: float, year: int | None = None) -> str:
    """Solar longitude (degrees, J2000) as this year's date, e.g. "Oct 21".

    The old linear rule (365.25 days per 360 degrees from a 20 March anchor)
    ignored the Sun's changing speed and put summer and autumn peaks 3-4 days
    early (Perseids on 9 August, Orionids on 19 October)."""
    dt = _sol_lon_to_datetime(lon, year)
    return f"{dt.strftime('%b')} {dt.day}"


# ── MDC fetch ──────────────────────────────────────────────────────────────

def _fetch_raw() -> str | None:
    """Try current year then previous year for the MDC full data file."""
    year = datetime.date.today().year
    for y in (year, year - 1):
        url = _MDC_URL_TPL.format(year=y)
        try:
            r = requests.get(url, timeout=_HTTP_TIMEOUT)
            if r.status_code == 200:
                logger.info("MDC poller: fetched %s (%d bytes)", url, len(r.content))
                return r.text
            logger.warning("MDC poller: HTTP %s for %s", r.status_code, url)
        except Exception as exc:
            logger.warning("MDC poller: request failed for %s: %s", url, exc)
    return None


# ── MDC parse ─────────────────────────────────────────────────────────────

def _pf(s: str) -> float | None:
    """Parse a stripped string to float, returning None if blank/invalid.

    Some MDC records hold the text "nan" (e.g. the Corvids' radiant drift):
    float() accepts it, but NaN is not JSON, and one would make the whole
    /api/showers response unparseable in the browser."""
    s = s.strip()
    if not s:
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def _parse(raw: str) -> dict[str, dict]:
    """Parse MDC pipe-delimited text → {iau_code: shower_info}.

    Column indices (0-based, after splitting on '"|"' and stripping quotes):
      3  code       IAU 3-letter code
      4  s          status (negative = removed/rejected)
      5  subdate    submission date (YYYY-mm-dd)
      6  name       full shower name
      7  activity   activity type string
      8  LoSb       solar longitude at activity begin
      9  LoSe       solar longitude at activity end
     10  LoS        solar longitude at peak
     15  Vg         geocentric velocity (km/s)
     21  Flags      'M' present → mean/averaged record
     31  Origin     parent body
    """
    candidates: dict[str, list[dict]] = {}

    for line in raw.splitlines():
        line = line.strip()
        if not line or line[0] in (':', '+'):
            continue

        parts = [p.strip().strip('"') for p in line.split('"|"')]
        if len(parts) < 32:
            continue

        code   = parts[3].strip()
        status = parts[4].strip()
        if not code or len(code) > 5:
            continue
        try:
            if int(status) < 0:
                continue
        except ValueError:
            continue

        flags   = parts[21].strip() if len(parts) > 21 else ''
        is_mean = 'M' in flags

        candidates.setdefault(code, []).append({
            'name':      parts[6].strip(),
            'subdate':   parts[5].strip(),
            'activity':  parts[7].strip(),
            'sol_begin': _pf(parts[8]),
            'sol_end':   _pf(parts[9]),
            'sol_peak':  _pf(parts[10]),
            'vg':        _pf(parts[15]),
            'ra':        _pf(parts[11]),   # radiant at the peak, J2000 (deg)
            'dec':       _pf(parts[12]),
            'dra':       _pf(parts[13]),   # radiant drift, deg per deg of solar longitude
            'ddec':      _pf(parts[14]),
            'iau_no':    parts[1].strip(),
            'parent':    parts[31].strip() if len(parts) > 31 else '',
            'is_mean':   is_mean,
        })

    result: dict[str, dict] = {}
    for code, recs in candidates.items():
        # Prefer mean records; within that, most recent submission date.
        pool = sorted(
            [r for r in recs if r['is_mean']] or recs,
            key=lambda r: r['subdate'],
            reverse=True,
        )
        b = pool[0]

        entry: dict[str, object] = {'name': b['name']}
        if b['vg'] is not None:
            entry['vg'] = round(b['vg'], 1)
        if b['parent']:
            entry['parent'] = b['parent']
        # The numbers behind the dates, and the radiant, for the Showers
        # page's details (this year's dates, best viewing time from Romania).
        for key in ('sol_begin', 'sol_end', 'sol_peak', 'ra', 'dec', 'dra', 'ddec'):
            if b[key] is not None:
                entry[key] = round(b[key], 2)
        if b['iau_no'].isdigit():
            entry['iau_no'] = int(b['iau_no'])
        if b['sol_begin'] is not None:
            entry['activity_begin'] = _sol_lon_to_date(b['sol_begin'])
        if b['sol_end'] is not None:
            entry['activity_end']   = _sol_lon_to_date(b['sol_end'])
        if b['sol_peak'] is not None:
            entry['peak'] = _sol_lon_to_date(b['sol_peak'])
        if code in _ZHR:
            entry['zhr'] = _ZHR[code]
        if code in _WIKIPEDIA:
            entry['wikipedia'] = _WIKIPEDIA[code]

        result[code] = entry

    # Inject SPO manually — not present in MDC as a parseable shower.
    result.setdefault('SPO', {'name': 'Sporadic', 'zhr': _ZHR['SPO']})

    return result


# ── Public API ─────────────────────────────────────────────────────────────

def get_shower_data() -> dict[str, dict]:
    """Return a snapshot of the cached shower data. Empty dict before first fetch."""
    with _cache_lock:
        return dict(_cache)


# ── Background poller ──────────────────────────────────────────────────────

def _load_disk_cache() -> tuple[dict[str, dict], float] | None:
    """(showers, age_seconds) from the on-disk copy, or None."""
    try:
        raw = json.loads(_DISK_CACHE.read_text(encoding="utf-8"))
        showers = raw.get("showers")
        saved_at = float(raw.get("saved_at") or 0)
        if not isinstance(showers, dict) or not showers:
            return None
        if raw.get("schema") != _CACHE_SCHEMA:
            return showers, float("inf")       # old format: serve, but refresh now
        return showers, max(0.0, time.time() - saved_at)
    except Exception:
        return None


def _save_disk_cache(parsed: dict[str, dict]) -> None:
    try:
        write_atomic(
            _DISK_CACHE,
            json.dumps({"saved_at": time.time(), "schema": _CACHE_SCHEMA, "showers": parsed}),
            tmp=_DISK_CACHE.with_name(f"{_DISK_CACHE.name}.{os.getpid()}.tmp"),
            encoding="utf-8", mkdir=True,
        )
    except Exception as exc:
        logger.warning("MDC poller: could not persist shower cache: %s", exc)


def _poll_loop(stop: threading.Event) -> None:
    global _cache
    wait_s = 0.0
    disk = _load_disk_cache()
    if disk is not None:
        # Any disk copy beats an empty table: showers barely change year to
        # year, and a stale copy is the fallback if the refresh below fails.
        with _cache_lock:
            _cache = disk[0]
        if disk[1] < _POLL_INTERVAL_S:
            wait_s = _POLL_INTERVAL_S - disk[1]
            logger.info("MDC poller: loaded %d showers from disk cache (%.0f h old)",
                        len(disk[0]), disk[1] / 3600)
        else:
            logger.info("MDC poller: disk cache is %.1f d old — serving it, refreshing now",
                        disk[1] / 86400)
    while not stop.is_set():
        if wait_s and stop.wait(wait_s):
            return
        raw = _fetch_raw()
        if raw:
            parsed = _parse(raw)
            with _cache_lock:
                _cache = parsed
            _save_disk_cache(parsed)
            logger.info("MDC poller: cached %d showers", len(parsed))
            wait_s = _POLL_INTERVAL_S
        else:
            logger.warning("MDC poller: all fetch attempts failed — keeping existing cache, retrying in %.0f min",
                           _RETRY_AFTER_FAILURE_S / 60)
            wait_s = _RETRY_AFTER_FAILURE_S


def start_mdc_poller() -> threading.Event:
    """Spawn the MDC background poller daemon thread. Returns a stop event."""
    stop = threading.Event()
    threading.Thread(
        target=_poll_loop, args=(stop,),
        name="mdc-poller",
        daemon=True,
    ).start()
    return stop
