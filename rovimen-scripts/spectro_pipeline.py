#!/usr/bin/env python3
"""Meteor spectroscopy pipeline for the GH662S grating camera.

The spectro camera records continuous 20 s color chunks. Since 2026-09-25 it
runs at 25 fps with its own RMS instance (``rms-cam5``, station ID GH662S,
upload off), so RMS detects the zero order directly, anywhere in the frame;
before that it took 2-5 s exposures and had no detection of its own. The
colour side still treats it as ``rms: false`` (sun-scheduled capture).

CUT   After each night, take the spectro camera's OWN detections (trigger
      ``self``; the grating's dispersed copies RMS also picks up are dropped)
      and the co-pointed witness cameras' detections projected through the
      solved pointing model (dropped when an own detection covers the same
      meteor). At video rate a few-second window is stream-copied out of the
      retention-managed capture directory; long-exposure chunks are copied
      whole (plus one neighbour each side). Both land in
      ``~/spectro_events/<night>/<event_id>/`` with an ``event.json``.

      At video rate REDUCE decodes the window, finds the zero order in every
      frame, walks the dispersion axis from each, and co-adds; see the
      "REDUCE at video rate" section. The rest of this docstring describes the
      long-exposure path, kept for nights recorded before the switch.

REDUCE  For each saved event: extract real frames (``-vsync passthrough`` --
      the stream's nominal fps lies about the 5 s exposures), isolate the
      transient by frame differencing, locate the zero-order streak near the
      predicted track, extract the first-order spectrum along the dispersion
      axis, and write ``spectrum.csv`` (wavelength-calibrated with the
      configured dispersion, refined on Na/Mg lines when present),
      ``spectrum.png`` and an annotated detection frame.

Pointing model: gnomonic camera at fixed (az, alt) with roll and per-axis
scale, solved astrometrically (see tools/solve_spectro_pointing.py) and
stored in config.json under ``stations.<code>.spectro``:

    "spectro": {
        "trigger_stations": ["RO000A", "RO000B"],
        "az_center": 32.21, "alt_center": 69.53,
        "scale_x": 25.26, "scale_y": 24.36,
        "basis": [[...],[...],[...]],   # written by the solver: c, R, U rows
        "dispersion_px_per_nm": 0.78,
        "dispersion_axis": [1.0, 0.0],
        "zero_order_to_400nm_px": 312
    }
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

import rovimen_config
import rovimen_media
import rovimen_night

logger = logging.getLogger("spectro_pipeline")

FRAME_W, FRAME_H = 1280, 720
CHUNK_SECONDS = 20
NA_NM, MG_NM = 589.3, 517.3


# ── pointing model ─────────────────────────────────────────────────────────

@dataclass
class SpectroModel:
    basis: np.ndarray       # rows: c (optical axis), R (image +x), U (image +y up)
    scale_x: float
    scale_y: float
    dispersion_px_per_nm: float
    dispersion_axis: tuple[float, float]
    zero_order_to_400nm_px: float
    trigger_stations: list[str]

    @classmethod
    def from_config(cls, cfg: dict) -> "SpectroModel":
        return cls(
            basis=np.array(cfg["basis"], dtype=float),
            scale_x=float(cfg["scale_x"]),
            scale_y=float(cfg["scale_y"]),
            dispersion_px_per_nm=float(cfg.get("dispersion_px_per_nm", 0.78)),
            dispersion_axis=tuple(cfg.get("dispersion_axis", [1.0, 0.0])),
            zero_order_to_400nm_px=float(cfg.get("zero_order_to_400nm_px", 312)),
            trigger_stations=list(cfg.get("trigger_stations", [])),
        )

    def azalt_to_px(self, az_deg: float, alt_deg: float) -> tuple[float, float] | None:
        az, al = np.radians(az_deg), np.radians(alt_deg)
        v = np.array([np.cos(al) * np.sin(az), np.cos(al) * np.cos(az), np.sin(al)])
        c, R, U = self.basis
        d = v @ c
        if d < 0.2:
            return None
        px = FRAME_W / 2 + np.degrees(np.arctan((v @ R) / d)) * self.scale_x
        # U is image-UP (the solver samples the pixel 100 rows above centre)
        # and rows grow downward, so the U term is subtracted. Adding it
        # flipped every track about the centre row: on 2026-09-24 3 of 64
        # V<5 stars landed within 10 px of a star with the flip, 22 without.
        py = FRAME_H / 2 - 1 - np.degrees(np.arctan((v @ U) / d)) * self.scale_y
        return float(px), float(py)


# ── FTPdetectinfo parsing ──────────────────────────────────────────────────

def parse_ftpdetect(path: Path) -> list[dict]:
    """Detections with FF timestamp, measured az/alt track endpoints, and the
    per-frame centroids ``[frame, x, y, level]`` in the camera's own pixels.

    Block layout (RMS ``writeFTPdetectinfo``): FF name, a calibration line
    ("Uncalibrated" without a platepar), the detection header whose 4th field
    is the fps, then one row per frame.
    """
    out = []
    lines = path.read_text(errors="replace").splitlines()
    i = 0
    while i < len(lines):
        if lines[i].startswith("FF_"):
            ff = lines[i].strip()
            pts, frames = [], []
            fps = 25.0
            if i + 2 < len(lines):
                head = lines[i + 2].split()
                try:
                    fps = float(head[3])
                except (IndexError, ValueError):
                    pass
            j = i + 3
            while j < len(lines) and not lines[j].startswith(("FF_", "----")):
                parts = lines[j].split()
                if len(parts) >= 9:
                    try:
                        pts.append((float(parts[5]), float(parts[6])))
                        frames.append([float(parts[0]), float(parts[1]),
                                       float(parts[2]), float(parts[7])])
                    except ValueError:
                        pass
                j += 1
            m = re.match(r"FF_(\w+)_(\d{8})_(\d{6})_(\d+)_", ff)
            if pts and m:
                out.append({
                    "ff": ff, "station": m.group(1),
                    "date": m.group(2), "hms": m.group(3),
                    "ff_ms": int(m.group(4)),
                    "fps": fps,
                    "track_azalt": [list(pts[0]), list(pts[-1])],
                    "n_points": len(pts),
                    "frames": frames,
                })
            i = j
        else:
            i += 1
    return out


def detection_utc(det: dict) -> tuple[float, float]:
    """Epoch seconds of a detection's first and last frame.

    The FF name carries the block's start (to the ms); frame numbers count
    from it at the camera's fps. Using the FF time alone puts the meteor up to
    10 s early -- the block is 256 frames long.
    """
    t_ff = datetime.strptime(det["date"] + det["hms"], "%Y%m%d%H%M%S").replace(
        tzinfo=timezone.utc).timestamp() + det.get("ff_ms", 0) / 1000.0
    frames = det.get("frames") or [[0.0]]
    fps = det.get("fps") or 25.0
    return t_ff + frames[0][0] / fps, t_ff + frames[-1][0] / fps


def drop_dispersed_copies(dets: list[dict], model: "SpectroModel") -> list[dict]:
    """Remove detections that are the grating's dispersed image of another.

    On the grating camera RMS sees the zero order AND, for a bright meteor,
    its first order (or a strong emission line) as a second trail: the same
    frames, displaced along the dispersion axis by roughly the 400-780 nm
    offset. Only the zero order is an event; the copy is its spectrum.
    """
    dx, dy = model.dispersion_axis
    lo = 0.5 * model.zero_order_to_400nm_px
    hi = model.zero_order_to_400nm_px + 400.0 * model.dispersion_px_per_nm + 100.0
    drop = set()
    for a_i, a in enumerate(dets):
        fa = {round(f[0]): f for f in a.get("frames", [])}
        for b_i, b in enumerate(dets):
            if a_i == b_i or b["ff"] != a["ff"] or b_i in drop:
                continue
            common = [(fa[round(f[0])], f) for f in b.get("frames", [])
                      if round(f[0]) in fa]
            if len(common) < 2:
                continue
            along = np.median([(fb[1] - fa_[1]) * dx + (fb[2] - fa_[2]) * dy
                               for fa_, fb in common])
            across = np.median([abs(-(fb[1] - fa_[1]) * dy + (fb[2] - fa_[2]) * dx)
                                for fa_, fb in common])
            if lo <= along <= hi and across <= 25.0:
                drop.add(b_i)
    return [d for i, d in enumerate(dets) if i not in drop]


def track_in_fov(model: SpectroModel, det: dict, margin: float = 60.0) -> list | None:
    """Sampled pixel track if the detection crosses the spectro FOV."""
    (az0, el0), (az1, el1) = det["track_azalt"]
    pts = []
    for f in np.linspace(0, 1, 9):
        az = az0 + f * ((az1 - az0 + 180) % 360 - 180)
        p = model.azalt_to_px(az % 360, el0 + f * (el1 - el0))
        if p and -margin <= p[0] < FRAME_W + margin and -margin <= p[1] < FRAME_H + margin:
            pts.append([round(p[0], 1), round(p[1], 1)])
    return pts if len(pts) >= 2 else None


# ── CUT stage ──────────────────────────────────────────────────────────────

def chunks_for_time(capture_night_dir: Path, date: str, hms: str,
                    pad_chunks: int = 1) -> list[Path]:
    """Capture chunks covering the FF time, plus pad_chunks neighbours."""
    want = int(hms[:2]) * 3600 + int(hms[2:4]) * 60 + int(hms[4:6])
    rows = []
    for p in sorted(capture_night_dir.glob("*_color.mkv")):
        m = re.match(r".*_(\d{8})_(\d{6})_color\.mkv", p.name)
        if not m or m.group(1) != date:
            continue
        t = m.group(2)
        ts = int(t[:2]) * 3600 + int(t[2:4]) * 60 + int(t[4:6])
        rows.append((ts, p))
    rows.sort()
    hit = [k for k, (ts, _) in enumerate(rows) if ts <= want < ts + CHUNK_SECONDS + 2]
    if not hit:
        return []
    k = hit[0]
    lo, hi = max(0, k - pad_chunks), min(len(rows), k + pad_chunks + 1)
    return [p for _, p in rows[lo:hi]]


# A camera at or above this rate records meteors as moving points (25 fps
# after 2026-09-25); below it each frame is a multi-second exposure holding the
# whole trail, which the legacy single-frame reducer handles.
VIDEO_RATE_MIN_FPS = 5.0
# Window rescued around a video-rate event: background frames before, slack
# for the capture pipeline's latency (~4 frames) and keyframe-aligned seeking.
WINDOW_PRE_S = 3.0
WINDOW_POST_S = 3.0
WINDOW_MAX_S = 15.0
# A witness-camera trigger within this many seconds of one of the spectro
# camera's own detections is the same meteor; the own detection wins because
# it carries the zero order's per-frame position directly.
SELF_DEDUP_S = 3.0


def _chunk_start_utc(p: Path) -> float | None:
    """Epoch start of a regular capture chunk (stitched clips are not chunks here)."""
    start = rovimen_night.chunk_start_utc(p.name, stitched=False)
    return None if start is None else start.timestamp()


def chunks_for_window(capture_night_dir: Path, t0: float, t1: float) -> list[Path]:
    """Chunks overlapping the epoch window [t0, t1], in time order."""
    rows = sorted((t, p) for p in capture_night_dir.glob("*_color.mkv")
                  if (t := _chunk_start_utc(p)) is not None)
    return [p for t, p in rows if t < t1 and t + CHUNK_SECONDS + 2 > t0]


def chunk_fps(mkv: Path) -> float:
    """Real frame rate of a chunk: packets over duration. The container's
    nominal rate is not trustworthy on this camera (it reported 25 fps for
    5 s exposures)."""
    data = rovimen_media.ffprobe_json(mkv, timeout=60, args=("-count_packets",),
                                      entries="stream=nb_read_packets:format=duration")
    try:
        packets = float(rovimen_media.first_stream(data)["nb_read_packets"])
        duration = float((data or {})["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        return 0.0
    return packets / duration if duration > 0 else 0.0


def _keyframe_at_or_before(mkv: Path, t: float) -> float:
    """Time of the last keyframe at or before ``t`` s into ``mkv`` (0 on error).

    Stream copy can only start on a keyframe. The camera's own chunks have a
    1 s GOP, but a chunk the dawn pass re-encoded in place can have one keyframe
    in 10 s, and a blind -ss would then shift the whole window early by up to
    that much and cut the meteor off its end.
    """
    data = rovimen_media.ffprobe_json(mkv, timeout=60, args=("-skip_frame", "nokey"),
                                      entries="frame=pts_time")
    kfs: list[float] = []
    for frame in (data or {}).get("frames") or []:
        try:
            k = float(frame["pts_time"])
        except (KeyError, TypeError, ValueError):
            continue  # "N/A"
        if k >= 0.0:
            kfs.append(k)
    before = [k for k in kfs if k <= t + 1e-3]
    return max(before) if before else 0.0


def cut_window(chunks: list[Path], t_start: float, t_end: float, out: Path) -> dict:
    """Stream-copy [t_start - PRE, t_end + POST] out of the covering chunks.

    Stream copy keeps the camera's own encode (nothing is re-quantised) and
    costs ~1 MB/s instead of a 20 s chunk per event. The copy starts on the
    keyframe at or before the requested start and is lengthened by the same
    amount, so the requested span is always inside the window; the reducer
    aligns frames by content, never by timestamp.
    """
    t0_chunk = _chunk_start_utc(chunks[0])
    want = max(0.0, t_start - WINDOW_PRE_S - t0_chunk)
    ss = _keyframe_at_or_before(chunks[0], want)
    dur = min(WINDOW_MAX_S,
              (want - ss) + (t_end - t_start) + WINDOW_PRE_S + WINDOW_POST_S)
    lst = out.with_suffix(".concat.txt")
    lst.write_text("".join(f"file '{c.resolve()}'\n" for c in chunks))
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0",
         # One frame past the keyframe: seeking exactly onto it lands on the
         # keyframe BEFORE, which would silently shorten the window's end.
         "-ss", f"{ss + 0.04 if ss > 0 else 0.0:.3f}", "-i", str(lst), "-t", f"{dur:.3f}",
         "-c", "copy", str(out)],
        check=True, timeout=120)
    lst.unlink(missing_ok=True)
    return {"file": out.name, "t0_nominal_utc": t0_chunk + ss, "duration_s": dur,
            "chunks": [c.name for c in chunks]}


def _rescue(ev_dir: Path, night_dir: Path, det: dict, legacy_hms: str
            ) -> dict | None:
    """Save the recording for one event: a window at video rate, the covering
    chunks (plus one neighbour each side) for legacy long exposures. Returns
    the event.json fields describing it, or None when the chunks are gone."""
    t0, t1 = detection_utc(det)
    covering = chunks_for_window(night_dir, t0 - WINDOW_PRE_S, t1 + WINDOW_POST_S)
    if not covering:
        return None
    fps = chunk_fps(covering[0])
    if fps >= VIDEO_RATE_MIN_FPS:
        ev_dir.mkdir(parents=True, exist_ok=True)
        win = ev_dir / "window.mkv"
        window = None
        if win.exists():
            try:
                window = json.loads((ev_dir / "event.json").read_text()).get("window")
            except (OSError, ValueError):
                window = None
        if window is None:
            window = cut_window(covering, t0, t1, win)
        return {"fps": round(fps, 2), "window": window, "chunks": [],
                "t_start_utc": t0, "t_end_utc": t1}
    chunks = chunks_for_time(night_dir, det["date"], legacy_hms)
    if not chunks:
        return None
    ev_dir.mkdir(parents=True, exist_ok=True)
    for c in chunks:
        if not (ev_dir / c.name).exists():
            shutil.copy2(c, ev_dir / c.name)
    return {"fps": round(fps, 2), "chunks": [c.name for c in chunks],
            "t_start_utc": t0, "t_end_utc": t1}


def _capture_root(config: dict, home: Path) -> Path:
    """The colour-capture root, from the same key chain as every station script.

    Reading only ``videocapture_path`` missed stations that name the root with
    one of the other four keys, and an empty ``videocapture_path`` resolved to
    the working directory.
    """
    if any(config.get(key) for key in rovimen_config.CAPTURE_ROOT_KEYS):
        return rovimen_config.capture_root(config)
    return home / "color_capture"


def _ftpdetect_files(home: Path, night: str, stations: set[str]):
    for cam_dir in sorted(home.glob("RMS_data/cam*/ArchivedFiles/*")):
        m = re.match(r"(\w+)_(\d{8})_\d{6}_\d+$", cam_dir.name)
        if not m or m.group(2) != night or m.group(1) not in stations:
            continue
        fps = [p for p in cam_dir.glob("FTPdetectinfo_*.txt")
               if "unfiltered" not in p.name and "uncal" not in p.name
               and "backup" not in p.name]
        if fps:
            yield m.group(1), fps[0]


def run_cut(config: dict, spectro_code: str, night: str, home: Path) -> list[Path]:
    """Rescue the recording around every meteor the spectro camera can hold.

    Two trigger sources: the spectro camera's OWN RMS detections (trigger
    ``self``; its zero order, per frame, in its own pixels), and the witness
    RMS cameras in ``trigger_stations`` projected through the pointing model.
    A witness event that coincides with an own detection is dropped.
    """
    scfg = config["stations"][spectro_code].get("spectro")
    if not scfg:
        logger.error("no spectro model configured for %s", spectro_code)
        return []
    model = SpectroModel.from_config(scfg)
    capture_root = _capture_root(config, home)
    night_dir = capture_root / spectro_code / night
    if not night_dir.is_dir():
        logger.warning("no capture night dir: %s", night_dir)
        return []

    events_out = []
    own_times: list[float] = []
    for _, ftp in _ftpdetect_files(home, night, {spectro_code}):
        for det in drop_dispersed_copies(parse_ftpdetect(ftp), model):
            t0, t1 = detection_utc(det)
            own_times.append(t0)
            # Named by the meteor's own start, not the FF block's: two
            # meteors in one 10 s block must not share a directory.
            event_id = spectro_code + datetime.fromtimestamp(
                t0, timezone.utc).strftime("_%Y%m%d_%H%M%S")
            ev_dir = home / "spectro_events" / night / event_id
            saved = _rescue(ev_dir, night_dir, det, det["hms"])
            if saved is None:
                logger.info("own detection %s: chunks already pruned", det["ff"])
                continue
            f0, f1 = det["frames"][0], det["frames"][-1]
            (ev_dir / "event.json").write_text(json.dumps(dict({
                "event_id": event_id, "ff": det["ff"], "night": night,
                "trigger": "self", "spectro_code": spectro_code,
                "fps_ff": det["fps"], "frames": det["frames"],
                "track_px": [[f0[1], f0[2]], [f1[1], f1[2]]],
                "n_points": det["n_points"],
            }, **saved), indent=1))
            events_out.append(ev_dir)
            logger.info("cut own event %s: %d frames", event_id, det["n_points"])

    witnesses = set(model.trigger_stations) - {spectro_code}
    for _, ftp in _ftpdetect_files(home, night, witnesses):
        for det in parse_ftpdetect(ftp):
            track = track_in_fov(model, det)
            if not track:
                continue
            t0, _ = detection_utc(det)
            if any(abs(t0 - t) <= SELF_DEDUP_S for t in own_times):
                logger.info("witness %s: same meteor as an own detection", det["ff"])
                continue
            event_id = f"{det['station']}_{det['date']}_{det['hms']}"
            ev_dir = home / "spectro_events" / night / event_id
            saved = _rescue(ev_dir, night_dir, det, det["hms"])
            if saved is None:
                logger.info("event %s crosses FOV but chunks already pruned", det["ff"])
                continue
            (ev_dir / "event.json").write_text(json.dumps(dict({
                "event_id": event_id, "ff": det["ff"], "night": night,
                "track_px": track, "track_azalt": det["track_azalt"],
                "n_points": det["n_points"], "spectro_code": spectro_code,
            }, **saved), indent=1))
            events_out.append(ev_dir)
            logger.info("cut event %s: track %s -> %s", event_id, track[0], track[-1])
    return events_out


# ── WIDE trigger: streak detection on the widefield long-exposure frames ──
#
# OFF by default in the ``night`` run (opt in with ``"widefield_trigger": true``
# in the spectro block; ``wide`` still runs it on demand). Over nights
# 20260920-24 it cut 230 of 255 events and 25 of the 26 "spectra", every one a
# moonlit cloud, and it has never caught a meteor the RMS cameras confirmed.
#
# RMS cannot digest 5-10 s exposures (its detector works on 25 fps FF blocks),
# but at these exposures a meteor is a complete trail in a SINGLE frame, so a
# frame-difference + Hough line pass is sufficient. Rejections encode the three
# main impostors: aircraft strobes make DASHED trails (low filled fraction
# along the fitted segment); satellites and moonlit cloud edges persist across
# frames (any overlapping line within the last WIDE_PERSIST_FRAMES frames);
# and cloud edges are BROAD, unlike a meteor trail that is a few pixels wide
# (perpendicular off-line flux comparable to on-line flux). The first spectro
# night (20260917) produced 7 false spectra, all moonlit clouds, all of which
# fail the thinness and persistence tests below.

def _px_to_azalt(model: SpectroModel, px: float, py: float) -> tuple[float, float]:
    c, R, U = model.basis
    tx = np.tan(np.radians((px - FRAME_W / 2) / model.scale_x))
    ty = np.tan(np.radians((FRAME_H / 2 - 1 - py) / model.scale_y))
    v = c + tx * R + ty * U
    v /= np.linalg.norm(v)
    az = float(np.degrees(np.arctan2(v[0], v[1])) % 360)
    alt = float(np.degrees(np.arcsin(np.clip(v[2], -1, 1))))
    return az, alt


# A frame yielding more than this many DISTINCT thin objects is a cloud field;
# a real meteor frame has 1, occasionally 2 with a simultaneous satellite.
# Count objects, never raw Hough segments: one bright trail routinely fits as
# 5+ overlapping segments (verified on the RO000A 21:14:27 meteor), so capping
# raw fits would reject exactly the events this pipeline exists to catch.
WIDE_MAX_STREAKS_PER_FRAME = 4
# Persistence window for the satellite/cloud veto, in frames (10 s exposures:
# a meteor exists in exactly one).
WIDE_PERSIST_FRAMES = 3


def detect_streaks(frame: np.ndarray, prev: np.ndarray,
                   min_len: float = 25.0, thresh: float = 12.0,
                   max_off_ratio: float = 0.5) -> list[dict]:
    """Streak segments in one long-exposure frame (both arrays grayscale)."""
    import cv2

    diff = np.clip(frame - prev, 0, 255)
    binary = (diff > thresh).astype(np.uint8) * 255
    binary = cv2.dilate(binary, np.ones((3, 3), np.uint8))
    lines = cv2.HoughLinesP(binary, 1, np.pi / 180, threshold=30,
                            minLineLength=int(min_len), maxLineGap=6)
    out = []
    if lines is None:
        return out
    h, w = diff.shape
    # OpenCV 4.x returns (N, 1, 4); 5.0 returns (N, 4). Reshape covers both —
    # the fleet is on 4.12 today, so the 5.x form is latent, not theoretical.
    for x0, y0, x1, y1 in np.asarray(lines).reshape(-1, 4):
        n = max(int(np.hypot(x1 - x0, y1 - y0)), 1)
        xs = np.linspace(x0, x1, n).astype(int)
        ys = np.linspace(y0, y1, n).astype(int)
        filled = float((diff[ys, xs] > thresh * 0.6).mean())
        if filled < 0.65:
            continue  # dashed: aircraft strobes
        on_line = float(diff[ys, xs].mean())
        # thinness veto: a meteor trail is a few px wide, a cloud edge is a
        # broad ramp. Compare the flux ±6 px perpendicular to the segment
        # against the flux on it; clouds keep most of it, trails keep little.
        ux, uy = (x1 - x0) / n, (y1 - y0) / n
        px, py = -uy, ux  # unit perpendicular
        off_vals = []
        for sign in (-6, 6):
            ox = np.clip((xs + sign * px).astype(int), 0, w - 1)
            oy = np.clip((ys + sign * py).astype(int), 0, h - 1)
            off_vals.append(float(diff[oy, ox].mean()))
        off_ratio = max(off_vals) / max(on_line, 1e-6)
        if off_ratio > max_off_ratio:
            continue  # broad ramp: cloud edge, not a trail
        out.append({"p0": [int(x0), int(y0)], "p1": [int(x1), int(y1)],
                    "length": n, "filled": round(filled, 2),
                    "off_ratio": round(off_ratio, 2),
                    "mean_excess": round(on_line, 1)})
    return out


def _same_line(a: dict, b: dict, tol: float = 40.0) -> bool:
    """Loose spatial overlap: either endpoint OR the midpoint of ``a`` lies
    within ``tol`` px of ``b``'s segment ends or midpoint. Cloud edges and
    satellites drift and re-fit differently between 10 s frames, so the old
    both-endpoints-within-15-px test let them through."""
    def _mid(s: dict) -> list[float]:
        return [(s["p0"][0] + s["p1"][0]) / 2, (s["p0"][1] + s["p1"][1]) / 2]

    pts_a = (a["p0"], a["p1"], _mid(a))
    pts_b = (b["p0"], b["p1"], _mid(b))
    return any(np.hypot(pa[0] - pb[0], pa[1] - pb[1]) <= tol
               for pa in pts_a for pb in pts_b)


def cluster_streaks(streaks: list[dict]) -> list[dict]:
    """Collapse overlapping Hough fits of the same object into one entry,
    keeping the longest segment of each cluster as its representative.

    HoughLinesP returns several partly-overlapping segments for one bright
    trail; without this every count (the cloud-field cap) and every trigger
    would be per-fragment instead of per-object.
    """
    clusters: list[list[dict]] = []
    for s in sorted(streaks, key=lambda x: -x["length"]):
        for c in clusters:
            if any(_same_line(s, m) for m in c):
                c.append(s)
                break
        else:
            clusters.append([s])
    return [max(c, key=lambda x: x["length"]) for c in clusters]


def run_wide_trigger(config: dict, wide_code: str, spectro_code: str,
                     night: str, home: Path) -> list[Path]:
    """Scan the widefield night for streaks; cut spectro chunks where a streak
    maps into the spectro FOV."""
    w_model = SpectroModel.from_config(config["stations"][wide_code]["spectro"])
    s_model = SpectroModel.from_config(config["stations"][spectro_code]["spectro"])
    capture_root = _capture_root(config, home)
    w_night = capture_root / wide_code / night
    s_night = capture_root / spectro_code / night
    if not w_night.is_dir():
        logger.warning("no widefield night dir: %s", w_night)
        return []

    import tempfile

    from collections import deque

    events = []
    prev_gray, prev_name = None, ""
    history: deque[list[dict]] = deque(maxlen=WIDE_PERSIST_FRAMES)
    for chunk in sorted(w_night.glob("*_color.mkv")):
        with tempfile.TemporaryDirectory() as td:
            try:
                frames = extract_frames(chunk, Path(td))
            except subprocess.SubprocessError:
                continue
            for fpath in frames:
                gray = _load_gray(fpath)
                if prev_gray is not None:
                    # Cluster first: the cap and the triggers both count
                    # OBJECTS, not the several Hough fits one trail produces.
                    streaks = cluster_streaks(detect_streaks(gray, prev_gray))
                    # cloud-field veto: a frame full of "streaks" is a cloud
                    # deck, not meteors. Keep them in the history so the next
                    # frames' persistence veto sees them, but trigger nothing.
                    if len(streaks) > WIDE_MAX_STREAKS_PER_FRAME:
                        logger.info("wide frame %s: %d objects, cloud field — skipped",
                                    fpath.name, len(streaks))
                        history.append(streaks)
                        prev_gray, prev_name = gray, fpath.name
                        continue
                    # persistence veto (satellites, slow cloud edges): drop
                    # streaks overlapping anything seen in the last
                    # WIDE_PERSIST_FRAMES frames.
                    seen = [p for fr in history for p in fr]
                    fresh = [s for s in streaks
                             if not any(_same_line(s, p) for p in seen)]
                    for s in fresh:
                        # map endpoints into the spectro frame
                        in_s = []
                        for p in (s["p0"], s["p1"]):
                            az, alt = _px_to_azalt(w_model, *p)
                            sp = s_model.azalt_to_px(az, alt)
                            if sp and -60 <= sp[0] < FRAME_W + 60 and -60 <= sp[1] < FRAME_H + 60:
                                in_s.append([round(sp[0], 1), round(sp[1], 1)])
                        if not in_s:
                            continue
                        m = re.match(r".*_(\d{8})_(\d{6})_color", chunk.name)
                        event_id = f"WIDE_{m.group(1)}_{m.group(2)}"
                        ev_dir = home / "spectro_events" / night / event_id
                        ev_dir.mkdir(parents=True, exist_ok=True)
                        for c in chunks_for_time(s_night, m.group(1), m.group(2)):
                            if not (ev_dir / c.name).exists():
                                shutil.copy2(c, ev_dir / c.name)
                        (ev_dir / "event.json").write_text(json.dumps({
                            "event_id": event_id, "night": night,
                            "trigger": "widefield_streak",
                            "wide_chunk": chunk.name, "wide_frame": fpath.name,
                            "wide_streak": s, "track_px": in_s if len(in_s) >= 2
                            else in_s * 2,
                            "chunks": [c.name for c in
                                       chunks_for_time(s_night, m.group(1), m.group(2))],
                            "spectro_code": spectro_code,
                        }, indent=1))
                        # The widefield frame lives in a TemporaryDirectory
                        # that is gone after this chunk, so the preview has to
                        # be written here or the streak that fired the trigger
                        # is unreviewable afterwards.
                        try:
                            write_preview(gray, ev_dir / "wide.jpg",
                                          lines=[[s["p0"], s["p1"]]])
                        except Exception as exc:
                            logger.warning("wide preview skipped: %s", exc)
                        events.append(ev_dir)
                        logger.info("wide trigger %s: streak len=%d filled=%.2f off=%.2f",
                                    event_id, s["length"], s["filled"], s["off_ratio"])
                    history.append(streaks)
                prev_gray, prev_name = gray, fpath.name
    return events


# ── REDUCE stage ───────────────────────────────────────────────────────────

# On-track excess (ADU over the median frame) that counts as a transient.
# Below it the event still gets a preview image and an index entry -- it just
# reads as "nothing found" rather than being invisible.
TRANSIENT_MIN_SCORE = 3.0

# The pointing model is a tangent plane with no distortion terms, so a
# projected track lands up to ~30 px off the real zero order near the frame
# edge (median star residual 17 px on 2026-09-24). Search a band this wide on
# either side of the prediction, not the single predicted pixel line.
TRACK_SEARCH_PX = 60
TRACK_SEARCH_STEP_PX = 2

# A zero-order meteor trail is ~6 px wide; a moonlit cloud is hundreds. The
# line is scored against the BRIGHTER of two flanks this far out, so a broad
# brightening or a cloud edge scores ~0 and only a thin ridge counts.
RIDGE_FLANK_PX = 12
RIDGE_MIN_SAMPLES = 20

# Below this fraction of the 380-780 nm walk inside the frame, the first
# order fell off the sensor: the event is a transient without a measurable
# spectrum, whatever the controls say.
SPECTRUM_MIN_COVERAGE = 0.25

# Background control for the dispersed-light measurement. The shifts are
# perpendicular to the dispersion axis, far enough to clear the zero order and
# its wings but still inside the same sky and the same cloud field. The
# threshold is a ratio against the MEDIAN control, so one control that happens
# to cross a bright cloud cannot veto a real spectrum on its own.
SPECTRUM_CONTROL_SHIFTS_PX = (-120.0, -80.0, -40.0, 40.0, 80.0, 120.0)
SPECTRUM_MIN_CONTRAST = 1.5

# Previews are the only picture most events have (a spectrum is extracted for a
# handful), so every event writes one. JPEG at this width keeps a night's worth
# under a few MB on the archive; the science products stay lossless.
PREVIEW_MAX_W = 960
PREVIEW_QUALITY = 85


def write_preview(img: np.ndarray, out: Path, lines: list | tuple = ()) -> None:
    """Write a contrast-stretched JPEG preview of *img*, tracing *lines* on it.

    Percentile stretch rather than a fixed gain: these are median-subtracted
    difference frames whose useful range varies by three orders of magnitude
    between a bright fireball and an empty sky, and a fixed gain renders most
    of them as black rectangles.
    """
    from PIL import Image, ImageDraw

    a = np.asarray(img, dtype=float)
    lo, hi = float(np.percentile(a, 1.0)), float(np.percentile(a, 99.7))
    if hi <= lo:
        hi = lo + 1.0
    a = np.clip((a - lo) / (hi - lo), 0.0, 1.0) * 255.0
    mode = "L" if a.ndim == 2 else "RGB"
    im = Image.fromarray(a.astype(np.uint8), mode=mode).convert("RGB")
    draw = ImageDraw.Draw(im)
    for pts in lines:
        if len(pts) >= 2:
            draw.line([(float(x), float(y)) for x, y in pts],
                      fill=(255, 72, 72), width=2)
    if im.width > PREVIEW_MAX_W:
        im = im.resize((PREVIEW_MAX_W,
                        max(1, round(im.height * PREVIEW_MAX_W / im.width))),
                       Image.LANCZOS)
    im.save(out, "JPEG", quality=PREVIEW_QUALITY, optimize=True)


# One chunk's frame extraction (a 20 s chunk, ~500 PNGs at 25 fps) may take
# this long, like decode_window's: a stalled ffmpeg (a hung USB capture disk)
# fails this chunk instead of holding the night's spectro run.
EXTRACT_TIMEOUT_S = 600


def extract_frames(mkv: Path, out_dir: Path) -> list[Path]:
    """Every frame of *mkv* as a PNG in *out_dir*. Raises CalledProcessError
    when ffmpeg fails and TimeoutExpired, with the partial frames removed, when
    it runs past EXTRACT_TIMEOUT_S."""
    pattern = out_dir / (mkv.stem + "_%03d.png")
    try:
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(mkv),
             "-vsync", "passthrough", str(pattern)],
            check=True, timeout=EXTRACT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        for partial in out_dir.glob(mkv.stem + "_*.png"):
            partial.unlink(missing_ok=True)
        raise
    return sorted(out_dir.glob(mkv.stem + "_*.png"))


def _load_gray(path: Path) -> np.ndarray:
    from PIL import Image
    return np.asarray(Image.open(path).convert("L"), dtype=float)[:FRAME_H]


def _load_rgb(path: Path) -> np.ndarray:
    from PIL import Image
    return np.asarray(Image.open(path).convert("RGB"), dtype=float)[:FRAME_H]


def _track_samples(track: list, n: int = 200) -> tuple[np.ndarray, np.ndarray]:
    """``n`` points along the track's end-to-end line, and its unit normal."""
    t = np.asarray(track, dtype=float)
    p0, p1 = t[0], t[-1]
    d = p1 - p0
    length = float(np.hypot(d[0], d[1]))
    normal = np.array([-d[1], d[0]]) / length if length > 0 else np.array([0.0, 1.0])
    return p0 + np.linspace(0.0, 1.0, n)[:, None] * d, normal


def ridge_score(diff: np.ndarray, pts: np.ndarray, normal: np.ndarray,
                shift: float) -> float:
    """Mean excess of the line ``pts + normal*shift`` over its brighter flank.

    Only track positions where the line and both flanks are inside the frame
    count, so a track running off the edge is not diluted by clamped pixels.
    """
    h, w = diff.shape[:2]
    lines = [pts + normal * (shift + s) for s in (0.0, -RIDGE_FLANK_PX, RIDGE_FLANK_PX)]
    ok = np.ones(len(pts), dtype=bool)
    for q in lines:
        ok &= (q[:, 0] >= 0) & (q[:, 0] < w) & (q[:, 1] >= 0) & (q[:, 1] < h)
    if ok.sum() < RIDGE_MIN_SAMPLES:
        return 0.0
    on, lo, hi = (float(diff[q[ok, 1].astype(int), q[ok, 0].astype(int)].mean())
                  for q in lines)
    return on - max(lo, hi)


def find_transient_frame(frames: list[Path], track: list
                        ) -> tuple[int, np.ndarray, float, float] | None:
    """Best on-track frame: ``(index, median-subtracted image, score, shift)``.

    ``shift`` is the perpendicular offset (px, along ``_track_samples``'
    normal) within ``TRACK_SEARCH_PX`` where the ridge scored best. Returns
    the best candidate even when it falls below ``TRANSIENT_MIN_SCORE`` --
    the caller decides what counts as a detection, and a non-detection still
    needs its frame to render a preview. None only when there are no frames.
    """
    if not frames:
        return None
    imgs = [_load_gray(f) for f in frames]
    med = np.median(imgs, axis=0)
    pts, normal = _track_samples(track)
    shifts = np.arange(-TRACK_SEARCH_PX, TRACK_SEARCH_PX + 1, TRACK_SEARCH_STEP_PX)
    best = None
    for k, img in enumerate(imgs):
        diff = img - med
        for shift in shifts:
            s = ridge_score(diff, pts, normal, float(shift))
            if best is None or s > best[1]:
                best = (k, s, float(shift))
    k, s, shift = best
    return k, imgs[k] - med, s, shift


# ── REDUCE at video rate ───────────────────────────────────────────────────
#
# At 25 fps each frame holds the meteor as a short moving point, so its
# first-order spectrum in that frame is a point-source spectrum at a known
# offset from the zero order. Walking the dispersion axis from each frame's
# zero order and co-adding over frames gives the spectrum without the trail
# smearing wavelengths into each other (which a multi-second exposure does
# whenever the trail runs along the dispersion axis), and clouds -- static
# over 40 ms -- cancel in the background median.

ZERO_ORDER_BOX_PX = 4
ALIGN_MIN_EXCESS = 6.0        # ADU: zero-order peak over background, median over frames
TRACE_MIN_EXCESS = 6.0        # ADU: per-frame peak along the found line
TRACE_MIN_FRAMES = 3
TRACE_MIN_MONOTONIC = 0.8     # |corr(frame, position along line)|: a meteor moves one way
SPECTRUM_BAND_PX = 3
VIDEO_CONTROL_SHIFTS_PX = (-90.0, -60.0, -30.0, 30.0, 60.0, 90.0)
BACKGROUND_FRAMES = 60


DECODE_TIMEOUT_S = 600
# Frames assumed when the window's packets cannot be counted: WINDOW_MAX_S at
# 25 fps. The array grows if the decode delivers more.
_WINDOW_FRAMES_GUESS = int(WINDOW_MAX_S * 25)


def _window_frame_count(mkv: Path) -> int | None:
    """Video packets in *mkv* (one per frame), or None when ffprobe fails."""
    stream = rovimen_media.first_stream(rovimen_media.ffprobe_json(
        mkv, timeout=60, args=("-count_packets",), entries="stream=nb_read_packets"))
    try:
        return int(stream["nb_read_packets"])
    except (KeyError, TypeError, ValueError):
        return None


def decode_window(mkv: Path) -> np.ndarray:
    """Every frame of a window as uint8 RGB ``(n, FRAME_H, FRAME_W, 3)``.

    Decoded straight into one array sized from the window's packet count:
    reading ffmpeg's whole output first and wrapping it afterwards held every
    frame twice (~2 GB for a 15 s window) on gmnro01 (Ghirdoveni), whose swap
    is full. Raises CalledProcessError when ffmpeg fails and TimeoutExpired
    when it runs past DECODE_TIMEOUT_S.
    """
    import tempfile
    import threading

    cmd = ["ffmpeg", "-v", "error", "-i", str(mkv), "-vsync", "passthrough",
           "-vf", f"scale={FRAME_W}:{FRAME_H}", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "pipe:1"]
    shape = (FRAME_H, FRAME_W, 3)
    size = FRAME_W * FRAME_H * 3
    frames = np.empty((max(1, _window_frame_count(mkv) or _WINDOW_FRAMES_GUESS), *shape),
                      dtype=np.uint8)
    got = 0
    # stderr to a file, never an unread pipe: a damaged window's per-macroblock
    # errors would fill a pipe and block ffmpeg, and this loop with it.
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err)
        timed_out = threading.Event()

        def _kill() -> None:
            timed_out.set()
            try:
                proc.kill()
            except OSError:
                pass

        timer = threading.Timer(DECODE_TIMEOUT_S, _kill)
        timer.daemon = True
        timer.start()
        try:
            flat = memoryview(frames.reshape(-1))
            while True:
                if got == frames.nbytes:
                    # Full: grow only if the decode has more to give.
                    extra = proc.stdout.read(size)
                    if not extra:
                        break
                    grown = np.empty((2 * len(frames), *shape), dtype=np.uint8)
                    grown[:len(frames)] = frames
                    frames = grown
                    flat = memoryview(frames.reshape(-1))
                    flat[got:got + len(extra)] = extra
                    got += len(extra)
                    continue
                n = proc.stdout.readinto(flat[got:])
                if not n:
                    break
                got += n
        finally:
            timer.cancel()
            proc.stdout.close()
            rc = proc.wait()
        if timed_out.is_set():
            raise subprocess.TimeoutExpired(cmd, DECODE_TIMEOUT_S)
        if rc != 0:
            err.seek(0)
            raise subprocess.CalledProcessError(rc, cmd, stderr=err.read())
    return frames[:got // size]


def _background(frames: np.ndarray) -> np.ndarray:
    """Per-pixel median of up to BACKGROUND_FRAMES spread over the window. A
    meteor sits on any one pixel for a frame or two, so it drops out; stars,
    their spectra and the sky stay."""
    idx = np.unique(np.linspace(0, len(frames) - 1,
                                min(len(frames), BACKGROUND_FRAMES)).astype(int))
    return np.median(frames[idx], axis=0).astype(np.float32)


def _gray(img: np.ndarray) -> np.ndarray:
    return img.astype(np.float32).mean(axis=-1)


def _box(a: np.ndarray, x: float, y: float, r: int) -> tuple[np.ndarray, int, int]:
    x0, y0 = max(0, int(round(x)) - r), max(0, int(round(y)) - r)
    return a[y0:int(round(y)) + r + 1, x0:int(round(x)) + r + 1], x0, y0


# The zero order in one 40 ms frame is a point (a short dash for a fast
# meteor); cloud is not. Score the peak against the brighter pixels of a ring
# around it, so a broad brightening -- or a cloud edge, which lights up half
# the ring -- cancels, while a compact point does not.
POINT_RING_PX = (14, 17)
POINT_RING_PERCENTILE = 75


def _point_excess(g: np.ndarray, x: float, y: float) -> tuple[float, int, int]:
    """``(excess, peak_x, peak_y)`` of a compact source near (x, y) in ``g``."""
    r_in, r_out = POINT_RING_PX
    patch, x0, y0 = _box(g, x, y, r_out)
    if patch.size == 0:
        return -np.inf, int(x), int(y)
    cy, cx = int(round(y)) - y0, int(round(x)) - x0
    yy, xx = np.ogrid[:patch.shape[0], :patch.shape[1]]
    cheb = np.maximum(abs(yy - cy), abs(xx - cx))
    inner = cheb <= ZERO_ORDER_BOX_PX
    ring = (cheb >= r_in) & (cheb <= r_out)
    if not inner.any() or ring.sum() < 8:
        return -np.inf, int(x), int(y)
    vals = np.where(inner, patch, -np.inf)
    j = int(np.argmax(vals))
    py, px = np.unravel_index(j, patch.shape)
    excess = float(patch[py, px] - np.percentile(patch[ring], POINT_RING_PERCENTILE))
    return excess, x0 + int(px), y0 + int(py)


def align_own_detection(frames: np.ndarray, bg_gray: np.ndarray, rms: list
                        ) -> tuple[int, float, list]:
    """Find where the RMS centroids sit in the recording: ``(k0, score, trace)``.

    RMS frame numbers count from its FF block and the colour recording runs a
    few frames behind the RMS feed, so the offset is found from content: the
    window frame ``k0`` where the zero order is brightest at the RMS positions
    (median over frames). The trace refines each position to its local peak.
    """
    n0 = rms[0][0]
    rel = [int(round(f[0] - n0)) for f in rms]
    span = rel[-1]
    n = len(frames)
    if n <= span:
        return 0, 0.0, []
    # excess[k, i]: peak of (frame k - background) around centroid i
    excess = np.full((n, len(rms)), -np.inf, dtype=np.float32)
    for k in range(n):
        g = None
        for i, (_, x, y, _) in enumerate(rms):
            if not (0 <= x < FRAME_W and 0 <= y < FRAME_H):
                continue
            if g is None:
                g = _gray(frames[k]) - bg_gray
            excess[k, i] = _point_excess(g, x, y)[0]
    best_k0, best = 0, -np.inf
    for k0 in range(0, n - span):
        vals = excess[[k0 + r for r in rel], range(len(rms))]
        vals = vals[np.isfinite(vals)]
        if len(vals) and float(np.median(vals)) > best:
            best_k0, best = k0, float(np.median(vals))
    if not np.isfinite(best):
        return 0, 0.0, []
    trace = []
    for (_, x, y, _), r in zip(rms, rel):
        k = best_k0 + r
        e, px, py = _point_excess(_gray(frames[k]) - bg_gray, x, y)
        if e >= TRACE_MIN_EXCESS:
            trace.append([k, float(px), float(py)])
    return best_k0, best, trace


def trace_along_track(frames: np.ndarray, bg_gray: np.ndarray, track: list
                      ) -> tuple[float, float, np.ndarray, list]:
    """Witness-triggered event at video rate: ``(score, shift, maxproj, trace)``.

    The max projection of the background-subtracted window rebuilds the trail
    the way a long exposure would; the ridge search finds it near the
    projected track; each frame then contributes its brightest point on that
    line. A trail whose points do not move one way along the line is not a
    meteor (a cloud edge brightening in place, a blinking aircraft).
    """
    maxproj = np.full(bg_gray.shape, -np.inf, dtype=np.float32)
    for k in range(len(frames)):
        np.maximum(maxproj, _gray(frames[k]) - bg_gray, out=maxproj)
    pts, normal = _track_samples(track)
    best = (0.0, 0.0)
    for shift in np.arange(-TRACK_SEARCH_PX, TRACK_SEARCH_PX + 1, TRACK_SEARCH_STEP_PX):
        sc = ridge_score(maxproj, pts, normal, float(shift))
        if sc > best[0]:
            best = (sc, float(shift))
    score, shift = best
    if score < TRANSIENT_MIN_SCORE:
        return score, shift, maxproj, []
    line_len = int(np.hypot(*(pts[-1] - pts[0])))
    line, _ = _track_samples(track, n=max(2, line_len))
    line = line + normal * shift
    ok = (line[:, 0] >= 0) & (line[:, 0] < FRAME_W) & (line[:, 1] >= 0) & (line[:, 1] < FRAME_H)
    line = line[ok]
    trace, along = [], []
    for k in range(len(frames)):
        g = _gray(frames[k]) - bg_gray
        prof = np.full(len(line), -np.inf, dtype=np.float32)
        for b in range(-SPECTRUM_BAND_PX, SPECTRUM_BAND_PX + 1):
            q = line + normal * b
            qx = q[:, 0].clip(0, FRAME_W - 1).astype(int)
            qy = q[:, 1].clip(0, FRAME_H - 1).astype(int)
            np.maximum(prof, g[qy, qx], out=prof)
        j = int(np.argmax(prof)) if len(prof) else 0
        if len(prof) and prof[j] >= TRACE_MIN_EXCESS:
            e, px, py = _point_excess(g, line[j, 0], line[j, 1])
            if e >= TRACE_MIN_EXCESS:
                trace.append([k, float(px), float(py)])
                along.append(j)
    if len(trace) >= TRACE_MIN_FRAMES:
        with np.errstate(invalid="ignore", divide="ignore"):
            r = np.corrcoef([t[0] for t in trace], along)[0, 1]
        if not np.isfinite(r) or abs(r) < TRACE_MIN_MONOTONIC:
            trace = []
    else:
        trace = []
    return score, shift, maxproj, trace


def extract_video_spectrum(frames: np.ndarray, bg: np.ndarray, trace: list,
                           model: SpectroModel) -> dict:
    """Walk the dispersion axis from every traced zero order and co-add.

    Returns wavelengths, the co-added RGB spectrum, one spectrum per frame
    (for the waterfall), the control walks' totals (the same walk displaced
    perpendicular to the dispersion, through the same sky and cloud) and the
    fraction of the walk that fell on the sensor.
    """
    lam = np.linspace(380.0, 780.0, 360)
    off = model.zero_order_to_400nm_px + (lam - 400.0) * model.dispersion_px_per_nm
    dx, dy = model.dispersion_axis
    nx, ny = -dy, dx
    total = np.zeros((len(lam), 3))
    per_frame = []
    controls = {s: 0.0 for s in VIDEO_CONTROL_SHIFTS_PX}
    inside = samples = 0
    for k, x, y in trace:
        sig = np.clip(frames[int(k)].astype(np.float32) - bg, 0.0, None)

        def walk(shift: float) -> np.ndarray:
            acc = np.zeros((len(lam), 3))
            for b in range(-SPECTRUM_BAND_PX, SPECTRUM_BAND_PX + 1):
                xs = np.round(x + off * dx + nx * (b + shift)).astype(int)
                ys = np.round(y + off * dy + ny * (b + shift)).astype(int)
                ok = (xs >= 0) & (xs < FRAME_W) & (ys >= 0) & (ys < FRAME_H)
                acc[ok] += sig[ys[ok], xs[ok]]
            return acc

        a = walk(0.0)
        total += a
        per_frame.append(a.sum(axis=1))
        for s in VIDEO_CONTROL_SHIFTS_PX:
            controls[s] += float(walk(s).sum())
        xs = x + off * dx
        ys = y + off * dy
        inside += int(((xs >= 0) & (xs < FRAME_W) & (ys >= 0) & (ys < FRAME_H)).sum())
        samples += len(lam)
    return {"lam": lam, "total": total, "per_frame": np.array(per_frame),
            "controls": controls,
            "coverage": inside / samples if samples else 0.0}


def _write_video_spectrum_plot(out: Path, ev_id: str, maxproj: np.ndarray,
                               trace: list, spec: dict, prof: np.ndarray) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(9, 12),
                                        gridspec_kw={"height_ratios": [3, 2, 2]})
    lo, hi = np.percentile(maxproj, 1), np.percentile(maxproj, 99.7)
    ax1.imshow(np.clip((maxproj - lo) / max(hi - lo, 1.0), 0, 1), cmap="gray")
    t = np.array(trace)
    ax1.plot(t[:, 1], t[:, 2], "r.", ms=2)
    ax1.set_title(f"{ev_id}: max projection, {len(trace)} traced frames")
    ax2.imshow(spec["per_frame"], aspect="auto", cmap="magma",
               extent=[spec["lam"][0], spec["lam"][-1], len(trace), 0])
    ax2.set_ylabel("traced frame")
    ax3.plot(spec["lam"], prof, color="k", lw=1.0, label="total")
    for c, col in enumerate(("r", "g", "b")):
        ch = spec["total"][:, c]
        if ch.max() > 0:
            ax3.plot(spec["lam"], ch / spec["total"].sum(axis=1).max(), col, lw=0.6)
    for nm, name in ((NA_NM, "Na"), (MG_NM, "Mg")):
        for ax in (ax2, ax3):
            ax.axvline(nm, color="orange", lw=0.6)
        ax3.text(nm, 1.02, name, ha="center", fontsize=8)
    ax3.set_xlabel("wavelength (nm), configured dispersion")
    ax3.set_ylabel("relative intensity")
    fig.savefig(out, dpi=130, bbox_inches="tight")
    plt.close(fig)


def reduce_video_event(ev_dir: Path, ev: dict, model: SpectroModel) -> dict:
    """Reduce one video-rate event (see the section comment)."""
    base = {"event_id": ev["event_id"], "video_rate": True,
            "fps": ev.get("fps"), "trigger": ev.get("trigger", "rms")}

    def _done(result: dict) -> dict:
        (ev_dir / "reduction.json").write_text(json.dumps(result, indent=1))
        return result

    win = ev_dir / ev["window"]["file"]
    if not win.is_file():
        return _done(dict(base, detected=False, note="window recording missing"))
    frames = decode_window(win)
    if len(frames) < 10:
        return _done(dict(base, detected=False, note="window has %d frames" % len(frames)))
    bg = _background(frames)
    bg_gray = bg.mean(axis=-1)

    if ev.get("trigger") == "self":
        k0, score, trace = align_own_detection(frames, bg_gray, ev["frames"])
        found = score >= ALIGN_MIN_EXCESS and len(trace) >= TRACE_MIN_FRAMES
        maxproj = np.full(bg_gray.shape, -np.inf, dtype=np.float32)
        for k in range(len(frames)):
            np.maximum(maxproj, _gray(frames[k]) - bg_gray, out=maxproj)
        base.update(frame_offset=k0)
    else:
        predicted = ev["track_px"]
        if ev.get("track_azalt"):
            predicted = track_in_fov(model, ev) or predicted
        score, shift, maxproj, trace = trace_along_track(frames, bg_gray, predicted)
        found = bool(trace)
        base.update(shift_px=round(shift, 1))
    base.update(score=round(float(score), 2), n_frames=len(trace),
                trace=[[int(k), round(x, 1), round(y, 1)] for k, x, y in trace])

    line = [[t[1], t[2]] for t in (trace[0], trace[-1])] if trace else ev.get("track_px", [])
    try:
        write_preview(maxproj, ev_dir / "spectro.jpg", lines=[line] if line else ())
    except Exception as exc:  # Pillow is optional on a bare station
        logger.warning("spectro preview skipped: %s", exc)

    if not found:
        return _done(dict(base, detected=False,
                          note="zero order not found in the recording"))

    spec = extract_video_spectrum(frames, bg, trace, model)
    base.update(detected=True, spectrum=False, coverage=round(spec["coverage"], 2))
    if spec["coverage"] < SPECTRUM_MIN_COVERAGE:
        return _done(dict(base, note="first-order spectrum falls outside the frame "
                                     "(%.0f%% of 380-780 nm on the sensor)"
                                     % (spec["coverage"] * 100)))
    prof = spec["total"].sum(axis=1)
    ref = float(np.median(list(spec["controls"].values())))
    contrast = float(prof.sum() / ref) if ref > 0 else float("inf")
    base.update(contrast=round(contrast, 2) if np.isfinite(contrast) else None)
    if contrast < SPECTRUM_MIN_CONTRAST or prof.max() <= 0:
        return _done(dict(base, note="dispersed flux consistent with background "
                                     "(contrast %.2f < %.2f)"
                                     % (contrast, SPECTRUM_MIN_CONTRAST)))
    norm = prof / prof.max()
    rgb = spec["total"] / prof.max()
    np.savetxt(ev_dir / "spectrum.csv",
               np.column_stack([spec["lam"], norm, rgb]), delimiter=",",
               header="wavelength_nm,relative_intensity,r,g,b", comments="")
    try:
        _write_video_spectrum_plot(ev_dir / "spectrum.png", ev["event_id"],
                                   maxproj, trace, spec, norm)
    except Exception as exc:  # matplotlib is optional on stations
        logger.warning("plot skipped: %s", exc)
    logger.info("%s: video-rate spectrum from %d frames", ev["event_id"], len(trace))
    return _done(dict(base, spectrum=True, csv="spectrum.csv"))


def reduce_event(ev_dir: Path, model: SpectroModel) -> dict | None:
    ev = json.loads((ev_dir / "event.json").read_text())
    # A re-reduction replaces the verdict; a spectrum from the previous run
    # must not outlive it and get pushed next to a "none".
    for name in ("spectrum.csv", "spectrum.png"):
        (ev_dir / name).unlink(missing_ok=True)
    if ev.get("window") and (ev.get("fps") or 0) >= VIDEO_RATE_MIN_FPS:
        return reduce_video_event(ev_dir, ev, model)
    frames_dir = ev_dir / "frames"
    frames_dir.mkdir(exist_ok=True)
    frames = []
    for chunk in ev["chunks"]:
        if (ev_dir / chunk).exists():
            frames += extract_frames(ev_dir / chunk, frames_dir)
    if not frames:
        logger.warning("%s: no frames", ev_dir.name)
        return None

    # Re-project RMS events from their sky track: event.json holds the pixel
    # track of whichever model cut it, and a model fix has to reach
    # re-reductions (every event cut before 2026-09-25 carries a flipped one).
    track_px = ev["track_px"]
    if ev.get("track_azalt"):
        track_px = track_in_fov(model, ev) or track_px
    hit = find_transient_frame(frames, track_px)
    if hit is None:
        result = {"event_id": ev["event_id"], "detected": False,
                  "note": "no frames to search"}
        (ev_dir / "reduction.json").write_text(json.dumps(result, indent=1))
        return result
    k, diff, score, shift = hit

    # Once found, the zero order sits `shift` px off the prediction; the
    # preview and the spectrum walk both follow the found line.
    predicted = np.array(track_px, dtype=float)
    _, normal = _track_samples(predicted)
    found = score >= TRANSIENT_MIN_SCORE
    track = predicted + normal * shift if found else predicted
    # Every event gets its spectro-camera preview, detection or not: on the
    # dashboard an event with no picture is indistinguishable from an event
    # that was never processed.
    try:
        write_preview(diff, ev_dir / "spectro.jpg", lines=[track])
    except Exception as exc:  # Pillow is optional on a bare station
        logger.warning("spectro preview skipped: %s", exc)

    if not found:
        result = {"event_id": ev["event_id"], "detected": False,
                  "score": round(score, 2), "frame": frames[k].name,
                  "note": "no on-track transient above threshold"}
        (ev_dir / "reduction.json").write_text(json.dumps(result, indent=1))
        return result

    # spectrum extraction: band around the track, profile along dispersion axis
    rgb = _load_rgb(frames[k])
    med_rgb = np.median([_load_rgb(f) for f in frames[max(0, k - 3):k]], axis=0) \
        if k >= 1 else np.zeros_like(rgb)
    signal = np.clip(rgb - med_rgb, 0, None)

    dx, dy = model.dispersion_axis
    # sample: for each position along the zero-order track, walk the dispersion
    # axis and accumulate; wavelengths from configured dispersion
    n_lam = 360
    lam = np.linspace(380.0, 780.0, n_lam)
    off = model.zero_order_to_400nm_px + (lam - 400.0) * model.dispersion_px_per_nm

    walk = [track[0] + f * (track[-1] - track[0]) for f in np.linspace(0, 1, 24)]
    xs_all = np.concatenate([b[0] + off * dx for b in walk])
    ys_all = np.concatenate([b[1] + off * dy for b in walk])
    coverage = float(np.mean((xs_all >= 0) & (xs_all < FRAME_W)
                             & (ys_all >= 0) & (ys_all < FRAME_H)))
    base = {"event_id": ev["event_id"], "detected": True, "spectrum": False,
            "score": round(score, 2), "frame": frames[k].name,
            "shift_px": round(shift, 1), "coverage": round(coverage, 2)}
    if coverage < SPECTRUM_MIN_COVERAGE:
        result = dict(base, note="first-order spectrum falls outside the frame "
                                 "(%.0f%% of 380-780 nm on the sensor)" % (coverage * 100))
        (ev_dir / "reduction.json").write_text(json.dumps(result, indent=1))
        logger.info("%s: transient, first order off the sensor", ev["event_id"])
        return result

    def _integrate(shift: float) -> np.ndarray:
        """Walk the dispersion axis from a track displaced perpendicular to it.

        ``shift`` of 0 is the meteor's own track; a non-zero shift samples
        blank sky the same way, which is what makes the result falsifiable.
        """
        px, py = -dy, dx          # unit normal to the dispersion axis
        out = np.zeros(n_lam)
        for f in np.linspace(0, 1, 24):
            base = track[0] + f * (track[-1] - track[0])
            base = base + np.array([px, py]) * shift
            xs = (base[0] + off * dx).astype(int)
            ys = (base[1] + off * dy).astype(int)
            ok = (xs >= 0) & (xs < FRAME_W) & (ys >= 0) & (ys < FRAME_H)
            out[ok] += signal[ys[ok], xs[ok]].sum(axis=-1)
        return out

    prof = _integrate(0.0)
    if prof.max() <= 0:
        result = dict(base, note="transient found, no spectrum signal")
        (ev_dir / "reduction.json").write_text(json.dumps(result, indent=1))
        return result

    # A bright zero order proves a transient, NOT that its dispersed light was
    # recorded. On a cloudy night the walk crosses moonlit cloud and integrates
    # a smooth continuum that looks like a spectrum and is not one: on the
    # 2026-09-21 21:12:36 event blank-sky controls returned up to 1.91x the
    # meteor track's own flux, and every earlier "spectrum" from 2026-09-17 was
    # a cloud edge. So measure the same walk off-target and require the real
    # track to actually stand out.
    controls = [_integrate(s) for s in SPECTRUM_CONTROL_SHIFTS_PX]
    ref = float(np.median([c.sum() for c in controls])) if controls else 0.0
    contrast = float(prof.sum() / ref) if ref > 0 else float("inf")
    if contrast < SPECTRUM_MIN_CONTRAST:
        result = dict(base, contrast=round(contrast, 2),
                      note="dispersed flux consistent with background "
                           "(contrast %.2f < %.2f)" % (contrast, SPECTRUM_MIN_CONTRAST))
        (ev_dir / "reduction.json").write_text(json.dumps(result, indent=1))
        logger.info("%s: no spectrum -- contrast %.2f vs background",
                    ev["event_id"], contrast)
        return result

    prof /= prof.max()

    np.savetxt(ev_dir / "spectrum.csv",
               np.column_stack([lam, prof]), delimiter=",",
               header="wavelength_nm,relative_intensity", comments="")
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(9, 8))
        ax1.imshow(np.clip(diff * 4 + 20, 0, 255), cmap="gray")
        ax1.plot(track[:, 0], track[:, 1], "r-", lw=0.8)
        ax1.set_title(f"{ev['event_id']} frame {k}")
        ax2.plot(lam, prof, lw=1.0)
        for nm, name in ((NA_NM, "Na"), (MG_NM, "Mg")):
            ax2.axvline(nm, color="orange", lw=0.6)
            ax2.text(nm, 1.02, name, ha="center", fontsize=8)
        ax2.set_xlabel("wavelength (nm), configured dispersion")
        ax2.set_ylabel("relative intensity")
        fig.savefig(ev_dir / "spectrum.png", dpi=130, bbox_inches="tight")
        plt.close(fig)
    except Exception as exc:  # matplotlib is optional on stations
        logger.warning("plot skipped: %s", exc)

    result = dict(base, spectrum=True, contrast=round(contrast, 2), csv="spectrum.csv")
    (ev_dir / "reduction.json").write_text(json.dumps(result, indent=1))
    logger.info("%s: spectrum extracted (frame %d)", ev["event_id"], k)
    return result


# ── CLI ────────────────────────────────────────────────────────────────────

PUSH_ARTIFACTS = ("event.json", "reduction.json", "spectrum.csv",
                  "spectrum.png", "wide.jpg", "spectro.jpg")


def build_night_index(night_dir: Path, spectro_code: str, night: str) -> dict:
    """Summarise a night's events into one JSON the dashboard reads per night.

    The alternative is the dashboard opening two JSONs per event to learn what
    each one is: 76 storage-box reads for a 38-event night, over SSHFS, to
    render one page. Writing the summary here costs nothing -- the files are
    already open -- and the tab then needs a single read.
    """
    events = []
    counts = {"events": 0, "detected": 0, "spectra": 0,
              "rms": 0, "widefield": 0}
    for ev_dir in sorted(p for p in night_dir.glob("*") if p.is_dir()):
        try:
            ev = json.loads((ev_dir / "event.json").read_text())
        except (OSError, ValueError):
            continue
        red = {}
        if (ev_dir / "reduction.json").is_file():
            try:
                red = json.loads((ev_dir / "reduction.json").read_text())
            except (OSError, ValueError):
                red = {}
        wide = ev.get("trigger") == "widefield_streak"
        if red.get("spectrum"):
            state = "spectrum"
        elif red.get("detected"):
            state = "transient"
        else:
            state = "none"
        parts = ev_dir.name.split("_")
        hms = parts[-1] if len(parts) >= 3 and len(parts[-1]) == 6 else ""
        events.append({
            "id": ev_dir.name,
            "time": f"{hms[:2]}:{hms[2:4]}:{hms[4:6]}" if hms else "",
            "date": parts[-2] if len(parts) >= 3 else night,
            "trigger": "widefield" if wide else "rms",
            "source": "GH662W" if wide else parts[0],
            "state": state,
            "score": red.get("score"),
            "note": red.get("note", ""),
            "files": sorted(f.name for f in ev_dir.iterdir()
                            if f.name in PUSH_ARTIFACTS),
        })
        counts["events"] += 1
        counts["widefield" if wide else "rms"] += 1
        if state in ("transient", "spectrum"):
            counts["detected"] += 1
        if state == "spectrum":
            counts["spectra"] += 1
    index = {
        "night": night,
        "spectro_code": spectro_code,
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "counts": counts,
        "events": events,
    }
    (night_dir / "index.json").write_text(json.dumps(index, indent=1))
    return index


def push_events(config: dict, spectro_code: str, night: str, home: Path) -> int:
    """Upload each event's small artifacts to the VPS blob ingest.

    Only metadata + reduction products go up (event.json, reduction.json,
    spectrum.csv, spectrum.png) so the dashboard's Final Data Products tab can
    render them — never the rescued chunks (tens of MB each; they stay under
    local retention like every other chunk). Blob path:
    ``<code>/<night>/spectro/<event_id>__<name>``. Idempotent: the ingest
    endpoint skips same-size re-POSTs. Returns the number of uploads.
    """
    try:
        from ingest_blob import post_blob
        from rovimen_pusher import (
            resolve_base_url,
            resolve_host_key,
            resolve_ingest_key,
        )
    except Exception:
        logger.info("ingest helpers unavailable; skipping event upload")
        return 0
    base = resolve_base_url(config)
    host_key = resolve_host_key(config)
    key = resolve_ingest_key()
    if not (base and host_key and key):
        logger.info("ingest identity incomplete; skipping event upload")
        return 0
    n = 0
    night_dir = home / "spectro_events" / night
    for ev_dir in sorted(night_dir.glob("*")):
        if not ev_dir.is_dir():
            continue
        for name in PUSH_ARTIFACTS:
            f = ev_dir / name
            if not f.is_file():
                continue
            url = (f"{base.rstrip('/')}/api/ingest/v1/{host_key}/blob/"
                   f"{spectro_code}/{night}/spectro/{ev_dir.name}__{name}")
            if post_blob(url, key, f.read_bytes()):
                n += 1
    # Last, so the index never advertises an artifact that has not landed yet.
    if night_dir.is_dir():
        build_night_index(night_dir, spectro_code, night)
        idx = night_dir / "index.json"
        url = (f"{base.rstrip('/')}/api/ingest/v1/{host_key}/blob/"
               f"{spectro_code}/{night}/spectro/index.json")
        if post_blob(url, key, idx.read_bytes()):
            n += 1
    if n:
        logger.info("uploaded %d spectro artifact(s) for %s/%s",
                    n, spectro_code, night)
    return n


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser(description="GH662S meteor spectroscopy pipeline")
    ap.add_argument("command", choices=["cut", "wide", "reduce", "night"],
                    help="cut (RMS trigger), wide (widefield streak trigger), "
                         "reduce saved events, or night (all three)")
    ap.add_argument("--night", help="night dir name YYYYMMDD (default: yesterday)")
    ap.add_argument("--code", default="GH662S")
    ap.add_argument("--wide-code", default="GH662W")
    ap.add_argument("--config", default=None)
    ap.add_argument("--force", action="store_true",
                    help="re-reduce events that already have a reduction.json")
    args = ap.parse_args()

    home = Path.home()
    cfg_path = Path(args.config) if args.config else home / "rovimen_scripts" / "config.json"
    config = json.loads(cfg_path.read_text())
    night = args.night
    if not night:
        import datetime
        night = (datetime.date.today() - datetime.timedelta(days=1)).strftime("%Y%m%d")

    scfg = config.get("stations", {}).get(args.code, {}).get("spectro")
    if not scfg:
        # Fleet-wide unit: stations without a spectro camera exit quietly.
        logger.info("no stations.%s.spectro block; nothing to do", args.code)
        return 0
    model = SpectroModel.from_config(scfg)

    if args.command in ("cut", "night"):
        run_cut(config, args.code, night, home)
    if args.command == "wide" or (args.command == "night"
                                  and scfg.get("widefield_trigger", False)):
        wcfg = config["stations"].get(args.wide_code, {}).get("spectro")
        if wcfg:
            run_wide_trigger(config, args.wide_code, args.code, night, home)
        else:
            logger.info("no pointing model for %s; wide trigger skipped", args.wide_code)
    if args.command in ("reduce", "night"):
        for ev_dir in sorted((home / "spectro_events" / night).glob("*")):
            if ev_dir.is_dir() and (args.force
                                    or not (ev_dir / "reduction.json").exists()):
                try:
                    reduce_event(ev_dir, model)
                except Exception:
                    logger.exception("reduction failed: %s", ev_dir.name)
        push_events(config, args.code, night, home)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
