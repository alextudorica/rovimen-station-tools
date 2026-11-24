#!/usr/bin/env python3
"""Live fireball alert: fleet coincidence of station watcher events -> Telegram.

Each station runs ``rovimen-scripts/fireball_watch.py``, which follows RMS's
live capture files and posts small events to the ingest API
(``/api/ingest/v1/<host>/fireball``, stored by :mod:`fireball_store`): fieldsum
spikes (whole-sky brightenings), FR line tracks (RMS's live bright-object
crops) and the camera's current platepar. This daemon reads them, runs the
rule from the 2026-10-17 replay of 29 nights on 51 cameras, and posts to one
Telegram chat:

A. Fieldsums: at least 5 cameras at 3 or more stations whose qualifying spikes
   (the per-camera test is done on the station) fall within 2.0 s, stations
   chained within 450 km, and an FR file on one of those cameras overlapping
   the cluster +-5 s (a lamp camera, see B, does not count). Without an FR the
   cluster needs 8 cameras at 4 stations. Time slides of nights 2026-11-06..09
   (64 cameras) put the chance rate at 3.4 a month without the FR condition,
   0.3 with it, and none seen for 8 at 4; the bare 5-at-3 rule raised a false
   alarm on 2026-11-09 18:09:34 UTC (five faint spikes, no FR anywhere).
B. FR pairs: two cameras at different stations whose FR head tracks pass the
   two-camera plane test (:func:`fireball_geometry.pair_consistency`:
   20-170 km, 8-80 km/s, dt <= 2.5 s), with overlapping height ranges whose
   lowest point is under 110 km, where one FR has at least 5000 saturated
   pixel-frames and the longer FR lasts at least 2 s. A camera that wrote 3 or
   more FR files in the 60 s up to the FR in question is ignored (a lamp,
   moonlit cloud: both replay false alarms came from such cameras).

Replayed: 5 of 6 known fireballs, 0 alerts outside them in 29 nights. Every
known fireball that met A also had an FR on a brightening camera.

Messages: a preliminary alert on the first trigger, a confirmed or retracted
update CONFIRM_AFTER_S later (the fieldsum step check needs the level 6-10 s
after the peak, which arrives with the next block), then the data pull: the
FF/FR pack +-60 s of every reachable camera within PULL_MAX_DIST_KM of a
station in the alert (``/api/fireball_pack``, a read of files already on disk),
stored under ``ROVIMEN_FIREBALL_EVENTS_DIR``, and the
colour chunk via ``POST /api/process_clip`` only where the station encodes on
its GPU (VAAPI PASS in the watcher's status). Elsewhere the dawn run encodes
the RMS-locked chunk and the clip requester fills gaps in daylight: no heavy
work on a station at night. A summary with maxpixel thumbnails closes the
event.

This is the one alert the fleet sends, at the maintainer's explicit request
(2026-10-17): fireball events only. Every message goes through
:mod:`alert_channels` with its kind (preliminary, confirmed, retracted, maps,
pulled, thumbs, clips): the maintainer's private Telegram chat gets everything; the
members' Telegram channel, Discord and email (2026-11-09, a members' vote)
get the confirmed alert, its maps, the thumbnails and the clips. No channel
configured -> it logs the messages and sends nothing.

Signals: the same preliminary/confirmed/retracted messages are also POSTed as
JSON to every URL in ``ROVIMEN_FIREBALL_SIGNAL_URLS`` (space or comma
separated) with the header ``X-Rovimen-Signal: <ROVIMEN_FIREBALL_SIGNAL_SECRET>``,
so a Pi on the tailnet can switch an LED or play a sound
(``tools/fireball_signal.py`` is the receiver). Unset -> nothing is posted.
The same payload also goes, as one ``FIREBALL_SIGNAL {json}`` text line, to
every Telegram chat in ``ROVIMEN_FIREBALL_SIGNAL_TELEGRAM_CHAT_IDS``: a
receiver that only makes outbound connections (``fireball_signal.py
--telegram-token-file``) reads it there with its own bot, because the
tailnet ACL lets the VPS reach a station only on :7779.

Environment: ROVIMEN_TELEGRAM_BOT_TOKEN (the bot the nightly summary uses, or
ROVIMEN_FIREBALL_TELEGRAM_BOT_TOKEN), ROVIMEN_FIREBALL_TELEGRAM_CHAT_ID, the
channel settings in :mod:`alert_channels`,
ROVIMEN_FIREBALL_SIGNAL_URLS, ROVIMEN_FIREBALL_SIGNAL_SECRET,
ROVIMEN_FIREBALL_SIGNAL_TELEGRAM_CHAT_IDS,
ROVIMEN_FIREBALL_DB, ROVIMEN_FIREBALL_EVENTS_DIR, ROVIMEN_PUBLIC_BASE_URL,
ROVIMEN_CONFIG.

Phase 2 (designed, not built): after the pull, solve the trajectory on the VPS
from the FR tracks of every camera with a platepar (the pair intersection
generalised to N cameras, then WMPL on the FF/FR pack) and post the radiant,
speed and end point.
"""

from __future__ import annotations

import argparse
import html
import io
import json
import logging
import math
import os
import re
import sys
import threading
import time
import zipfile
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

logger = logging.getLogger("fireball_alert")

# Rule A (fieldsums)
A_WIN_S = 2.0
A_MIN_CAMS = 5
A_MIN_STATIONS = 3
A_MAX_DIST_KM = 450.0
A_FR_SLACK_S = 5.0          # an FR on a cluster camera within this of the cluster corroborates it
A_FR_WAIT_S = 120.0         # FRs post 7-11 s after they end (p90); give up on a cluster after this
A_STRONG_MIN_CAMS = 8       # ... or this many cameras at this many stations, no FR needed
A_STRONG_MIN_STATIONS = 4
# Rule B (FR pairs)
B_GAP_S = 3.0
B_MIN_SAT = 5000
B_MIN_DUR_S = 2.0
B_LOW_MAX_KM = 110.0
B_OVERLAP_KM = -10.0
B_VETO_FILES = 3
B_VETO_WINDOW_S = 60.0
# Alert lifecycle
CONFIRM_AFTER_S = 15.0
CONFIRM_MAX_WAIT_S = 45.0
MERGE_S = 30.0
KEEP_S = 900.0
PULL_WINDOW_S = 60
PULL_TIMEOUT_S = 180
PULL_PARALLEL = 6
PULL_MAX_DIST_KM = 600.0    # stations further from every station in the alert are not pulled
WITHDRAW_MIN_PACKS = 3      # withdraw a fieldsum alert only when this many of its own cameras' packs hold no FR
MAX_THUMBS = 10

PUBLIC_BASE = os.environ.get("ROVIMEN_PUBLIC_BASE_URL", "https://dashboard.rovimen.org").rstrip("/")
EVENTS_DIR = Path(os.environ.get("ROVIMEN_FIREBALL_EVENTS_DIR", "/srv/rovimen/archive/.fireball_events"))
TELEGRAM_API = "https://api.telegram.org"

# Registry labels written without diacritics (dashboard_config.yaml).
_PLACE_FIX: dict[str, str] = {}

try:
    from zoneinfo import ZoneInfo
    _LOCAL_TZ: Any = ZoneInfo("Europe/Bucharest")
except Exception:  # pragma: no cover - tzdata missing
    _LOCAL_TZ = timezone(timedelta(hours=3))


@dataclass
class CamInfo:
    station: str
    place: str
    lat: float | None = None
    lon: float | None = None


def cam_label(cam: str, info: dict[str, CamInfo]) -> str:
    ci = info.get(cam)
    return f"{cam} ({ci.place})" if ci and ci.place else cam


def station_label(hk: str, info: dict[str, CamInfo]) -> str:
    for ci in info.values():
        if ci.station == hk and ci.place:
            return f"{hk} ({ci.place})"
    return hk


def cam_info_from_config(cfg: dict[str, Any]) -> dict[str, CamInfo]:
    out: dict[str, CamInfo] = {}
    for hk, st in (cfg.get("stations") or {}).items():
        place = _PLACE_FIX.get(st.get("label") or "", st.get("label") or "")
        for cam in st.get("cameras") or []:
            code = (cam.get("code") or "").upper()
            if code:
                out[code] = CamInfo(hk, place, st.get("lat"), st.get("lon"))
    return out


def night_of(t: float) -> str:
    """Observing night = UTC date of the evening (t - 12 h), as the dashboard names nights."""
    return datetime.fromtimestamp(t - 12 * 3600, timezone.utc).strftime("%Y%m%d")


def event_id(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y%m%d_%H%M%S")


def _hav_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(h))


@dataclass
class Alert:
    id: str
    t_event: float
    created: float
    rule_a: dict[str, Any] | None = None
    pairs: list[dict[str, Any]] = field(default_factory=list)
    state: str = "preliminary"
    confirmed_a: dict[str, Any] | None = None
    closed_at: float | None = None
    final_reason: str = ""


@dataclass
class Message:
    kind: str           # preliminary | confirmed | retracted | withdrawn
    alert: Alert
    text: str
    at: float


class Engine:
    """Sliding-window coincidence over watcher events. Pure logic: the clock
    is passed in, so the daemon and the replay harness run the same code."""

    def __init__(self, cam_info: dict[str, CamInfo]) -> None:
        self.info = cam_info
        self.spikes: dict[str, dict[str, Any]] = {}
        self.frs: dict[str, dict[str, Any]] = {}
        self.fr_files: dict[str, deque[float]] = {}
        self.cameras: dict[str, Any] = {}
        self.pp_mtime: dict[str, float] = {}
        self.status: dict[str, dict[str, Any]] = {}
        self.alerts: list[Alert] = []
        self._pair_cache: dict[tuple[str, str], dict[str, Any]] = {}

    # ── input ────────────────────────────────────────────────────────
    def station_of(self, cam: str, fallback: str | None = None) -> str | None:
        ci = self.info.get(cam)
        return ci.station if ci else fallback

    def set_platepar(self, cam: str, pp: dict[str, Any], mtime: float = 0.0) -> bool:
        import fireball_geometry as geo
        if mtime and mtime < self.pp_mtime.get(cam, 0.0):
            return False
        try:
            self.cameras[cam] = geo.camera_from_platepar(cam, pp)
        except Exception as exc:
            logger.warning("platepar for %s unusable: %s", cam, exc)
            return False
        self.pp_mtime[cam] = mtime
        self._pair_cache = {k: v for k, v in self._pair_cache.items() if cam not in k[0] and cam not in k[1]}
        ci = self.info.get(cam)
        if ci is not None and pp.get("lat") is not None:
            ci.lat, ci.lon = float(pp["lat"]), float(pp["lon"])
        return True

    def add(self, station: str, ev: dict[str, Any], now: float) -> None:
        kind = ev.get("kind")
        cam = ev.get("cam")
        if cam and cam not in self.info:
            self.info[cam] = CamInfo(station, "")
        if kind == "fs" and ev.get("id"):
            ev = dict(ev, station=self.station_of(cam, station), recv=now)
            self.spikes[ev["id"]] = ev
        elif kind == "fr" and ev.get("id"):
            ev = dict(ev, station=self.station_of(cam, station), recv=now)
            if ev["id"] not in self.frs:
                dq = self.fr_files.setdefault(cam, deque(maxlen=200))
                if not dq or dq[-1] != ev["t0"]:
                    dq.append(float(ev["t0"]))
            self.frs[ev["id"]] = ev
        elif kind == "platepar" and isinstance(ev.get("pp"), dict):
            self.set_platepar(cam, ev["pp"], float(ev.get("mtime") or 0))
        elif kind == "status":
            self.status[station] = dict(ev, recv=now)

    def prune(self, now: float) -> None:
        horizon = now - KEEP_S
        self.spikes = {k: v for k, v in self.spikes.items() if v["t_peak"] > horizon}
        self.frs = {k: v for k, v in self.frs.items() if v["tend"] > horizon}
        self._pair_cache = {k: v for k, v in self._pair_cache.items() if k[0] in self.frs and k[1] in self.frs}
        self.alerts = [a for a in self.alerts if a.closed_at is None or a.closed_at > now - 3600]

    # ── rule A ───────────────────────────────────────────────────────
    def _geo_component(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        stations = list(dict.fromkeys(r["station"] for r in rows))
        parent = {s: s for s in stations}

        def find(x: str) -> str:
            while parent[x] != x:
                x = parent[x]
            return x

        pos: dict[str, tuple[float, float] | None] = {}
        for r in rows:
            ci = self.info.get(r["cam"])
            if r["station"] not in pos or pos[r["station"]] is None:
                pos[r["station"]] = (ci.lat, ci.lon) if ci and ci.lat is not None else None
        for i, a in enumerate(stations):
            for b in stations[i + 1:]:
                pa, pb = pos.get(a), pos.get(b)
                if pa is None or pb is None or _hav_km(pa, pb) <= A_MAX_DIST_KM:
                    parent[find(a)] = find(b)
        groups: dict[str, list[str]] = {}
        for s in stations:
            groups.setdefault(find(s), []).append(s)
        best = max(groups.values(), key=lambda g: (len(g), sum(1 for r in rows if r["station"] in g)))
        return [r for r in rows if r["station"] in best]

    def rule_a(self, step_check: bool, around: float | None = None) -> list[dict[str, Any]]:
        """Clusters meeting rule A. ``step_check`` drops spikes known to be steps
        (a spike whose level 6-10 s later is still unknown counts, as in the replay)."""
        cand = [s for s in self.spikes.values() if s.get("qualifies")
                and not (step_check and s.get("step"))]
        if around is not None:
            cand = [s for s in cand if abs(s["t_peak"] - around) <= MERGE_S]
        cand.sort(key=lambda s: s["t_peak"])
        clusters: list[list[dict[str, Any]]] = []
        cur: list[dict[str, Any]] = []
        t_first = 0.0
        for s in cand:
            if cur and s["t_peak"] - t_first > A_WIN_S:
                clusters.append(cur)
                cur = []
            if not cur:
                t_first = s["t_peak"]
            cur.append(s)
        if cur:
            clusters.append(cur)
        hits = []
        for c in clusters:
            # one spike per camera: the strongest
            best: dict[str, dict[str, Any]] = {}
            for s in c:
                if s["cam"] not in best or s["peak"] > best[s["cam"]]["peak"]:
                    best[s["cam"]] = s
            rows = self._geo_component(list(best.values()))
            if len({r["cam"] for r in rows}) >= A_MIN_CAMS and len({r["station"] for r in rows}) >= A_MIN_STATIONS:
                rows.sort(key=lambda r: -r["peak"])
                hit = {"t": min(r["t_peak"] for r in rows), "spikes": rows,
                       "span": max(r["t_peak"] for r in rows) - min(r["t_peak"] for r in rows)}
                hit["fr_stations"] = self.fr_support(hit)
                hit["tier"] = self.a_tier(hit)
                hits.append(hit)
        return hits

    def fr_support(self, hit: dict[str, Any]) -> list[str]:
        """Stations where a camera of the cluster wrote an FR overlapping it (+-A_FR_SLACK_S),
        lamp cameras (the rule B veto) excluded."""
        cams = {s["cam"] for s in hit["spikes"]}
        t0, t1 = hit["t"] - A_FR_SLACK_S, hit["t"] + hit["span"] + A_FR_SLACK_S
        return sorted({fr["station"] for fr in self.frs.values()
                       if fr["cam"] in cams and fr["tstart"] <= t1 and fr["tend"] >= t0
                       and not self.vetoed(fr)})

    @staticmethod
    def a_tier(hit: dict[str, Any]) -> str | None:
        """"strong" (enough cameras to stand alone), "fr" (corroborated by an FR) or None."""
        sp = hit["spikes"]
        if len({s["cam"] for s in sp}) >= A_STRONG_MIN_CAMS and len({s["station"] for s in sp}) >= A_STRONG_MIN_STATIONS:
            return "strong"
        return "fr" if hit.get("fr_stations") else None

    # ── rule B ───────────────────────────────────────────────────────
    def vetoed(self, fr: dict[str, Any]) -> bool:
        dq = self.fr_files.get(fr["cam"]) or ()
        t0 = float(fr["t0"])
        return sum(1 for t in dq if t0 - B_VETO_WINDOW_S < t <= t0 + 1e-6) >= B_VETO_FILES

    def _pair(self, a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any] | None:
        import fireball_geometry as geo
        key = (a["id"], b["id"]) if a["id"] < b["id"] else (b["id"], a["id"])
        if key in self._pair_cache:
            return self._pair_cache[key]
        ca, cb = self.cameras.get(a["cam"]), self.cameras.get(b["cam"])
        res: dict[str, Any] | None = None
        if ca is not None and cb is not None:
            try:
                import numpy as np
                oa = geo.Obs(ca, ca.rays(a["hx"], a["hy"]), a["t0"] + np.asarray(a["t"], float) / a.get("fps", 25.0))
                ob = geo.Obs(cb, cb.rays(b["hx"], b["hy"]), b["t0"] + np.asarray(b["t"], float) / b.get("fps", 25.0))
                res = geo.pair_consistency(oa, ob)
            except Exception:
                logger.exception("pair geometry failed for %s x %s", a["id"], b["id"])
                res = None
        self._pair_cache[key] = res if res is not None else {"ok": None, "why": "no_platepar"}
        return self._pair_cache[key]

    @staticmethod
    def heights_ok(p: dict[str, Any]) -> bool:
        a, b = p.get("h_a"), p.get("h_b")
        if not isinstance(a, list) or not isinstance(b, list):
            return True
        lo, hi = max(a[1], b[1]), min(a[0], b[0])
        return bool(hi - lo > B_OVERLAP_KM and min(a[1], b[1]) < B_LOW_MAX_KM)

    def rule_b(self) -> list[dict[str, Any]]:
        frs = sorted(self.frs.values(), key=lambda f: f["tstart"])
        hits = []
        for i, a in enumerate(frs):
            for b in frs[i + 1:]:
                if b["tstart"] > a["tend"] + B_GAP_S:
                    break
                if b["cam"] == a["cam"] or a["station"] == b["station"]:
                    continue
                gap = max(0.0, b["tstart"] - a["tend"], a["tstart"] - b["tend"])
                if gap > B_GAP_S:
                    continue
                if max(a["n250_sum"], b["n250_sum"]) < B_MIN_SAT or max(a["dur"], b["dur"]) < B_MIN_DUR_S:
                    continue
                if self.vetoed(a) or self.vetoed(b):
                    continue
                p = self._pair(a, b)
                if not p or not p.get("ok") or not self.heights_ok(p):
                    continue
                hits.append({"t": min(a["tstart"], b["tstart"]), "a": a, "b": b, "geo": p})
        return hits

    # ── alerts ───────────────────────────────────────────────────────
    def _alert_for(self, t: float, now: float) -> tuple[Alert, bool]:
        for al in self.alerts:
            if abs(al.t_event - t) <= MERGE_S:
                return al, False
        al = Alert(event_id(t), t, now)
        self.alerts.append(al)
        return al, True

    def tick(self, now: float) -> list[Message]:
        out: list[Message] = []
        new: list[Alert] = []
        # Step spikes are dropped and the counts re-checked, as at confirmation, so a step
        # camera does not cancel a cluster that holds without it, whether its step update
        # arrives before or after the FR.
        for hit in self.rule_a(step_check=True):
            # Fieldsums alone are noise unless strong: wait for an FR, then give up.
            if hit["tier"] is None or now - hit["t"] > A_FR_WAIT_S:
                continue
            al, created = self._alert_for(hit["t"], now)
            if al.state != "preliminary":
                continue
            if al.rule_a is None or len(hit["spikes"]) > len(al.rule_a["spikes"]):
                al.rule_a = hit
            if created:
                new.append(al)
        for hit in self.rule_b():
            al, created = self._alert_for(hit["t"], now)
            if al.state != "preliminary":
                continue
            ids = {(p["a"]["id"], p["b"]["id"]) for p in al.pairs}
            if (hit["a"]["id"], hit["b"]["id"]) not in ids:
                al.pairs.append(hit)
                al.t_event = min(al.t_event, hit["t"])
            if created:
                new.append(al)
        for al in new:
            al.id = event_id(al.t_event)
            out.append(Message("preliminary", al, self.format_preliminary(al, now), now))
        for al in self.alerts:
            if al.state != "preliminary" or now - al.created < CONFIRM_AFTER_S:
                continue
            hits = self.rule_a(step_check=True, around=al.t_event)
            pending = [s for h in hits for s in h["spikes"] if s.get("after") is None]
            if not al.pairs and pending and now - al.created < CONFIRM_MAX_WAIT_S:
                continue
            hits = [h for h in hits if h["tier"]]
            al.confirmed_a = max(hits, key=lambda h: len(h["spikes"])) if hits else None
            al.closed_at = now
            if al.confirmed_a or al.pairs:
                al.state = "confirmed"
                out.append(Message("confirmed", al, self.format_final(al, now), now))
            else:
                al.state = "retracted"
                out.append(Message("retracted", al, self.format_final(al, now), now))
        self.prune(now)
        return out

    # ── text ─────────────────────────────────────────────────────────
    def _when(self, t: float) -> str:
        utc = datetime.fromtimestamp(t, timezone.utc)
        loc = utc.astimezone(_LOCAL_TZ)
        day = "" if loc.date() == utc.date() else loc.strftime(", %d %b")
        return f"{utc:%Y-%m-%d %H:%M:%S} UTC ({loc:%H:%M:%S} {loc.tzname()}{day})"

    def events_url(self, t: float) -> str:
        """The Detections page on its Grid tab, every camera's minute around the event. The Grid
        reads the window from ``gfrom``/``gto``; ``from``/``to`` keep the other tabs on it too."""
        a = datetime.fromtimestamp(t - 60, timezone.utc)
        b = datetime.fromtimestamp(t + 120, timezone.utc)
        win = f"from={a:%H:%M}&to={b:%H:%M}"
        return f"{PUBLIC_BASE}/events?tab=grid&date={night_of(t)}&{win}&g{win.replace('&to=', '&gto=')}"

    def _pair_line(self, p: dict[str, Any]) -> str:
        g = p["geo"]
        la = cam_label(p["a"]["cam"], self.info)
        lb = cam_label(p["b"]["cam"], self.info)
        parts = [f"{la} x {lb}"]
        hs = [h for k in ("h_a", "h_b") if isinstance(g.get(k), list) for h in g[k]]
        if hs:
            parts.append(f"{max(hs):.0f} to {min(hs):.0f} km")
        speed = self._speed_text(g)
        if speed:
            parts.append(speed)
        ll = g.get("ll_a") or g.get("ll_b")
        if isinstance(ll, list) and len(ll) == 2:
            parts.append(f"from {ll[0][0]:.2f}N {ll[0][1]:.2f}E to {ll[1][0]:.2f}N {ll[1][1]:.2f}E")
        return ", ".join(parts)

    @staticmethod
    def _speed_text(g: dict[str, Any]) -> str:
        """Speed high in the trail, plus the low-trail speed when the fireball slowed.

        The cameras of a pair often see different parts of the trail (20261021_201634: Bistrița
        102-53 km, Cluj 55-29 km), so the upper speed comes from the camera that saw highest and
        the lower one from the camera that saw lowest, instead of an average of both.
        """
        prof = [p for p in g.get("v_prof") or [] if isinstance(p, list) and len(p) == 6]
        if not prof:
            v = g.get("v")
            return f"{sum(v) / len(v):.0f} km/s" if v else ""
        top = max(prof, key=lambda p: p[1])
        low = min(prof, key=lambda p: p[5])
        text = f"{top[0]:.0f} km/s at {top[1]:.0f}-{top[2]:.0f} km"
        if low[3] < 0.8 * top[0]:
            text += f", slowing to {low[3]:.0f} km/s at {low[4]:.0f}-{low[5]:.0f} km"
        return text

    def _best_pair(self, al: Alert) -> dict[str, Any] | None:
        if not al.pairs:
            return None
        return max(al.pairs, key=lambda p: max(p["a"]["n250_sum"], p["b"]["n250_sum"]))

    def _spike_list(self, spikes: list[dict[str, Any]], limit: int = 12) -> str:
        items = [f"{cam_label(s['cam'], self.info)} +{s['peak'] * 100:.0f}%" for s in spikes[:limit]]
        more = len(spikes) - limit
        return ", ".join(items) + (f" and {more} more" if more > 0 else "")

    def format_preliminary(self, al: Alert, now: float) -> str:
        e = html.escape
        lines = ["<b>Fireball alert (preliminary)</b>", e(self._when(al.t_event))]
        if al.rule_a:
            sp = al.rule_a["spikes"]
            lines.append(e(f"Rule A, fieldsums: {len({s['cam'] for s in sp})} cameras at "
                           f"{len({s['station'] for s in sp})} stations within {al.rule_a['span']:.1f} s"
                           + self._fr_note(al.rule_a)))
        bp = self._best_pair(al)
        if bp:
            lines.append(e(f"Rule B, FR pair: {self._pair_line(bp)}"))
        if al.rule_a:
            lines.append(e("Sky brightening: " + self._spike_list(al.rule_a["spikes"])))
        if not bp:
            lines.append("No FR pair yet, so no height or position.")
        lines.append(e(f"Events page: {self.events_url(al.t_event)}"))
        lines.append(e(f"Raised {now - al.t_event:.1f} s after the event. Confirmation or retraction follows in about {CONFIRM_AFTER_S:.0f} s."))
        return "\n".join(lines)

    def _fr_note(self, hit: dict[str, Any]) -> str:
        frs = hit.get("fr_stations") or []
        if frs:
            return ", FR files at " + ", ".join(station_label(h, self.info) for h in frs)
        return ", no FR files" if hit.get("tier") != "strong" else ", strong enough without FR files"

    def format_final(self, al: Alert, now: float) -> str:
        e = html.escape
        hhmmss = datetime.fromtimestamp(al.t_event, timezone.utc).strftime("%H:%M:%S")
        if al.state == "confirmed":
            lines = [f"<b>Fireball confirmed</b>, {hhmmss} UTC"]
        else:
            lines = [f"<b>Alert retracted</b>, {hhmmss} UTC"]
        if al.rule_a:
            first = [self.spikes.get(s["id"]) or s for s in al.rule_a["spikes"]]
            kept = al.confirmed_a["spikes"] if al.confirmed_a else []
            steps = [s for s in first if s.get("step")]
            left = [s for s in first if not s.get("step")]
            txt = (f"Fieldsums: {len({s['cam'] for s in left})} of {len({s['cam'] for s in first})} cameras pass the"
                   f" step check (level 6-10 s later below half the peak)")
            if steps:
                txt += "; steps (lights or gain change): " + ", ".join(cam_label(s["cam"], self.info) for s in steps)
            lines.append(e(txt))
            if kept:
                lines.append(e(f"Rule A holds: {len({s['cam'] for s in kept})} cameras at {len({s['station'] for s in kept})} stations"
                               + self._fr_note(al.confirmed_a)))
            else:
                lines.append(e(f"Rule A no longer holds: {len({s['cam'] for s in left})} cameras at "
                               f"{len({s['station'] for s in left})} stations left; {A_MIN_CAMS} at {A_MIN_STATIONS} "
                               f"with an FR file, or {A_STRONG_MIN_CAMS} at {A_STRONG_MIN_STATIONS}, needed."))
        if al.pairs:
            lines.append(e(f"FR pairs passing the geometry test: {len(al.pairs)}"))
            for p in sorted(al.pairs, key=lambda p: -max(p["a"]["n250_sum"], p["b"]["n250_sum"]))[:4]:
                lines.append(e("  " + self._pair_line(p)))
        if al.state == "confirmed":
            lines.append(e(f"Pulling FF/FR +-{PULL_WINDOW_S} s from the stations within {PULL_MAX_DIST_KM:.0f} km;"
                           " a summary follows."))
        lines.append(e(f"Decided {now - al.t_event:.1f} s after the event."))
        return "\n".join(lines)


# ── platepars fetched from stations (fallback for cameras whose watcher sent none) ──

def fetch_station_platepars(cfg: Any, session: Any, timeout: float = 15.0) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for hk, st in cfg.stations.items():
        if not st.should_contact or st.jump_hosts:
            continue
        url = f"http://{st.host_for_path('/api/platepar')}:{cfg.station_api_port}/api/platepar"
        try:
            data = session.get(url, timeout=timeout).json()
        except Exception as exc:
            logger.info("platepar fetch from %s failed: %s", hk, exc)
            continue
        for cam, pp in (data or {}).items():
            if isinstance(pp, dict) and "error" not in pp and pp.get("F_scale"):
                out[cam.upper()] = pp
    return out


# ── outgoing messages ─────────────────────────────────────────────────

# Where messages go: an alert_channels.Broadcaster, which takes send(text, kind)
# and send_photos(photos, kind) and routes each kind to its channels; the
# replay tool uses one with a single Telegram chat, or none for a dry run.
Outlet = Any


# ── FF thumbnails ─────────────────────────────────────────────────────

def fits_maxpixel(data: bytes) -> Any:
    """The MAXPIXEL image of an RMS FF FITS file (minimal FITS reader, uint8 HDUs)."""
    import numpy as np
    off = 0
    while off < len(data):
        hdr: dict[str, str] = {}
        while True:
            block = data[off:off + 2880]
            off += 2880
            if len(block) < 2880:
                return None
            end = False
            for k in range(0, 2880, 80):
                card = block[k:k + 80].decode("ascii", "replace")
                key = card[:8].strip()
                if key == "END":
                    end = True
                    break
                if card[8:10] == "= ":
                    hdr[key] = card[10:].split("/")[0].strip().strip("'").strip()
            if end:
                break
        naxis = int(hdr.get("NAXIS", "0") or 0)
        bitpix = int(hdr.get("BITPIX", "8") or 8)
        if naxis == 2:
            n1, n2 = int(hdr["NAXIS1"]), int(hdr["NAXIS2"])
            size = n1 * n2 * abs(bitpix) // 8
            if hdr.get("EXTNAME", "").upper() == "MAXPIXEL":
                arr = np.frombuffer(data[off:off + size], dtype=">u1" if bitpix == 8 else ">u2").reshape(n2, n1)
                return arr
            off += (size + 2879) // 2880 * 2880
    return None


def _ff_time(name: str) -> float | None:
    m = re.search(r"_(\d{8})_(\d{6})_(\d{3})", name)
    if not m:
        return None
    dt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    return dt.timestamp() + int(m.group(3)) / 1000.0


def thumbnail_from_pack(zpath: Path, t_event: float) -> bytes | None:
    """JPEG of the max of the FF maxpixels covering t_event - 2 s .. + 12 s."""
    import numpy as np
    from PIL import Image
    try:
        zf = zipfile.ZipFile(zpath)
    except (OSError, zipfile.BadZipFile):
        return None
    with zf:
        stack = None
        for name in zf.namelist():
            base = name.rsplit("/", 1)[-1]
            if not (base.startswith("FF_") and base.endswith(".fits")):
                continue
            t0 = _ff_time(base)
            if t0 is None or not (t_event - 2 - 10.24 <= t0 <= t_event + 12):
                continue
            img = fits_maxpixel(zf.read(name))
            if img is None:
                continue
            stack = img.copy() if stack is None else np.maximum(stack, img)
    if stack is None:
        return None
    im = Image.fromarray(stack.astype("uint8"))
    im.thumbnail((1024, 1024))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=82)
    return buf.getvalue()


# ── data pull ─────────────────────────────────────────────────────────

def alert_cams(al: Alert) -> set[str]:
    cams = {s["cam"] for s in ((al.confirmed_a or al.rule_a or {}).get("spikes") or [])}
    return cams | {p[k]["cam"] for p in al.pairs for k in ("a", "b")}


def stations_in_reach(al: Alert, info: dict[str, CamInfo], stations: list[str]) -> tuple[list[str], list[str]]:
    """(near, far): a station is near when it is in the alert or within PULL_MAX_DIST_KM of a
    station that is; one without a position is near only when it is in the alert."""
    involved = {info[c].station for c in alert_cams(al) if c in info}
    pos: dict[str, tuple[float, float]] = {}
    for ci in info.values():
        if ci.lat is not None and ci.lon is not None:
            pos.setdefault(ci.station, (ci.lat, ci.lon))
    anchors = [pos[h] for h in involved if h in pos]
    near, far = [], []
    for hk in stations:
        p = pos.get(hk)
        ok = hk in involved or (p is not None and any(_hav_km(p, a) <= PULL_MAX_DIST_KM for a in anchors))
        (near if ok else far).append(hk)
    return near, far


def fr_files_near(zpath: Path, t_event: float) -> int:
    """FR bins in a pack that start between t_event - 30 s and t_event + 15 s."""
    try:
        with zipfile.ZipFile(zpath) as zf:
            names = [n.rsplit("/", 1)[-1] for n in zf.namelist()]
    except (OSError, zipfile.BadZipFile):
        return 0
    n = 0
    for base in names:
        if base.startswith("FR_") and base.endswith(".bin"):
            t0 = _ff_time(base)
            n += t0 is not None and t_event - 30 <= t0 <= t_event + 15
    return n


def pull_event(al: Alert, cfg: Any, engine: Engine, session: Any, out_root: Path,
               telegram: Outlet) -> dict[str, Any]:
    """Fetch the FF/FR pack of every reachable camera within PULL_MAX_DIST_KM, request
    clips where the station encodes on its GPU, and post a summary with thumbnails."""
    from concurrent.futures import ThreadPoolExecutor
    t = al.t_event
    night = night_of(t)
    hhmmss = datetime.fromtimestamp(t, timezone.utc).strftime("%H%M%S")
    out = out_root / al.id
    out.mkdir(parents=True, exist_ok=True)
    port = cfg.station_api_port
    jobs, skipped = [], []
    near, far = stations_in_reach(al, engine.info, list(cfg.stations))
    for hk, st in cfg.stations.items():
        if not st.should_contact or hk not in near:
            continue
        if st.jump_hosts:
            skipped.append(hk)
            continue
        for cam in st.cameras:
            jobs.append((hk, st, cam.code.upper()))

    def one(job: tuple[str, Any, str]) -> tuple[str, str, int, str]:
        hk, st, cam = job
        path = f"/api/fireball_pack/{cam}/{night}"
        url = f"http://{st.host_for_path(path)}:{port}{path}"
        try:
            with session.get(url, params={"time": hhmmss, "window": PULL_WINDOW_S},
                             timeout=(10, PULL_TIMEOUT_S), stream=True) as r:
                if r.status_code == 404:
                    return hk, cam, 0, "no data"
                if r.status_code != 200:
                    return hk, cam, 0, f"HTTP {r.status_code}"
                dest = out / f"{cam}.zip"
                tmp = dest.with_suffix(".part")
                n = 0
                with open(tmp, "wb") as fh:
                    for chunk in r.iter_content(1 << 20):
                        fh.write(chunk)
                        n += len(chunk)
                os.replace(tmp, dest)
                return hk, cam, n, "ok"
        except Exception as exc:
            return hk, cam, 0, type(exc).__name__

    with ThreadPoolExecutor(PULL_PARALLEL) as ex:
        results = list(ex.map(one, jobs))
    got = [r for r in results if r[3] == "ok"]

    # Clips: only where the watcher reports a working GPU encoder (VAAPI PASS).
    involved = alert_cams(al)
    clip_req, clip_later = [], []
    for hk, st, cam in jobs:
        if cam not in involved:
            continue
        if (engine.status.get(hk) or {}).get("vaapi") != "PASS":
            clip_later.append(cam)
            continue
        name = _request_clip(st, cam, night, t, port, session)
        (clip_req if name else clip_later).append(cam)

    post_summary(al, results, out, engine, telegram, skipped, clip_req, clip_later, involved, far)
    if clip_req:
        threading.Thread(target=_announce_clips, args=(al, clip_req, night, engine, telegram),
                         daemon=True).start()
    fr_bins = {r[1]: fr_files_near(out / f"{r[1]}.zip", t) for r in got}
    own = [cam for cam in fr_bins if cam in involved]
    return {"pulled": len(got), "bytes": sum(r[2] for r in got), "clips": clip_req,
            "fr_bins": sum(fr_bins.values()),
            "alert_packs": len(own), "alert_fr_bins": sum(fr_bins[cam] for cam in own)}


def post_summary(al: Alert, results: list[tuple[str, str, int, str]], out: Path, engine: Engine,
                 telegram: Outlet, skipped: list[str], clip_req: list[str], clip_later: list[str],
                 involved: set[str], far: list[str] | None = None) -> None:
    """Summary text and an album of FF maxpixel thumbnails from the pulled packs."""
    t = al.t_event
    hhmmss = datetime.fromtimestamp(t, timezone.utc).strftime("%H:%M:%S")
    got = [r for r in results if r[3] == "ok"]
    total = sum(r[2] for r in got)
    lines = [f"<b>Fireball data pulled</b>, {hhmmss} UTC"]
    stations = sorted({r[0] for r in got})
    lines.append(html.escape(f"FF/FR +-{PULL_WINDOW_S} s: {len(got)} cameras at {len(stations)} stations, "
                             f"{total / 1e6:.0f} MB, stored under {out}"))
    missing = [r for r in results if r[3] != "ok"]
    if missing:
        lines.append(html.escape("Nothing from: " + ", ".join(
            f"{cam_label(r[1], engine.info)} ({r[3]})" for r in missing[:20])
            + (f" and {len(missing) - 20} more" if len(missing) > 20 else "")))
    if skipped:
        lines.append(html.escape("Tunnel-only, not pulled: " + ", ".join(station_label(h, engine.info) for h in skipped)))
    if far:
        lines.append(html.escape(f"Over {PULL_MAX_DIST_KM:.0f} km away, not pulled: "
                                 + ", ".join(station_label(h, engine.info) for h in far)))
    if clip_req:
        lines.append(html.escape("Clips requested now (GPU encode): " + ", ".join(cam_label(c, engine.info) for c in clip_req)
                                 + "; links follow when they reach the archive."))
    if clip_later:
        lines.append(html.escape("Clips at dawn (no GPU encoder, so no encoding at night): "
                                 + ", ".join(cam_label(c, engine.info) for c in clip_later)))
    telegram.send("\n".join(lines), "pulled")
    order = sorted(got, key=lambda r: (r[1] not in involved, r[1]))
    photos = []
    for _hk, cam, _, _ in order:
        if len(photos) >= MAX_THUMBS:
            break
        jpg = thumbnail_from_pack(out / f"{cam}.zip", t)
        if jpg:
            photos.append((f"{cam_label(cam, engine.info)}, FF maxpixel around {hhmmss} UTC", jpg))
    telegram.send_photos(photos, "thumbs")


def _request_clip(st: Any, cam: str, night: str, t: float, port: int, session: Any) -> str | None:
    path = f"/api/chunks/{cam}/{night}"
    try:
        data = session.get(f"http://{st.host_for_path(path)}:{port}{path}", timeout=15).json()
    except Exception:
        return None
    rows = data.get("chunks") if isinstance(data, dict) else data
    best = None
    for row in rows or []:
        name = row.get("filename") if isinstance(row, dict) else row
        m = re.match(r"^[A-Z0-9]+_(\d{8})_(\d{6})_color\.mkv$", name or "")
        if not m:
            continue
        t0 = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).timestamp()
        if t0 <= t and (best is None or t0 > best[0]):
            best = (t0, name)
    if best is None or t - best[0] > 600:
        return None
    path = f"/api/process_clip/{cam}/{night}/{best[1]}"
    try:
        r = session.post(f"http://{st.host_for_path(path)}:{port}{path}", timeout=15)
    except Exception:
        return None
    return best[1] if r.status_code in (200, 202, 409) else None


def clip_url(cam: str, night: str, name: str, has_mp4: bool) -> str:
    """Public link to an archived clip. /media/v1 takes the night as YYYY-MM-DD (a YYYYMMDD night
    404s), and the .mp4 sibling plays in phone browsers and Telegram where .mkv does not."""
    if has_mp4:
        name = name.replace(".mkv", ".mp4")
    return f"{PUBLIC_BASE}/media/v1/clip/{cam}/{night[:4]}-{night[4:6]}-{night[6:]}/{name}"


def _announce_clips(al: Alert, cams: list[str], night: str, engine: Engine, telegram: Outlet) -> None:
    archive = Path(os.environ.get("ROVIMEN_ARCHIVE_PATH", "/srv/rovimen/archive"))
    deadline = time.time() + 1200
    found: dict[str, str] = {}
    while time.time() < deadline and len(found) < len(cams):
        for cam in cams:
            if cam in found:
                continue
            d = archive / cam / night / "meteors"
            try:
                names = sorted(n for n in os.listdir(d) if n.endswith("_color.mkv"))
            except OSError:
                continue
            for n in names:
                m = re.match(r"^[A-Z0-9]+_(\d{8})_(\d{6})_", n)
                if not m:
                    continue
                t0 = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).timestamp()
                if t0 <= al.t_event < t0 + 600:
                    found[cam] = clip_url(cam, night, n, os.path.exists(d / n.replace(".mkv", ".mp4")))
        time.sleep(30)
    if found and al.state != "withdrawn":
        telegram.send("\n".join([f"<b>Fireball clips</b>, {al.id}"] +
                                [html.escape(f"{cam_label(c, engine.info)}: {u}") for c, u in sorted(found.items())]),
                      "clips")


# ── daemon ────────────────────────────────────────────────────────────

def tracks_from_alert(al: Alert, info: dict[str, CamInfo]) -> list[dict[str, Any]]:
    """One ground track per camera from the FR pairs: the first and last point of what that
    camera saw (``ll_a``/``ll_b``) with the heights there. The longest track wins when a camera
    sits in more than one pair."""
    best: dict[str, dict[str, Any]] = {}
    for p in al.pairs:
        g = p.get("geo") or {}
        for side in ("a", "b"):
            cam = p[side]["cam"]
            ll, hh = g.get(f"ll_{side}"), g.get(f"h_{side}")
            if not (isinstance(ll, list) and len(ll) == 2 and isinstance(hh, list) and len(hh) == 2):
                continue
            track = {"label": cam_label(cam, info), "cam": cam, "begin": (ll[0][0], ll[0][1]),
                     "end": (ll[1][0], ll[1][1]), "h_begin": float(hh[0]), "h_end": float(hh[1])}
            if cam not in best or (hh[0] - hh[1]) > (best[cam]["h_begin"] - best[cam]["h_end"]):
                best[cam] = track
    return list(best.values())


def render_alert_maps(al: Alert, engine: Engine, kind: str) -> list[bytes]:
    """Two JPEG maps of the alert's ground track, the whole country first, then the trail close up;
    empty without an FR pair (no position to draw)."""
    import fireball_map
    tracks = tracks_from_alert(al, engine.info)
    if not tracks:
        return []
    cams = sorted({p[k]["cam"] for p in al.pairs for k in ("a", "b")})
    cameras = [(cam_label(c, engine.info), engine.info[c].lat, engine.info[c].lon)
               for c in cams if c in engine.info and engine.info[c].lat is not None]
    when = datetime.fromtimestamp(al.t_event, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    title = (f"Fireball {when} UTC, {kind} ground track from the FR pair geometry\n"
             f"arrow: the trail from the highest to the lowest point seen; triangles: the cameras")
    return [fireball_map.render_track_map(tracks, cameras, title, overview=True),
            fireball_map.render_track_map(tracks, cameras, title)]


def send_track_map(telegram: Outlet, al: Alert, engine: Engine, kind: str) -> bool:
    """Render and send the maps for a preliminary or confirmed alert as one album. Never raises."""
    try:
        maps = render_alert_maps(al, engine, kind)
    except Exception:
        logger.exception("track map for %s failed", al.id)
        return False
    if not maps:
        return False
    hhmmss = datetime.fromtimestamp(al.t_event, timezone.utc).strftime("%H:%M:%S")
    bp = engine._best_pair(al)
    cap = [f"{kind.capitalize()} ground track, {hhmmss} UTC"]
    if bp:
        cap.append(engine._pair_line(bp))
    cap.append("Arrow: the trail from the highest to the lowest point seen, heights in km at its ends; "
               "thin lines: what each camera saw; triangles: the cameras.")
    return telegram.send_photos([("\n".join(cap), maps[0]), ("Close-up of the trail", maps[1])], f"{kind}_map")


def load_registry() -> tuple[dict[str, Any], Any]:
    import yaml
    from config_file import dashboard_config_path
    from models import DashboardConfig
    raw = yaml.safe_load(dashboard_config_path().read_text())
    return raw, DashboardConfig.model_validate(raw)


# ── Signals (LED, buzzer, sound on a Pi) ──────────────────────────────

def signal_payload(msg: Message, info: dict[str, CamInfo]) -> dict[str, Any]:
    """What a signal receiver gets: enough to decide on/off and to say where."""
    al = msg.alert
    cams = alert_cams(al)
    stations = sorted({info[c].station for c in cams if c in info})
    return {
        "kind": msg.kind,
        "id": al.id,
        "utc": datetime.fromtimestamp(al.t_event, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cameras": sorted(cams),
        "stations": [station_label(hk, info) for hk in stations],
        "text": html.unescape(re.sub(r"<[^>]+>", "", msg.text)),
    }


# Prefix of the machine-readable signal line posted to Telegram signal chats;
# tools/fireball_signal.py matches it.
SIGNAL_LINE_PREFIX = "FIREBALL_SIGNAL "


class Signals:
    """POST each alert message to the configured receivers, never blocking the loop."""

    def __init__(self, urls: list[str], secret: str,
                 telegram_chats: list[str] | None = None, telegram_token: str = "") -> None:
        self.urls = urls
        self.secret = secret
        self.telegram_chats = [c for c in (telegram_chats or []) if c] if telegram_token else []
        self.telegram_token = telegram_token

    def send(self, msg: Message, info: dict[str, CamInfo]) -> list[threading.Thread]:
        if not self.urls and not self.telegram_chats:
            return []
        body = signal_payload(msg, info)
        threads = [threading.Thread(target=self._post, args=(u, body), daemon=True) for u in self.urls]
        threads += [threading.Thread(target=self._post_telegram, args=(c, body), daemon=True)
                    for c in self.telegram_chats]
        for t in threads:
            t.start()
        return threads

    def _post(self, url: str, body: dict[str, Any]) -> None:
        import requests
        try:
            r = requests.post(url, json=body, headers={"X-Rovimen-Signal": self.secret}, timeout=5)
            if r.status_code >= 300:
                logger.error("signal %s HTTP %d", url, r.status_code)
        except Exception as exc:
            logger.error("signal %s failed: %s", url, type(exc).__name__)

    def _post_telegram(self, chat_id: str, body: dict[str, Any]) -> None:
        """One plain-text line (no parse mode, so no escaping) a receiver parses."""
        import requests
        text = SIGNAL_LINE_PREFIX + json.dumps({k: v for k, v in body.items() if k != "text"},
                                               ensure_ascii=False)
        try:
            r = requests.post(f"{TELEGRAM_API}/bot{self.telegram_token}/sendMessage",
                              data={"chat_id": chat_id, "text": text,
                                    "disable_web_page_preview": "true"}, timeout=10)
            if r.status_code >= 300:
                logger.error("signal telegram chat %s HTTP %d: %s", chat_id, r.status_code, r.text[:200])
        except Exception as exc:
            logger.error("signal telegram chat %s failed: %s", chat_id, type(exc).__name__)


def signals_from_env() -> Signals:
    urls = re.split(r"[\s,]+", os.environ.get("ROVIMEN_FIREBALL_SIGNAL_URLS", "").strip())
    chats = re.split(r"[\s,]+", os.environ.get("ROVIMEN_FIREBALL_SIGNAL_TELEGRAM_CHAT_IDS", "").strip())
    token = (os.environ.get("ROVIMEN_FIREBALL_TELEGRAM_BOT_TOKEN")
             or os.environ.get("ROVIMEN_TELEGRAM_BOT_TOKEN", "")).strip()
    return Signals([u for u in urls if u], os.environ.get("ROVIMEN_FIREBALL_SIGNAL_SECRET", "").strip(),
                   telegram_chats=[c for c in chats if c], telegram_token=token)


def run_daemon(poll_s: float = 1.0) -> int:
    import requests
    import fireball_store
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    raw, cfg = load_registry()
    engine = Engine(cam_info_from_config(raw))
    # Every outgoing message goes through the Broadcaster: Telegram (private
    # chat and the members' channel), Discord and email, each with its route.
    import alert_channels
    telegram = alert_channels.broadcaster_from_env()
    if telegram.dry_run:
        logger.warning("no alert channel configured: alerts are logged, not sent")
    else:
        logger.info("alert channels: %s", ", ".join(telegram.channel_names))
    signals = signals_from_env()
    if signals.urls or signals.telegram_chats:
        logger.info("signals to %d receiver(s) and %d Telegram chat(s)",
                    len(signals.urls), len(signals.telegram_chats))
    session = requests.Session()
    # Platepars: what watchers sent (newest per camera), then the stations' own
    # /api/platepar for cameras without one, refreshed every 6 h.
    for cam, (_st, ev) in fireball_store.platepars().items():
        engine.set_platepar(cam, ev.get("pp") or {}, float(ev.get("mtime") or 0))
    last_pp_fetch = 0.0
    last_cfg = time.time()
    last_id = fireball_store.last_id()
    logger.info("fireball_alert started: %d cameras, %d platepars, from event id %d",
                len(engine.info), len(engine.cameras), last_id)
    while True:
        try:
            now = time.time()
            if now - last_cfg > 600:
                last_cfg = now
                try:
                    raw, cfg = load_registry()
                    fresh = cam_info_from_config(raw)
                    for cam, ci in fresh.items():
                        old = engine.info.get(cam)
                        if old is not None and old.lat is not None and cam in engine.cameras:
                            ci.lat, ci.lon = old.lat, old.lon
                        engine.info[cam] = ci
                except Exception:
                    logger.exception("registry reload failed")
            if now - last_pp_fetch > 6 * 3600:
                last_pp_fetch = now

                def _fetch(cfg=cfg) -> None:
                    for cam, pp in fetch_station_platepars(cfg, session).items():
                        if cam not in engine.cameras:
                            engine.set_platepar(cam, pp, 0.0)
                threading.Thread(target=_fetch, daemon=True).start()
            for rid, _rec, station, ev in fireball_store.read_since(last_id):
                last_id = rid
                engine.add(station, ev, now)
            for msg in engine.tick(now):
                logger.info("%s alert %s", msg.kind, msg.alert.id)
                signals.send(msg, engine.info)
                telegram.send(msg.text, msg.kind)
                if msg.kind in ("preliminary", "confirmed") and msg.alert.pairs:
                    threading.Thread(target=send_track_map, args=(telegram, msg.alert, engine, msg.kind),
                                     daemon=True).start()
                if msg.kind == "confirmed":
                    threading.Thread(target=_safe_pull, args=(msg.alert, cfg, engine, telegram, signals),
                                     daemon=True).start()
            if int(now) % 3600 == 0:
                fireball_store.prune()
        except Exception:
            logger.exception("fireball_alert loop error")
            time.sleep(10)
        time.sleep(poll_s)


def format_withdrawn(al: Alert, reason: str) -> str:
    hhmmss = datetime.fromtimestamp(al.t_event, timezone.utc).strftime("%H:%M:%S")
    day = datetime.fromtimestamp(al.t_event, timezone.utc).strftime("%Y-%m-%d")
    return "\n".join([f"<b>Correction: false alarm</b>, {day} {hhmmss} UTC",
                      html.escape(f"The fireball alert for {day} {hhmmss} UTC is withdrawn: {reason}")])


def withdraw(al: Alert, reason: str, telegram: Outlet, signals: "Signals", info: dict[str, CamInfo]) -> None:
    """Take back a confirmed alert on every channel that got it, and send the signal
    receivers a retraction (the lamp goes off)."""
    al.state = "withdrawn"
    text = format_withdrawn(al, reason)
    logger.info("withdrawn alert %s: %s", al.id, reason)
    telegram.send(text, "withdrawn")
    signals.send(Message("retracted", al, text, time.time()), info)


def _safe_pull(al: Alert, cfg: Any, engine: Engine, telegram: Outlet, signals: "Signals") -> None:
    import requests
    try:
        with requests.Session() as s:
            res = pull_event(al, cfg, engine, s, EVENTS_DIR, telegram)
        logger.info("pull %s: %s", al.id, res)
    except Exception:
        logger.exception("pull for %s failed", al.id)
        return
    reason = withdraw_reason(al, res)
    if reason:
        withdraw(al, reason, telegram, signals, engine.info)


def withdraw_reason(al: Alert, res: dict[str, Any]) -> str | None:
    """Why a confirmed alert was a false alarm, judged from the pulled packs (the stations'
    own files), or None to let it stand.

    Only a fieldsum alert without an FR pair qualifies: a pair exists because two stations
    already sent FR tracks. The verdict rests on the alert's own cameras alone, and on at
    least WITHDRAW_MIN_PACKS of them: a camera outside the alert may not have seen that part
    of the sky, and an alert camera whose pack is slow or missing proves nothing. No FR bin
    in those packs means RMS saw no bright moving object there, so the alert was a
    coincidence of sky brightenings."""
    if al.pairs or res["alert_packs"] < WITHDRAW_MIN_PACKS or res["alert_fr_bins"]:
        return None
    return (f"none of the {res['alert_packs']} cameras in the alert whose data arrived recorded a bright "
            "moving object (no FR files), so the brightenings were a coincidence, not a fireball.")


def withdraw_cli(alert_id: str, reason: str) -> int:
    """Withdraw a past alert by hand: every channel plus the signal receivers."""
    import alert_channels
    env = alert_channels.read_env_file(Path("/etc/rovimen-dashboard.env"))
    env.update(alert_channels.load_settings())
    for k, v in env.items():
        os.environ.setdefault(k, v)
    t = datetime.strptime(alert_id, "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc).timestamp()
    al = Alert(alert_id, t, time.time(), state="confirmed")
    telegram = alert_channels.broadcaster_from_env()
    signals = signals_from_env()
    withdraw(al, reason, telegram, signals, {})
    ok = telegram.flush()
    time.sleep(2)   # signal threads are daemons
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--poll", type=float, default=1.0)
    ap.add_argument("--withdraw", metavar="EVENT_ID",
                    help="withdraw a confirmed alert (YYYYMMDD_HHMMSS) on every channel and turn the lamp off")
    ap.add_argument("--reason", default="it was a false alarm, not a fireball.")
    args = ap.parse_args(argv)
    if args.withdraw:
        return withdraw_cli(args.withdraw, args.reason)
    return run_daemon(args.poll)


if __name__ == "__main__":
    sys.exit(main())
