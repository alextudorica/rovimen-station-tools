"""Usage analytics derived from the audit + activity logs.

Two flat JSON-lines files feed every figure here:

* ``audit.log``    — one record per security-relevant event. We read the
  ``login`` / ``magic_login`` events (``result == "ok"``) to derive each
  user's *last login*.
* ``activity.log`` — one record per authenticated, non-poll request plus
  one per anonymous top-level page view (see
  ``security.log_request_activity``). This is the raw material for
  feature-usage counts, per-user *active time*, the daily-volume series,
  and the visitor-traffic report. Old lines predate the ``country`` /
  ``ua`` / ``bot`` fields, so every reader tolerates missing keys.

Everything is best-effort: the logs are append-only text written by a
separate daemon, may be mid-rotation, and individual lines can be
truncated or malformed. Every parse is wrapped so a bad line is skipped
rather than failing the whole report. All timestamps are ISO-8601 UTC as
written by ``security.audit`` / ``log_request_activity``.

The functions are read-only and hold no locks — safe to call from an
admin request thread. Scanning is bounded by ``_MAX_SCAN_BYTES`` (we tail
the file when it is larger) so a runaway log can never make an admin
endpoint hang.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import re
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

AUDIT_LOG_PATH = Path(
    os.environ.get("ROVIMEN_AUDIT_LOG_PATH", "/opt/rovimen/audit.log")
)
ACTIVITY_LOG_PATH = Path(
    os.environ.get("ROVIMEN_ACTIVITY_LOG_PATH", "/opt/rovimen/activity.log")
)

# Tail at most this many bytes from each log. The activity log is the high
# volume one; 64 MiB of one-line-per-request is on the order of hundreds of
# thousands of records — far more than any report window needs, and a hard
# ceiling against a pathologically large file blocking the request thread.
# deployment/vps/logrotate/rovimen-dashboard rotates the live activity log
# below this, so in production the tail is the whole file.
_MAX_SCAN_BYTES = 64 * 1024 * 1024

# A rotated segment is "<name>-<dateext stamp>" or "<name>.<n>", optionally
# gzipped. Matched against the suffix that follows the live file's name so an
# unrelated file sharing the prefix is not mistaken for history.
_SEGMENT_SUFFIX_RE = re.compile(r"^[-.][A-Za-z0-9_.-]+$")

# Consecutive same-user activity records less than this far apart are
# treated as continuous engagement; a longer gap starts a new session and
# the idle stretch in between is NOT counted. Each gap is also capped at
# this value so "active time" approximates wall-clock attention rather than
# raw last-minus-first (which would count a user who left a tab open).
_IDLE_GAP_S = 15 * 60


def _parse_ts(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _rotated_segments(path: Path) -> list[Path]:
    """Archived segments of ``path``, newest first.

    logrotate keeps history beside the live file as ``activity.log-<stamp>``
    (dateext) or ``activity.log.<n>``, gzipped after the first rotation. A
    report window longer than the live file is meaningless without them, and
    since retention is permanent (deployment/vps/logrotate/rovimen-dashboard)
    there can be a lot of them — so they are walked newest-first and the caller
    stops as soon as it reaches records older than the window.

    Sorting is by mtime rather than by name: the dateext stamp and the numbered
    suffix sort in opposite directions, and mtime is right for both.
    """
    try:
        sibs = [
            p for p in path.parent.iterdir()
            if p.name != path.name and p.name.startswith(path.name)
            and (p.suffix == ".gz" or _SEGMENT_SUFFIX_RE.match(p.name[len(path.name):]))
        ]
    except OSError:
        return []
    return sorted(sibs, key=lambda p: p.stat().st_mtime, reverse=True)


def _open_segment(path: Path):
    """Text handle for a log segment, transparently gunzipping a ``.gz``."""
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return open(path, "r", encoding="utf-8", errors="replace")


def _iter_log(path: Path, since: datetime) -> Iterator[dict[str, Any]]:
    """Yield JSON records at or after ``since``, live file plus archives.

    Reads the live file first, then walks rotated segments newest-first and
    stops at the first one whose newest record predates ``since`` — so a 7-day
    report costs one file even when years of history are on disk.

    Each segment is tailed to the last ``_MAX_SCAN_BYTES`` when bigger (dropping
    a possibly-partial first line); a gzipped segment is read whole, since its
    compressed size is already bounded by the rotation threshold. Malformed
    lines and records without a parseable ``ts`` are skipped silently.
    """
    yield from _iter_segment(path, since)
    for seg in _rotated_segments(path):
        newest: datetime | None = None
        produced = False
        for rec in _iter_segment(seg, since):
            newest = rec["_ts"] if newest is None else max(newest, rec["_ts"])
            produced = True
            yield rec
        if not produced:
            # Nothing at or after `since` in this segment, and segments only get
            # older from here.
            break


def _iter_segment(path: Path, since: datetime) -> Iterator[dict[str, Any]]:
    """Yield the records of one log segment (live or archived)."""
    gz = path.suffix == ".gz"
    try:
        size = 0 if gz else path.stat().st_size
    except OSError:
        return
    try:
        with _open_segment(path) as fh:
            if not gz and size > _MAX_SCAN_BYTES:
                fh.seek(size - _MAX_SCAN_BYTES)
                fh.readline()  # discard the partial line we landed in
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(rec, dict):
                    continue
                ts = _parse_ts(rec.get("ts", ""))
                if ts is None or ts < since:
                    continue
                rec["_ts"] = ts
                yield rec
    except OSError as exc:
        logger.warning("usage_stats: cannot read %s: %s", path, exc)


# ── Feature classification ───────────────────────────────────────────────
#
# Maps a request (method + path) to a human-facing feature label. First
# matching prefix wins, so order matters — most specific first. Unmatched
# paths return None and are excluded from feature stats. Page loads (GET of
# an HTML route) and
# the API calls behind each tab are deliberately bucketed under the same
# label so "Events" counts both the page view and its data fetches.

_FEATURE_RULES: list[tuple[str, str]] = [
    ("/api/sky_dome", "Celestial Dome"),
    ("/api/latest_frame", "Final Data Products"),
    ("/api/latest_stack", "Final Data Products"),
    ("/api/timelapses", "Timelapses"),
    ("/api/detections", "Detections"),
    ("/api/events", "Events"),
    ("/api/compilation", "Clip Compilation"),
    ("/api/compile", "Clip Compilation"),
    ("/api/rms", "RMS / Detection"),
    ("/api/nights", "Video DB"),
    ("/api/chunks", "Video DB"),
    ("/api/archive", "Archive"),
    ("/api/settings", "Station Settings"),
    ("/api/admin", "Admin actions"),
    ("/api/public", "Public API"),
    ("/events", "Events"),
    ("/live_view", "Live View"),
    ("/stream", "Live View"),
    ("/live", "Live View"),
    ("/highlights", "Highlights"),
    ("/showers", "Showers"),
    ("/wiki", "Build Guide"),
    ("/build", "Build Guide"),
    ("/social", "Social"),
    ("/station/", "Station dashboard"),
    ("/network", "Admin: Network"),
    ("/config", "Admin: Config"),
    ("/admin", "Admin"),
    ("/dashboard", "Station Dashboard"),
    ("/overview", "Overview"),
]


def _classify(path: str) -> str | None:
    if path == "/" or path == "":
        return "Overview"
    for prefix, label in _FEATURE_RULES:
        if path == prefix or path.startswith(prefix):
            return label
    return None


# ── Traffic page map ──────────────────────────────────────────────────────
#
# Every top-level HTML page, so no page is invisible in the traffic report.
# Kept separate from _FEATURE_RULES: this map is page-only (no API routes)
# and uses traffic-facing labels. Rules are anchored: exact match, or prefix
# match on the next path segment, so "/live" never swallows "/live_view".

_PAGE_RULES: list[tuple[str, str]] = [
    ("/station/", "Station dashboard"),
    ("/stations", "Fleet map"),
    ("/map", "Fleet map"),
    ("/events", "Detections"),
    ("/highlights", "Highlights"),
    ("/live", "Live View"),
    ("/showers", "Showers"),
    ("/wiki", "Build Guide"),
    ("/build", "Build Guide"),
    ("/social", "Social"),
    ("/login", "Login"),
]


def _page_label(path: str) -> str | None:
    """Human label for a top-level HTML page, or None for non-page paths."""
    if path in ("", "/"):
        return "Overview"
    for prefix, label in _PAGE_RULES:
        anchored = prefix if prefix.endswith("/") else prefix + "/"
        if path == prefix or path.startswith(anchored):
            return label
    return None


# UA substrings that mark a mobile browser. Checked after the bot flag, so
# a mobile-UA crawler still counts as a bot.
_MOBILE_UA_MARKERS = ("mobi", "android", "iphone", "ipad")

# Duplicated from security._BOT_UA_MARKERS deliberately: old log lines
# predate the ``bot`` flag, so the report re-derives bot-ness from the UA
# for any record that carries one but no flag.
_BOT_UA_MARKERS = (
    "bot", "spider", "crawl", "python-requests", "curl",
    "zgrab", "censys", "expanse",
)


def _record_kind(rec: dict[str, Any]) -> str:
    """'auth' | 'anon' | 'bot' for one activity record."""
    ua = (rec.get("ua") or "").lower()
    if rec.get("bot") or (ua and any(m in ua for m in _BOT_UA_MARKERS)):
        return "bot"
    return "auth" if rec.get("user") else "anon"


def _device_kind(rec: dict[str, Any], kind: str) -> str:
    """'mobile' | 'desktop' | 'bot' | 'unknown' from the UA."""
    if kind == "bot":
        return "bot"
    ua = (rec.get("ua") or "").lower()
    if not ua:
        return "unknown"  # pre-ua log lines
    if any(m in ua for m in _MOBILE_UA_MARKERS):
        return "mobile"
    return "desktop"


def _self_referrer_hosts() -> frozenset[str]:
    """Hostnames whose referrals are in-dashboard navigation, not inbound
    traffic. The canonical public host plus whatever
    ``ROVIMEN_PUBLIC_BASE_URL`` points at on this deployment."""
    hosts = {"dashboard.rovimen.org"}
    base = os.environ.get("ROVIMEN_PUBLIC_BASE_URL", "")
    if base:
        try:
            netloc = urlsplit(base).netloc.rpartition("@")[2]
            host = netloc.rsplit(":", 1)[0] if ":" in netloc and not netloc.endswith("]") else netloc
            if host:
                hosts.add(host.lower())
        except ValueError:
            pass
    return frozenset(hosts)


def _referrer_domain(ref: str) -> str | None:
    """Bare lowercase domain from a Referer URL, or None when unusable.

    Strips userinfo, port, and a leading ``www.`` so one site groups into
    one row.
    """
    try:
        netloc = urlsplit(ref).netloc
    except ValueError:
        return None
    if not netloc:
        return None
    host = netloc.rpartition("@")[2]  # strip userinfo
    if ":" in host and not host.endswith("]"):
        host = host.rsplit(":", 1)[0]  # strip port
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    return host or None


# ── TTL cache for expensive scans ─────────────────────────────────────────

_usage_cache: tuple[float, dict, int] | None = None
_usage_cache_lock = threading.Lock()
_activity_cache: tuple[float, dict, int] | None = None
_activity_cache_lock = threading.Lock()
_traffic_cache: tuple[float, dict, int] | None = None
_traffic_cache_lock = threading.Lock()
_USAGE_CACHE_TTL = 60  # seconds


# ── Per-user activity (last login + active time) ──────────────────────────


def _last_logins(since: datetime) -> dict[str, str]:
    """username -> ISO timestamp of most recent successful login."""
    out: dict[str, str] = {}
    for rec in _iter_log(AUDIT_LOG_PATH, since):
        if rec.get("event") not in ("login", "magic_login"):
            continue
        if rec.get("result") != "ok":
            continue
        user = rec.get("username")
        if not user or user == "?":
            continue
        ts = rec["_ts"].isoformat(timespec="seconds")
        # Lines are append-only chronological, so the last write wins.
        out[user] = ts
    return out


def compute_user_activity(days: int = 90) -> dict[str, dict[str, Any]]:
    """Per-user engagement summary over the trailing ``days`` window.

    Returns ``{username: {last_login, last_seen, requests, sessions,
    active_seconds}}``. ``active_seconds`` sums the gaps between a user's
    consecutive activity records, capping each gap at ``_IDLE_GAP_S`` and
    dropping gaps beyond it (session boundaries) — an attention estimate,
    not exact wall-clock.

    Results are cached for ``_USAGE_CACHE_TTL`` seconds to avoid
    re-scanning the log files on every request.
    """
    global _activity_cache
    now = time.monotonic()
    with _activity_cache_lock:
        if _activity_cache and (now - _activity_cache[0]) < _USAGE_CACHE_TTL:
            if _activity_cache[2] == days:
                return _activity_cache[1]

    result = _compute_user_activity_uncached(days)

    with _activity_cache_lock:
        _activity_cache = (time.monotonic(), result, days)
    return result


def _compute_user_activity_uncached(days: int) -> dict[str, dict[str, Any]]:
    """Inner implementation without caching."""
    since = datetime.now(timezone.utc) - timedelta(days=max(1, days))
    # user -> ordered list of activity timestamps
    stamps: dict[str, list[datetime]] = defaultdict(list)
    for rec in _iter_log(ACTIVITY_LOG_PATH, since):
        user = rec.get("user")
        if not user or rec.get("kind"):
            continue  # client-reported events are not requests
        stamps[user].append(rec["_ts"])

    last_logins = _last_logins(since)

    out: dict[str, dict[str, Any]] = {}
    # Union of everyone seen in either log so a user who logged in but did
    # nothing still gets a row.
    for user in set(stamps) | set(last_logins):
        ts_list = sorted(stamps.get(user, []))
        active = 0
        sessions = 1 if ts_list else 0
        for prev, cur in zip(ts_list, ts_list[1:]):
            gap = (cur - prev).total_seconds()
            if gap <= _IDLE_GAP_S:
                active += gap
            else:
                sessions += 1
        out[user] = {
            "last_login": last_logins.get(user),
            "last_seen": ts_list[-1].isoformat(timespec="seconds") if ts_list else None,
            "requests": len(ts_list),
            "sessions": sessions,
            "active_seconds": int(active),
        }
    return out


# ── Aggregate usage report ────────────────────────────────────────────────


def compute_usage(days: int = 7) -> dict[str, Any]:
    """Site-wide usage report over the trailing ``days`` window.

    Results are cached for ``_USAGE_CACHE_TTL`` seconds to avoid
    re-scanning the log files on every request.

    Shape::

        {
          "days", "since", "generated_at",
          "totals":   {"requests", "active_users", "sessions"},
          "features": [{"feature", "hits", "users", "last_used"}, ...],
          "users":    [{"user", "last_login", "last_seen", "requests",
                        "sessions", "active_seconds"}, ...],
          "daily":    [{"date", "hits"}, ...],  # oldest -> newest
          "client_failures": {"total", "users", "families", "daily",
                              "by_user", "recent"},
          "tab_views": [{"page", "tab", "views", "users", "last"}, ...]
        }

    Client-reported records (``kind`` set, see client_events.py) are
    excluded from requests, features and the daily series and feed only
    ``client_failures`` / ``tab_views``.
    """
    global _usage_cache
    now_mono = time.monotonic()
    with _usage_cache_lock:
        if _usage_cache and (now_mono - _usage_cache[0]) < _USAGE_CACHE_TTL:
            if _usage_cache[2] == days:
                return _usage_cache[1]

    result = _compute_usage_uncached(days)

    with _usage_cache_lock:
        _usage_cache = (time.monotonic(), result, days)
    return result


def _compute_usage_uncached(days: int) -> dict[str, Any]:
    """Inner implementation without caching."""
    days = max(1, min(days, 365))
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)

    feat_hits: dict[str, int] = defaultdict(int)
    feat_users: dict[str, set[str]] = defaultdict(set)
    feat_last: dict[str, datetime] = {}
    daily: dict[str, int] = defaultdict(int)
    total_requests = 0
    client = _ClientEventTally()

    for rec in _iter_log(ACTIVITY_LOG_PATH, since):
        user = rec.get("user")
        if not user:
            # Anonymous page views (user: null) feed the traffic report;
            # this report keeps its original meaning — authenticated
            # feature engagement.
            continue
        if rec.get("kind"):
            # Client-reported failure or tab view (client_events.py): its
            # own sections below, never a request.
            client.add(rec)
            continue
        path = rec.get("path") or ""
        ts = rec["_ts"]
        feat = _classify(path)
        if feat is not None:
            feat_hits[feat] += 1
            feat_users[feat].add(user)
            if feat not in feat_last or ts > feat_last[feat]:
                feat_last[feat] = ts
        daily[ts.date().isoformat()] += 1
        total_requests += 1

    features = sorted(
        (
            {
                "feature": f,
                "hits": feat_hits[f],
                "users": len(feat_users[f]),
                "last_used": feat_last[f].isoformat(timespec="seconds"),
            }
            for f in feat_hits
        ),
        key=lambda d: d["hits"],
        reverse=True,
    )

    activity = compute_user_activity(days=days)
    users = sorted(
        (
            {"user": u, **stats}
            for u, stats in activity.items()
        ),
        key=lambda d: (d.get("last_seen") or ""),
        reverse=True,
    )

    # Dense daily series so the chart has a bar for every day in the window,
    # including zero-traffic days. Anchored on today and walking back so the
    # last bar is always the current day (oldest -> newest).
    daily_series = []
    for i in range(days):
        d = (now.date() - timedelta(days=days - 1 - i)).isoformat()
        daily_series.append({"date": d, "hits": daily.get(d, 0)})

    return {
        "days": days,
        "since": since.isoformat(timespec="seconds"),
        "generated_at": now.isoformat(timespec="seconds"),
        "totals": {
            "requests": total_requests,
            "active_users": len([u for u in activity.values() if u["requests"]]),
            "sessions": sum(u["sessions"] for u in activity.values()),
        },
        "features": features,
        "users": users,
        "daily": daily_series,
        "client_failures": client.failures(now, days),
        "tab_views": client.tab_views(),
    }


# ── Client-reported events ────────────────────────────────────────────────
#
# Records written by routes/telemetry.py (``kind`` = client_failure |
# tab_view). Failures are what the request log cannot show: a 504 nginx
# served while Flask went on to log a 200, a network drop, a video that
# never loaded. Tab views count in-page tabs that make no request.

# Path segments that name a specific thing (night, station, camera, file,
# hash, number) collapse to '*' so failures group by endpoint family.
_ID_SEGMENT_RE = re.compile(
    r"^(\d{8}.*|\d{4}-\d{2}-\d{2}.*|gmn[a-z0-9_]+|[A-Z]{2}[0-9A-Z]{4}|"
    r".*\.[A-Za-z0-9]{2,5}|[0-9a-f]{12,}|\d+)$"
)
_RECENT_FAILURES = 15
_TOP_FAMILIES = 25


def _endpoint_family(path: str) -> str:
    """'/api/cached-video/gmnro07/RO000W/20260920/x.mkv' -> '/api/cached-video'."""
    segs = [seg for seg in path.split("/") if seg][:4]
    out = ["*" if _ID_SEGMENT_RE.match(seg) else seg for seg in segs]
    while out and out[-1] == "*":
        out.pop()
    return "/" + "/".join(out)


class _ClientEventTally:
    """Accumulates client-event records during the usage scan."""

    def __init__(self) -> None:
        self._fail_total = 0
        self._fail_users: set[str] = set()
        self._fam: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"count": 0, "users": set(), "outcomes": defaultdict(int), "last": None}
        )
        self._fail_daily: dict[str, int] = defaultdict(int)
        self._fail_by_user: dict[str, int] = defaultdict(int)
        self._recent: list[dict[str, Any]] = []
        self._tabs: dict[tuple[str, str], dict[str, Any]] = defaultdict(
            lambda: {"views": 0, "users": set(), "last": None}
        )

    def add(self, rec: dict[str, Any]) -> None:
        user = rec.get("user")
        ts: datetime = rec["_ts"]
        if rec.get("kind") == "client_failure":
            path = rec.get("path") or ""
            fam = self._fam[_endpoint_family(path)]
            fam["count"] += 1
            fam["users"].add(user)
            status = rec.get("status") or 0
            fam["outcomes"][str(status) if status else (rec.get("reason") or "?")] += 1
            fam["last"] = ts if fam["last"] is None or ts > fam["last"] else fam["last"]
            self._fail_total += 1
            self._fail_users.add(user)
            self._fail_daily[ts.date().isoformat()] += 1
            self._fail_by_user[user] += 1
            self._recent.append({
                "ts": ts.isoformat(timespec="seconds"), "user": user, "path": path,
                "status": status, "reason": rec.get("reason"), "page": rec.get("page"),
            })
            if len(self._recent) > _RECENT_FAILURES:
                self._recent.pop(0)
        elif rec.get("kind") == "tab_view":
            tab = self._tabs[(rec.get("path") or "", rec.get("tab") or "")]
            tab["views"] += 1
            tab["users"].add(user)
            tab["last"] = ts if tab["last"] is None or ts > tab["last"] else tab["last"]

    def failures(self, now: datetime, days: int) -> dict[str, Any]:
        families = sorted(
            (
                {
                    "family": name, "count": f["count"], "users": len(f["users"]),
                    "outcomes": dict(sorted(f["outcomes"].items(), key=lambda kv: -kv[1])),
                    "last": f["last"].isoformat(timespec="seconds"),
                }
                for name, f in self._fam.items()
            ),
            key=lambda d: d["count"], reverse=True,
        )[:_TOP_FAMILIES]
        daily = []
        for i in range(days):
            d = (now.date() - timedelta(days=days - 1 - i)).isoformat()
            daily.append({"date": d, "count": self._fail_daily.get(d, 0)})
        return {
            "total": self._fail_total,
            "users": len(self._fail_users),
            "families": families,
            "daily": daily,
            "by_user": sorted(
                ({"user": u, "count": n} for u, n in self._fail_by_user.items()),
                key=lambda d: d["count"], reverse=True,
            ),
            "recent": list(reversed(self._recent)),
        }

    def tab_views(self) -> list[dict[str, Any]]:
        return sorted(
            (
                {
                    "page": page, "tab": tab, "views": t["views"], "users": len(t["users"]),
                    "last": t["last"].isoformat(timespec="seconds"),
                }
                for (page, tab), t in self._tabs.items()
            ),
            key=lambda d: (d["page"], -d["views"]),
        )


# ── Traffic report (page views: anonymous + authenticated + bots) ─────────


def compute_traffic_report(days: int = 7) -> dict[str, Any]:
    """Visitor-traffic report over the trailing ``days`` window.

    Aggregates PAGE VIEWS only (GETs of top-level HTML pages, per
    ``_page_label``) so authenticated and anonymous traffic are counted
    with the same yardstick — the anonymous side of the log only ever
    contains page views, while the authenticated side also carries every
    API call.

    Crawlers (``bot: true`` records, plus UA re-derivation for older
    lines) appear in the totals, device split, and daily series but are
    excluded from the country / page / referrer tables so scanner sweeps
    can't drown out the human signal.

    Results are cached for ``_USAGE_CACHE_TTL`` seconds, same as the
    usage report. Shape::

        {
          "days", "since", "generated_at",
          "totals":    {"visits", "unique_visitors", "countries",
                        "auth_visits", "anon_visits", "bot_visits"},
          "countries": [{"country", "visits", "unique_ips",
                         "auth", "anon"}, ...],
          "devices":   {"mobile", "desktop", "bot", "unknown"},
          "daily":     [{"date", "auth", "anon", "bot"}, ...],
          "pages":     [{"page", "visits", "unique_ips"}, ...],
          "referrers": [{"domain", "visits"}, ...],
        }
    """
    global _traffic_cache
    now_mono = time.monotonic()
    with _traffic_cache_lock:
        if _traffic_cache and (now_mono - _traffic_cache[0]) < _USAGE_CACHE_TTL:
            if _traffic_cache[2] == days:
                return _traffic_cache[1]

    result = _compute_traffic_report_uncached(days)

    with _traffic_cache_lock:
        _traffic_cache = (time.monotonic(), result, days)
    return result


def _compute_traffic_report_uncached(days: int) -> dict[str, Any]:
    """Inner implementation without caching."""
    days = max(1, min(days, 365))
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    self_hosts = _self_referrer_hosts()

    kind_totals: dict[str, int] = {"auth": 0, "anon": 0, "bot": 0}
    human_ips: set[str] = set()
    devices: dict[str, int] = defaultdict(int)
    daily: dict[str, dict[str, int]] = defaultdict(
        lambda: {"auth": 0, "anon": 0, "bot": 0}
    )
    # country -> counters; keyed "??" when the Cloudflare header was absent
    # (tailnet-direct traffic never traverses the edge).
    countries: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"visits": 0, "ips": set(), "auth": 0, "anon": 0}
    )
    pages: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"visits": 0, "ips": set()}
    )
    referrers: dict[str, int] = defaultdict(int)

    for rec in _iter_log(ACTIVITY_LOG_PATH, since):
        if (rec.get("method") or "GET") != "GET":
            continue
        page = _page_label(rec.get("path") or "")
        if page is None:
            continue
        kind = _record_kind(rec)
        ip = rec.get("ip") or None

        kind_totals[kind] += 1
        devices[_device_kind(rec, kind)] += 1
        daily[rec["_ts"].date().isoformat()][kind] += 1
        if kind == "bot":
            continue  # visible in volume, excluded from the human tables

        if ip and ip != "-":
            human_ips.add(ip)
        c = countries[rec.get("country") or "??"]
        c["visits"] += 1
        c[kind] += 1
        if ip and ip != "-":
            c["ips"].add(ip)
        p = pages[page]
        p["visits"] += 1
        if ip and ip != "-":
            p["ips"].add(ip)
        domain = _referrer_domain(rec.get("ref") or "")
        if domain and domain not in self_hosts:
            referrers[domain] += 1

    daily_series = []
    for i in range(days):
        d = (now.date() - timedelta(days=days - 1 - i)).isoformat()
        counts = daily.get(d, {"auth": 0, "anon": 0, "bot": 0})
        daily_series.append({"date": d, **counts})

    country_rows = sorted(
        (
            {
                "country": name,
                "visits": c["visits"],
                "unique_ips": len(c["ips"]),
                "auth": c["auth"],
                "anon": c["anon"],
            }
            for name, c in countries.items()
        ),
        key=lambda r: r["visits"],
        reverse=True,
    )[:30]

    page_rows = sorted(
        (
            {"page": name, "visits": p["visits"], "unique_ips": len(p["ips"])}
            for name, p in pages.items()
        ),
        key=lambda r: r["visits"],
        reverse=True,
    )

    referrer_rows = sorted(
        ({"domain": d, "visits": n} for d, n in referrers.items()),
        key=lambda r: r["visits"],
        reverse=True,
    )[:20]

    return {
        "days": days,
        "since": since.isoformat(timespec="seconds"),
        "generated_at": now.isoformat(timespec="seconds"),
        "totals": {
            "visits": sum(kind_totals.values()),
            "unique_visitors": len(human_ips),
            "countries": len([n for n in countries if n != "??"]),
            "auth_visits": kind_totals["auth"],
            "anon_visits": kind_totals["anon"],
            "bot_visits": kind_totals["bot"],
        },
        "countries": country_rows,
        "devices": {
            "mobile": devices.get("mobile", 0),
            "desktop": devices.get("desktop", 0),
            "bot": devices.get("bot", 0),
            "unknown": devices.get("unknown", 0),
        },
        "daily": daily_series,
        "pages": page_rows,
        "referrers": referrer_rows,
    }
