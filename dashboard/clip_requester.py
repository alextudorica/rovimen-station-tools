#!/usr/bin/env python3
"""Ask stations for the detection clips the archive is missing.

A station with ``services.reencode.meteor_clips: false`` (gmnro05 (Bistrița)
since 2026-09-24) encodes no meteor clips at dawn, so none reach the archive:
every multi-station event it witnessed would open without its camera, and its
single-station detections would stay "clip pending" until the janitor deletes
them after ``locked_days``. This job finds every such detection and asks the
station to deliver it, multi-station witnesses first, through the
same ``POST /api/process_clip`` a Process click uses, with ``?idle=1`` so the
station refuses while its dawn run is still going.

Each pass (``rovimen-clip-requester.timer``, every 10 minutes):

1. Correlate the last ``--nights`` nights whose clips are due (from 12:00 UTC
   the next day, see ``READY_HOUR_UTC``; before that hour, last night too
   for a station whose probe says ``clips_at_dawn: false``) exactly as the
   Events page does (``event_correlation``), on every detection-index row
   rather than one per chunk, so a dimmer second meteor in a chunk still
   counts.
2. Keep the detections (every witness, then every single-station
   detection) whose clip is not in the archive: no ``meteors/`` entry
   for the chunk, no stitched splice spanning the meteor, not pruned by the
   retention janitor, on a camera with a clip pipeline, at a station the
   dashboard may contact.
3. For each station where the sun is up (a margin after sunrise, before
   sunset), probe once with a read-only GET (an older bundle without
   ``?idle=1`` answers 405 and is skipped), then POST up to
   ``--max-per-station`` of them, witnesses first, then brightest first. The station encodes them
   one at a time (``station_api._process_clip_serial``).

Outcomes are kept in a small JSON state file under ``cache/`` (the deploy
rsync leaves it alone): 409 means delivered, 404 means the chunk is gone from
the station, 503 means dawn is busy (stop for this pass), and a clip still
missing half an hour after a request is asked for again, up to
``MAX_ATTEMPTS``. Any other 4xx, to the probe or the POST, refuses that one
clip: it is asked again after ``REFUSED_RETRY_S`` (a station that cannot take
the name yet may get a bundle that can) and given up after ``MAX_REFUSALS``,
and the pass goes on to the next clip. A refused clip must never hold the
station's queue: in October 2026 a stitched splice name that gmnro05
(Bistrița) answered 400 to was probed first on every pass, and nothing was
requested for two nights while its janitor deleted unarchived detections.

Runs standalone from a systemd timer, never inside the gunicorn worker.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from atomic_write import write_atomic  # noqa: E402
import detection_db  # noqa: E402
from config_file import dashboard_config_path, load_config  # noqa: E402
from astronomy import _compute_twilight  # noqa: E402
from event_correlation import correlate_detections, resolve_stitched_chunk, station_coords  # noqa: E402
from models import DashboardConfig, StationConfig  # noqa: E402

logger = logging.getLogger("clip_requester")

ARCHIVE_PATH = Path(os.environ.get("ROVIMEN_ARCHIVE_PATH", "/srv/rovimen/archive"))
STATE_PATH = Path(os.environ.get(
    "ROVIMEN_CLIP_REQUEST_STATE", "/opt/rovimen/cache/clip_requests.json"))

DEFAULT_NIGHTS = 2
# 20 clips at ~11 s each on VAAPI (gmnro05 (Bistrița), 09-24 encoder.log) is
# ~4 min of encoding, done well inside one 10-minute pass.
DEFAULT_MAX_PER_STATION = 20
# A clip still missing this long after a request is asked for again: the
# station's queue dies with the 13:45 UTC reboot, and an upload can fail.
REREQUEST_AFTER_S = 1800
MAX_ATTEMPTS = 6
# A clip the station refused (a 4xx other than 404/409) is asked again after
# this long, and given up after MAX_REFUSALS refusals. The span has to outlast
# a bundle rollout (stations update at 14:00/14:15/16:45 UTC, and passes run in
# daylight only): a station that refuses a name its next bundle accepts must
# still be asked once it has that bundle. On 2026-10-05, 2 h x 3 would have
# given up on gmnro05 (Bistrița)'s 61 splices at 12:11, before its 14:00 update.
REFUSED_RETRY_S = 3 * 3600
MAX_REFUSALS = 5
# Entries older than this are dropped from the state file.
STATE_KEEP_S = 8 * 86400
HTTP_TIMEOUT_S = 15
# Daylight window at the station: start after sunrise (dawn has barely begun)
# and stop well before sunset, so no encode overlaps the evening capture start.
SUNRISE_MARGIN_MIN = 30
SUNSET_MARGIN_MIN = 45
# A night's clips are asked for from this hour (UTC) the next day. By then
# every station that encodes at dawn has encoded and uploaded (the slowest,
# gmnro08 (Brașov), finished stacking at 11:23 on 2026-09-27), so a request
# never races archive_upload's staggered morning burst; it only fills what the
# normal pipeline will not deliver.
READY_HOUR_UTC = 12

# Terminal outcomes: never asked again.
DONE = "done"
GONE = "gone"
FAILED = "failed"
REQUESTED = "requested"
# Not terminal: asked again after REFUSED_RETRY_S.
REFUSED = "refused"


def early_night(now: datetime) -> str | None:
    """Last night, before its clips are due: asked for only at stations whose
    dawn encodes no meteor clips (probe ``clips_at_dawn: false``), as soon as
    their dawn is done. On gmnro05 (Bistrița) that is about 10:40 UTC, and its
    one-at-a-time encoder (about 34 s a clip with its details video, measured
    2026-11-02) needs the extra hours: ~170 detections a night against ~240
    clips between 12:00 UTC and the evening cut-off."""
    if now.hour >= READY_HOUR_UTC:
        return None
    return (now - timedelta(days=1)).strftime("%Y%m%d")


def recent_nights(now: datetime, nights: int) -> list[str]:
    """Night keys (UTC start date) of the last ``nights`` nights whose clips
    are due, i.e. whose next day has reached ``READY_HOUR_UTC``."""
    newest = now - timedelta(days=1 if now.hour >= READY_HOUR_UTC else 2)
    return [(newest - timedelta(days=d)).strftime("%Y%m%d") for d in range(nights)]


def daylight_at(station: StationConfig, now: datetime) -> bool:
    """True inside the station's daylight window (sunrise + margin to sunset -
    margin, UTC minutes). A station without coordinates is never asked."""
    if station.lat is None or station.lon is None:
        return False
    tw = _compute_twilight(station.lat, station.lon, now.strftime("%Y%m%d"), horizon=0.0)
    minute = now.hour * 60 + now.minute
    return (tw["sunrise_min"] + SUNRISE_MARGIN_MIN
            <= minute
            <= tw["sunset_min"] - SUNSET_MARGIN_MIN)


class Archive:
    """What the archive holds for a camera-night: ``meteors/`` names (one
    listing, no per-file stat over sshfs) and the janitor's pruned chunks."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._names: dict[tuple[str, str], frozenset[str] | None] = {}
        self._pruned: dict[tuple[str, str], frozenset[str]] = {}

    def meteor_names(self, cam: str, night: str) -> frozenset[str] | None:
        """None when the listing fails for a reason other than "no clips yet"
        (the box is unmounted): the caller must not treat that as missing."""
        key = (cam, night)
        if key not in self._names:
            d = self._root / cam / night / "meteors"
            try:
                self._names[key] = frozenset(os.listdir(d))
            except FileNotFoundError:
                self._names[key] = (frozenset() if (self._root / cam).is_dir()
                                    else None)
            except OSError:
                self._names[key] = None
        return self._names[key]

    def pruned(self, cam: str, night: str) -> frozenset[str]:
        key = (cam, night)
        if key not in self._pruned:
            names: set[str] = set()
            try:
                state = json.loads((self._root / cam / night / "state.json").read_text())
                for name, info in (state.get("chunks") or {}).items():
                    if isinstance(info, dict) and info.get("video_pruned"):
                        names.add(name)
            except (OSError, ValueError, AttributeError):
                pass
            self._pruned[key] = frozenset(names)
        return self._pruned[key]


def _brightness(row: dict[str, Any]) -> float:
    mag = row.get("mag_apparent")
    if mag is None:
        mag = row.get("mag_absolute")
    return mag if mag is not None else float("inf")


def wanted_clips(config: DashboardConfig, nights: list[str],
                 rows: list[dict[str, Any]], archive: Archive) -> dict[str, list[dict[str, Any]]]:
    """Missing detection clips, by host key: multi-station witnesses first,
    then single-station detections, each group brightest first.

    Each entry is ``{"cam", "night", "chunk", "mag", "multi"}``, one per chunk.
    """
    cam_to_host: dict[str, str] = {}
    cam_has_clips: dict[str, bool] = {}
    for hk, st in config.stations.items():
        for cam in st.cameras:
            if cam.code:
                cam_to_host[cam.code.upper()] = hk
                cam_has_clips[cam.code.upper()] = cam.clips

    by_night: dict[str, list[dict[str, Any]]] = {n: [] for n in nights}
    for row in rows:
        cam = (row.get("cam") or "").upper()
        hk = cam_to_host.get(cam)
        night = row.get("date")
        if hk is None or night not in by_night or not row.get("time_utc"):
            continue
        by_night[night].append({
            "host_key": hk,
            "cam": cam,
            "meteor_time": row["time_utc"].replace(" ", "T"),
            "chunk_file": row.get("chunk_file"),
            "mag": _brightness(row),
            **{k: row.get(k) for k in ("azim_beg", "elev_beg", "azim_end", "elev_end")},
        })

    wanted: dict[str, dict[tuple[str, str, str], dict[str, Any]]] = {}

    def consider(det: dict[str, Any], night: str, multi: bool) -> None:
        cam, chunk, hk = det["cam"], det.get("chunk_file"), det["host_key"]
        station = config.stations[hk]
        if not chunk or not chunk.endswith("_color.mkv"):
            return
        if not station.should_contact or not cam_has_clips.get(cam, True):
            return
        names = archive.meteor_names(cam, night)
        if names is None:
            return
        if chunk in names or resolve_stitched_chunk(det["meteor_time"], names):
            return
        if chunk in archive.pruned(cam, night):
            return
        key = (cam, night, chunk)
        prev = wanted.setdefault(hk, {}).get(key)
        if prev is None:
            wanted[hk][key] = {"cam": cam, "night": night, "chunk": chunk,
                               "mag": det["mag"], "multi": multi}
        else:
            prev["mag"] = min(prev["mag"], det["mag"])
            prev["multi"] = prev["multi"] or multi

    coords = station_coords(config)
    for night, dets in by_night.items():
        dets.sort(key=lambda d: d["meteor_time"])
        for event in correlate_detections(dets, config.correlation_window_s, coords):
            for w in event["witnesses"]:
                consider(w, night, multi=True)
        # Every detection, not only witnesses: a station that encodes no
        # meteor clips at dawn (meteor_clips: false) gets them all archived
        # this way, and on any other station a clip still missing at
        # READY_HOUR_UTC is one its dawn failed to deliver.
        for det in dets:
            consider(det, night, multi=False)
    return {hk: sorted(items.values(), key=lambda c: (not c["multi"], c["mag"]))
            for hk, items in wanted.items()}


def load_state(path: Path) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(path: Path, state: dict[str, dict[str, Any]], now_s: float) -> None:
    kept = {k: v for k, v in state.items()
            if now_s - float(v.get("last", 0)) < STATE_KEEP_S}
    write_atomic(path, json.dumps(kept, indent=1, sort_keys=True), mkdir=True,
                 tmp=path.with_name(f".{path.name}.{os.getpid()}.tmp"))


def _due(entry: dict[str, Any] | None, now_s: float) -> bool:
    if entry is None:
        return True
    if entry.get("status") in (DONE, GONE, FAILED):
        return False
    wait = REFUSED_RETRY_S if entry.get("status") == REFUSED else REREQUEST_AFTER_S
    return now_s - float(entry.get("last", 0)) >= wait


def _refuse(state: dict[str, dict[str, Any]], key: str, now_s: float, code: int,
            counts: dict[str, int], hk: str) -> None:
    """Record a 4xx refusal of one clip: asked again later, FAILED after MAX_REFUSALS."""
    entry = state.get(key) or {}
    refusals = int(entry.get("refusals", 0)) + 1
    status = FAILED if refusals >= MAX_REFUSALS else REFUSED
    state[key] = {**entry, "status": status, "last": now_s, "http": code,
                  "refusals": refusals, "attempts": int(entry.get("attempts", 0))}
    counts["failed" if status == FAILED else "refused"] += 1
    logger.warning("%s: %s refused with HTTP %d (%d/%d)%s", hk, key, code, refusals,
                   MAX_REFUSALS, "; giving up" if status == FAILED else "; asking again later")


def request_url(config: DashboardConfig, station: StationConfig, clip: dict[str, Any]) -> str:
    path = f"/api/process_clip/{clip['cam']}/{clip['night']}/{clip['chunk']}"
    return f"http://{station.host_for_path(path)}:{config.station_api_port}{path}?idle=1"


def probe_station(config: DashboardConfig, station: StationConfig,
                  clip: dict[str, Any], session: Any) -> tuple[str, int | None, dict[str, Any]]:
    """One read-only GET before any POST: (verdict, HTTP status, body), the
    verdict one of ``ok``, ``old_bundle``, ``busy``, ``error`` or ``refused``.

    A bundle older than the ``?idle=1`` support answers the GET with 405, and
    would take a POST at once, during dawn, one thread per clip; such a
    station is left alone until its updater brings the new bundle. Any other
    4xx is about this clip (the station refuses its name), not the station:
    ``refused``, and the caller probes with the next clip.
    """
    url = request_url(config, station, clip).split("?", 1)[0]
    try:
        resp = session.get(url, timeout=HTTP_TIMEOUT_S)
    except requests.RequestException as exc:
        logger.warning("%s: station unreachable (%s); next pass", station.label, exc)
        return "error", None, {}
    code = resp.status_code
    if code == 405:
        return "old_bundle", code, {}
    if 400 <= code < 500:
        return "refused", code, {}
    if code != 200:
        return "error", code, {}
    try:
        body = resp.json()
    except ValueError:
        return "error", code, {}
    if not isinstance(body, dict):
        return "error", code, {}
    return ("busy" if body.get("dawn_running") else "ok"), code, body


def request_station(config: DashboardConfig, hk: str, clips: list[dict[str, Any]],
                    state: dict[str, dict[str, Any]], now_s: float, max_posts: int,
                    session: Any, dry_run: bool = False,
                    early: list[dict[str, Any]] | None = None) -> dict[str, int]:
    """POST the due clips for one station; update ``state`` in place.

    ``early`` (last night, before READY_HOUR_UTC) goes after ``clips`` and
    only to a station whose probe answers ``clips_at_dawn: false``.

    Stops at the first answer that says the station can't take work now
    (dawn busy, unreachable, 5xx), so a busy station costs one request a pass.
    A 4xx refuses only that clip, and the pass goes on with the next one.
    """
    station = config.stations[hk]
    counts = {"requested": 0, "done": 0, "gone": 0, "failed": 0, "refused": 0,
              "busy": 0, "error": 0, "old_bundle": 0}
    early_due = [c for c in (early or [])
                 if _due(state.get(f"{c['cam']}/{c['night']}/{c['chunk']}"), now_s)]
    due = [c for c in clips if _due(state.get(f"{c['cam']}/{c['night']}/{c['chunk']}"), now_s)]
    due += early_due
    if not dry_run:
        verdict, body = "ok", {}
        while due:
            verdict, code, body = probe_station(config, station, due[0], session)
            if verdict != "refused":
                break
            clip = due.pop(0)
            _refuse(state, f"{clip['cam']}/{clip['night']}/{clip['chunk']}", now_s,
                    code or 400, counts, hk)
        if verdict == "ok" and early_due and body.get("clips_at_dawn") is not False:
            early_ids = {id(c) for c in early_due}
            due = [c for c in due if id(c) not in early_ids]
        if not due:
            return counts
        if verdict != "ok":
            counts[verdict] += 1
            return counts
    for clip in due:
        if counts["requested"] >= max_posts:
            break
        key = f"{clip['cam']}/{clip['night']}/{clip['chunk']}"
        entry = state.get(key)
        attempts = int((entry or {}).get("attempts", 0))
        if attempts >= MAX_ATTEMPTS:
            state[key] = {**(entry or {}), "status": FAILED, "last": now_s}
            counts["failed"] += 1
            logger.warning("%s: giving up on %s after %d requests", hk, key, attempts)
            continue
        url = request_url(config, station, clip)
        if dry_run:
            logger.info("%s: would request %s (mag %s)", hk, key, clip["mag"])
            counts["requested"] += 1
            continue
        try:
            resp = session.post(url, timeout=HTTP_TIMEOUT_S)
        except requests.RequestException as exc:
            counts["error"] += 1
            logger.warning("%s: station unreachable (%s); next pass", hk, exc)
            break
        code = resp.status_code
        if code in (200, 202):
            # 202: already running from an earlier request or a click.
            state[key] = {"status": REQUESTED, "last": now_s,
                          "attempts": attempts + (1 if code == 200 else 0)}
            counts["requested"] += 1
        elif code == 409:
            state[key] = {"status": DONE, "last": now_s, "attempts": attempts}
            counts["done"] += 1
        elif code == 404:
            state[key] = {"status": GONE, "last": now_s, "attempts": attempts}
            counts["gone"] += 1
        elif code == 503:
            counts["busy"] += 1
            break
        elif 400 <= code < 500:
            _refuse(state, key, now_s, code, counts, hk)
        else:
            counts["error"] += 1
            logger.warning("%s: HTTP %d from the station; next pass", hk, code)
            break
    return counts


def run(config: DashboardConfig, now: datetime, *, nights: int, max_per_station: int,
        archive_root: Path, state_path: Path, session: Any,
        rows: list[dict[str, Any]] | None = None, dry_run: bool = False) -> dict[str, dict[str, int]]:
    night_keys = recent_nights(now, nights)
    early_key = early_night(now)
    if rows is None:
        rows = detection_db.query_detections(night_keys + ([early_key] if early_key else []))
    archive = Archive(archive_root)
    wanted = wanted_clips(config, night_keys, rows, archive)
    early = wanted_clips(config, [early_key], rows, archive) if early_key else {}
    state = load_state(state_path)
    now_s = now.timestamp()
    summary: dict[str, dict[str, int]] = {}
    for hk in sorted(set(wanted) | set(early)):
        clips, early_clips = wanted.get(hk, []), early.get(hk, [])
        missing = len(clips) + len(early_clips)
        station = config.stations[hk]
        if station.jump_hosts:
            logger.info("%s: %d missing clips, but the station is tunnel-only; skipped",
                        hk, missing)
            continue
        if not daylight_at(station, now):
            summary[hk] = {"missing": missing, "night": 1}
            continue
        counts = request_station(config, hk, clips, state, now_s, max_per_station,
                                 session, dry_run=dry_run, early=early_clips)
        summary[hk] = {"missing": missing, **counts}
        logger.info("%s (%s): %d missing, %s", hk, station.label, missing,
                    ", ".join(f"{k} {v}" for k, v in counts.items() if v))
    if not dry_run:
        save_state(state_path, state, now_s)
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--nights", type=int, default=DEFAULT_NIGHTS)
    ap.add_argument("--max-per-station", type=int, default=DEFAULT_MAX_PER_STATION)
    ap.add_argument("--dry-run", action="store_true",
                    help="list what would be requested; no POST, no state write")
    ap.add_argument("--now", help="pretend UTC time (ISO), for --dry-run checks")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    now = (datetime.fromisoformat(args.now).replace(tzinfo=timezone.utc)
           if args.now else datetime.now(timezone.utc))
    config = load_config(dashboard_config_path())
    with requests.Session() as session:
        run(config, now, nights=args.nights, max_per_station=args.max_per_station,
            archive_root=ARCHIVE_PATH, state_path=STATE_PATH, session=session,
            dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
