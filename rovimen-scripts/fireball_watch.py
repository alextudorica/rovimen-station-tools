#!/usr/bin/env python3
"""fireball_watch.py - live fireball evidence from RMS's own capture files.

RMS writes two small files while it captures, for every 256-frame block:

- ``FS_<cam>_<date>_<time>_<us>_fieldsum.bin``: the summed intensity of every
  field (or frame), written 1-4 s after the block ends. A fireball lights the
  whole sky, so the series jumps for a second or two.
- ``FR_<cam>_<date>_<time>_<ms>_<frame>.bin``: crops around anything bright
  RMS's live extractor followed through the block.

This watcher follows the newest CapturedFiles session of every running
RMS.StartCapture (inotify, else a 2 s poll), runs a causal fieldsum spike
detector per camera and reduces each FR line to a head-pixel track and its
brightness. What qualifies goes to the VPS as small JSON events through the
station's existing outbound ingest path (``/api/ingest/v1/<host>/fireball``,
same key as rovimen_pusher). The VPS correlates the fleet
(dashboard/fireball_alert.py). The rule and its thresholds come from the
2026-10-17 replay of 29 nights on 51 cameras (5 of 6 known fireballs, about
0.03 alerts a night).

Fieldsum spike, per camera (``FS_*`` thresholds below):
  r = value / median(previous 4 s, excluding the last 0.4 s) - 1;
  a spike is a run of r >= 1 % (two quiet samples end it);
  it qualifies with peak >= 4 %, peak >= 10 sigma (MAD of r over the last
  minute), rise >= 8 frames, FWHM <= 45 frames; a camera that saw 2 or more
  lightning-like flashes (>= 5 %, rise <= 3 frames, not a step) in the last
  20 min counts only rises >= 20 frames with FWHM >= 8.
  The step check (level 6-10 s after the peak below half the peak) needs data
  from the next block, so the spike is sent first without it and the level
  follows as an update.

Safety: read-only on RMS's files, never writes next to them, never touches RMS.
Stdlib only, Python 3.8 safe (gmnro05 (Bistrița) and gmnro08 (Brașov) run
3.8). Any exception is logged and the loop sleeps; the process restarts itself
(exit 0, systemd Restart=always) when its code changes on disk after a bundle
update. Opt out with ``services.fireball_watch.enabled: false``.
"""
from __future__ import annotations

import bisect
import ctypes
import ctypes.util
import json
import logging
import os
import queue
import re
import select
import struct
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, Iterable, List, Optional, Set, Tuple

logger = logging.getLogger("fireball_watch")

VERSION = "1.0.0"
SCRIPTS_DIR = Path.home() / "rovimen_scripts"
CONFIG_PATH = SCRIPTS_DIR / "config.json"
VAAPI_STATUS = SCRIPTS_DIR / "vaapi_status.json"

# Fieldsum detector (replay: fs_events.py + fs_coincide.py, 2026-10-17).
FS_BASE_S = 4.0
FS_GUARD_S = 0.4
FS_MIN_REL = 0.01
FS_PEAK = 0.04
FS_SNR = 10.0
FS_RISE = 8
FS_FWHM = 45
FS_AFTER_FRAC = 0.5
FS_AFTER_FROM, FS_AFTER_TO = 150, 250       # samples after the peak (6-10 s at 25 fps)
FS_SIGMA_SAMPLES = 1500                     # one minute
FS_STORM_LOOKBACK_S = 1200.0
FS_STORM_N = 2
FS_FLASH_PEAK = 0.05
FS_FLASH_RISE = 3
FS_STORM_RISE = 20
FS_STORM_FWHM = 8
FS_MAX_GROUP = 1500                         # a minute above 1 % is not a fireball
FS_KEEP = 2400                              # samples kept per camera

FR_MIN_FRAMES = 2
FR_TRACK_POINTS = 60

POLL_S = 2.0
DISCOVER_S = 60.0
RESCAN_S = 30.0
STATUS_S = 300.0
PLATEPAR_CHECK_S = 600.0
SEND_MAX_AGE_S = 180.0
QUEUE_MAX = 500
SEED_BLOCKS = 8

_TS_RE = re.compile(r"_(\d{8})_(\d{6})_(\d{3,6})")
_SESSION_RE = re.compile(r"_(\d{8})_(\d{6})_\d+$")
_FS_RE = re.compile(r"^FS_.*_fieldsum\.bin$")
_FR_RE = re.compile(r"^FR_.*\.bin$")

PLATEPAR_KEYS = (
    "F_scale", "JD", "Ho", "RA_d", "dec_d", "pos_angle_ref", "X_res", "Y_res",
    "az_centre", "alt_centre", "rotation_from_horiz", "fov_h", "fov_v",
    "lat", "lon", "elev", "x_poly_fwd", "y_poly_fwd", "x_poly_rev", "y_poly_rev",
    "x_poly", "y_poly", "distortion_type", "equal_aspect", "asymmetry_corr",
    "force_distortion_centre", "refraction", "station_code", "version", "UT_corr",
)


def block_time(name: str) -> Optional[float]:
    """Unix time of the block start encoded in an RMS file or directory name."""
    m = _TS_RE.search(name)
    if not m:
        return None
    d, t, frac = m.groups()
    try:
        dt = datetime.strptime(d + t, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return dt.timestamp() + int(frac) / (10 ** len(frac))


# ── fieldsums ─────────────────────────────────────────────────────────────


def parse_fs(data: bytes) -> Optional[List[int]]:
    """Values of a complete FS file (uint16 count, then uint32 per field), or
    None while it is still being written."""
    if len(data) < 2:
        return None
    n = struct.unpack_from("<H", data, 0)[0]
    if n < 16 or len(data) < 2 + 4 * n:
        return None
    return list(struct.unpack_from("<%dI" % n, data, 2))


def fs_frames(values: List[int]) -> List[float]:
    """Per-frame sums: an FS file of 512+ entries holds two fields per frame."""
    if len(values) >= 512:
        m = len(values) // 2 * 2
        return [(values[k] + values[k + 1]) / 2.0 for k in range(0, m, 2)]
    return [float(x) for x in values]


def _median(sorted_vals: List[float]) -> float:
    n = len(sorted_vals)
    mid = n // 2
    return sorted_vals[mid] if n % 2 else (sorted_vals[mid - 1] + sorted_vals[mid]) / 2.0


class _Group:
    __slots__ = ("start", "last_above", "peak_j", "closed", "sent", "last_sig", "done")

    def __init__(self, i: int) -> None:
        self.start = i
        self.last_above = i
        self.peak_j = i
        self.closed = False
        self.sent: Optional[Tuple] = None   # signature of the last emitted state
        self.last_sig: Optional[Dict[str, Any]] = None
        self.done = False


class FieldsumDetector:
    """Causal per-camera spike detector (the replay's ``fs_events.spikes`` run
    one block at a time). ``feed`` returns spike events to send: new
    qualifying spikes, and updates (peak, FWHM, step level) of spikes already
    sent."""

    def __init__(self, cam: str, fps: float = 25.0) -> None:
        self.cam = cam
        self.fps = fps or 25.0
        self.w = int(FS_BASE_S * self.fps)
        self.g = int(FS_GUARD_S * self.fps)
        self.off = 0                       # absolute index of element 0
        self.t: List[float] = []
        self.v: List[float] = []
        self.base: List[Optional[float]] = []
        self.r: List[Optional[float]] = []
        self.sig: List[Optional[float]] = []
        self.win: List[float] = []         # sorted baseline window
        self.cur_sig: Optional[float] = None
        self.group: Optional[_Group] = None
        self.watch: List[_Group] = []      # closed groups still waiting for data
        self.flashes: Deque[float] = deque(maxlen=400)
        self.last_t0: Optional[float] = None

    # absolute-index accessors
    @property
    def n(self) -> int:
        return self.off + len(self.v)

    def _r(self, i: int) -> Optional[float]:
        k = i - self.off
        return self.r[k] if 0 <= k < len(self.r) else None

    def _v(self, i: int) -> float:
        return self.v[i - self.off]

    def _sigma(self) -> Optional[float]:
        vals = [x for x in self.r[-FS_SIGMA_SAMPLES:] if x is not None]
        if len(vals) < 250:
            return None
        vals.sort()
        med = _median(vals)
        dev = sorted(abs(x - med) for x in vals)
        return 1.4826 * _median(dev)

    def _append(self, t: float, v: float) -> None:
        i = self.n
        w, g = self.w, self.g
        if i - g - 1 >= 0 and i - g - 1 >= self.off:
            bisect.insort(self.win, self._v(i - g - 1))
        old = i - g - w - 1
        if old >= 0 and old >= self.off:
            x = self._v(old)
            k = bisect.bisect_left(self.win, x)
            if k < len(self.win) and self.win[k] == x:
                self.win.pop(k)
        base = self.win[w // 2] if len(self.win) >= w else None
        r = (v / max(base, 1.0) - 1.0) if base is not None else None
        self.t.append(t)
        self.v.append(v)
        self.base.append(base)
        self.r.append(r)
        self.sig.append(self.cur_sig)
        self._track(i, r)

    def _track(self, i: int, r: Optional[float]) -> None:
        grp = self.group
        if r is not None and r >= FS_MIN_REL:
            if grp is not None and i - grp.last_above <= 2 and i - grp.start < FS_MAX_GROUP:
                grp.last_above = i
                if r > (self._r(grp.peak_j) or 0.0):
                    grp.peak_j = i
            else:
                if grp is not None:
                    self._close(grp)
                self.group = _Group(i)
        elif grp is not None and i - grp.last_above >= 2:
            self._close(grp)
            self.group = None

    def _close(self, grp: _Group) -> None:
        grp.closed = True
        # Only a group that can qualify, count as a lightning flash, or was
        # already sent needs more data; the rest are noise.
        if grp.sent is not None or (self._r(grp.peak_j) or 0.0) >= min(FS_PEAK, FS_FLASH_PEAK):
            self.watch.append(grp)

    def _trim(self) -> None:
        excess = len(self.v) - FS_KEEP
        if excess <= 0:
            return
        # Keep everything an open group or a pending step check still needs.
        keep_from = self.n - FS_KEEP
        for grp in ([self.group] if self.group else []) + self.watch:
            keep_from = min(keep_from, grp.start - self.g - 2)
        cut = max(0, keep_from - self.off)
        if cut <= 0:
            return
        for lst in (self.t, self.v, self.base, self.r, self.sig):
            del lst[:cut]
        self.off += cut

    def features(self, grp: _Group) -> Optional[Dict[str, Any]]:
        """The replay's spike features for a group, or None while its peak or
        half-maximum end is not yet in the data."""
        j = grp.peak_j
        pk = self._r(j)
        if pk is None:
            return None
        last = self.n - 1
        k0 = j
        while k0 - 1 >= self.off and (self._r(k0 - 1) or -1.0) >= pk / 2:
            k0 -= 1
        k1 = j
        while k1 + 1 <= last and (self._r(k1 + 1) or -1.0) >= pk / 2:
            k1 += 1
        if k1 == last and not grp.closed:
            return None                      # still at or above half maximum
        sig = self.sig[j - self.off]
        base = self.base[j - self.off] or 1.0
        after = None
        if j + FS_AFTER_TO <= last:
            seg = sorted(self.v[j + FS_AFTER_FROM - self.off:j + FS_AFTER_TO - self.off])
            after = _median(seg) / max(base, 1.0) - 1.0
        t_peak = self.t[j - self.off]
        return {
            "t_peak": round(t_peak, 3),
            "t_start": round(self.t[grp.start - self.off], 3),
            "peak": round(pk, 4),
            "snr": round(pk / sig, 1) if sig else None,
            "fwhm": int(k1 - k0 + 1),
            "rise": int(j - grp.start),
            "after": round(after, 4) if after is not None else None,
            "final": bool(grp.closed),
        }

    def storm_at(self, t: float) -> bool:
        n = sum(1 for x in self.flashes if t - FS_STORM_LOOKBACK_S <= x < t - 0.01)
        return n >= FS_STORM_N

    @staticmethod
    def qualifies(f: Dict[str, Any], storm: bool) -> bool:
        if f["peak"] < FS_PEAK or not f["snr"] or f["snr"] < FS_SNR:
            return False
        if f["fwhm"] > FS_FWHM or f["rise"] < FS_RISE:
            return False
        if storm and (f["rise"] < FS_STORM_RISE or f["fwhm"] < FS_STORM_FWHM):
            return False
        return True

    @staticmethod
    def is_step(f: Dict[str, Any]) -> bool:
        return f["after"] is not None and f["after"] > FS_AFTER_FRAC * f["peak"]

    def feed(self, t0: float, values: List[int], emit: bool = True) -> List[Dict[str, Any]]:
        """Process one FS block; return the spike events to send."""
        if self.last_t0 is not None and t0 <= self.last_t0:
            return []
        self.last_t0 = t0
        frames = fs_frames(values)
        self.cur_sig = self._sigma()
        for k, v in enumerate(frames):
            self._append(t0 + k / self.fps, v)
        out: List[Dict[str, Any]] = []
        groups = list(self.watch) + ([self.group] if self.group else [])
        for grp in groups:
            f = self.features(grp)
            if f is None:
                continue
            # Lightning bookkeeping for the storm flag, once the step level is known.
            if grp.closed and f["after"] is not None and not grp.done:
                if f["peak"] >= FS_FLASH_PEAK and f["rise"] <= FS_FLASH_RISE and not self.is_step(f):
                    self.flashes.append(f["t_peak"])
            storm = self.storm_at(f["t_peak"])
            ok = self.qualifies(f, storm)
            sigkey = (f["peak"], f["fwhm"], f["rise"], f["after"], f["final"], ok)
            if emit and (ok or grp.sent is not None) and sigkey != grp.sent:
                ev = dict(f)
                ev.update({"kind": "fs", "cam": self.cam, "id": "%s_%.2f" % (self.cam, f["t_start"]),
                           "storm": storm, "qualifies": ok, "step": self.is_step(f)})
                out.append(ev)
                grp.sent = sigkey
            if grp.closed and f["after"] is not None:
                grp.done = True
            elif grp.closed and self.n - 1 - grp.peak_j > FS_AFTER_TO + 300:
                grp.done = True              # the step level will never come
        self.watch = [g for g in self.watch if not g.done]
        self._trim()
        return out


# ── FR bins ───────────────────────────────────────────────────────────────


def parse_fr(data: bytes) -> Optional[List[Dict[str, Any]]]:
    """Per-frame features of every line in an FR bin (receive.read_fr of the
    replay), or None when the file is incomplete or malformed."""
    try:
        off = 0
        nlines = struct.unpack_from("<I", data, off)[0]
        off += 4
        if nlines > 1000:
            return None
        lines: List[Dict[str, Any]] = []
        for _ in range(nlines):
            nfr = struct.unpack_from("<I", data, off)[0]
            off += 4
            if nfr > 100000:
                return None
            rec: Dict[str, List[Any]] = {k: [] for k in ("t", "peak", "n250", "flux", "hx", "hy")}
            for _ in range(nfr):
                yc, xc, t, size = struct.unpack_from("<4I", data, off)
                off += 16
                npx = size * size
                if size > 2048 or off + npx > len(data):
                    return None
                crop = data[off:off + npx]
                off += npx
                peak, n250, flux, hx, hy = _crop_features(crop, size, xc, yc)
                rec["t"].append(int(t))
                rec["peak"].append(peak)
                rec["n250"].append(n250)
                rec["flux"].append(flux)
                rec["hx"].append(hx)
                rec["hy"].append(hy)
            lines.append(rec)
        if off != len(data):
            return None
        return lines
    except struct.error:
        return None


def _crop_features(crop: bytes, size: int, xc: int, yc: int) -> Tuple[int, int, int, float, float]:
    """peak, saturated-pixel count, excess flux and the head centroid of one crop."""
    if not crop:
        return 0, 0, 0, float(xc), float(yc)
    s = sorted(crop)
    n = len(s)
    bg = float(s[n // 2]) if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0
    peak = s[-1]
    n250 = n - bisect.bisect_left(s, 250)
    thr = bg + 15
    flux = 0.0
    hi = max(bg + 15, 0.5 * (peak + bg))
    sw = swx = swy = 0.0
    if peak > thr:
        for idx, val in enumerate(crop):
            if val > thr:
                ex = val - bg
                flux += ex
                if val >= hi:
                    y, x = divmod(idx, size)
                    sw += ex
                    swx += x * ex
                    swy += y * ex
    if sw > 0:
        hx = swx / sw - size / 2 + xc
        hy = swy / sw - size / 2 + yc
    else:
        hx, hy = float(xc), float(yc)
    return int(peak), int(n250), int(round(flux)), round(hx, 1), round(hy, 1)


def _decimate(idx: List[int], n: int) -> List[int]:
    if len(idx) <= n:
        return idx
    step = (len(idx) - 1) / float(n - 1)
    return sorted({idx[int(round(k * step))] for k in range(n)})


def fr_events(cam: str, name: str, lines: List[Dict[str, Any]], fps: float = 25.0) -> List[Dict[str, Any]]:
    """One small event per FR line: timing, brightness and a decimated head track."""
    t0 = block_time(name)
    if t0 is None:
        return []
    out = []
    for ln, rec in enumerate(lines):
        t = rec["t"]
        if len(t) < FR_MIN_FRAMES:
            continue
        keep = _decimate(list(range(len(t))), FR_TRACK_POINTS)
        out.append({
            "kind": "fr", "cam": cam, "id": "%s_%s_%d" % (cam, name[:-4], ln), "file": name, "line": ln,
            "t0": round(t0, 3), "fps": fps,
            "tstart": round(t0 + min(t) / fps, 3), "tend": round(t0 + max(t) / fps, 3),
            "nfr": len(t), "dur": round((max(t) - min(t) + 1) / fps, 2),
            "peak": max(rec["peak"]), "n250_sum": int(sum(rec["n250"])),
            "nsat_frames": int(sum(1 for x in rec["n250"] if x > 0)), "flux_sum": int(sum(rec["flux"])),
            "t": [t[k] for k in keep], "hx": [rec["hx"][k] for k in keep], "hy": [rec["hy"][k] for k in keep],
        })
    return out


# ── platepar ──────────────────────────────────────────────────────────────


def platepar_event(cam: str, path: Path) -> Optional[Dict[str, Any]]:
    try:
        pp = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(pp, dict):
        return None
    slim = {k: pp[k] for k in PLATEPAR_KEYS if k in pp}
    return {"kind": "platepar", "cam": cam, "id": "%s_pp_%d" % (cam, int(path.stat().st_mtime)),
            "mtime": int(path.stat().st_mtime), "source": str(path), "pp": slim}


# ── RMS discovery ─────────────────────────────────────────────────────────


class Cam:
    def __init__(self, code: str, fps: float, captured: List[Path]) -> None:
        self.code = code
        self.fps = fps
        self.captured = captured
        self.session: Optional[Path] = None
        self.seen: Set[str] = set()
        self.pending_fr: Dict[str, float] = {}
        self.det = FieldsumDetector(code, fps)
        self.last_fs: Optional[float] = None
        self.pp_mtime: Optional[int] = None
        self.pp_checked = 0.0


def discover() -> Dict[str, Tuple[float, List[Path]]]:
    """code -> (fps, CapturedFiles dirs) for every running RMS.StartCapture."""
    import camera_fps_guard as fps_guard
    out: Dict[str, Tuple[float, List[Path]]] = {}
    for p in fps_guard.rms_processes():
        rms = fps_guard.parse_rms_config(p.config)
        if rms is None:
            continue
        dirs = []
        for ld in rms.log_dirs:
            cap = ld.parent / "CapturedFiles"
            if cap.is_dir() and cap not in dirs:
                dirs.append(cap)
        if dirs:
            out[rms.code] = (rms.fps, dirs)
    return out


def newest_session(cam: Cam) -> Optional[Path]:
    best: Optional[Path] = None
    now = time.time()
    for cap in cam.captured:
        try:
            for d in os.scandir(str(cap)):
                if not d.is_dir() or not d.name.startswith(cam.code + "_") or not _SESSION_RE.search(d.name):
                    continue
                t = block_time(d.name)
                if t is None or t > now + 120:
                    continue
                if best is None or d.name > best.name:
                    best = Path(d.path)
        except OSError:
            continue
    return best


# ── inotify (ctypes), with a polling fallback ─────────────────────────────

IN_CLOSE_WRITE = 0x00000008
IN_MOVED_TO = 0x00000080
IN_NONBLOCK = 0o4000
IN_CLOEXEC = 0o2000000


class Inotify:
    def __init__(self) -> None:
        self.fd = -1
        self.wd: Dict[int, str] = {}
        try:
            libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
            self._libc = libc
            fd = libc.inotify_init1(IN_NONBLOCK | IN_CLOEXEC)
            if fd >= 0:
                self.fd = fd
        except (OSError, AttributeError):
            self.fd = -1

    @property
    def ok(self) -> bool:
        return self.fd >= 0

    def watch(self, path: Path) -> bool:
        if not self.ok:
            return False
        wd = self._libc.inotify_add_watch(self.fd, str(path).encode(), IN_CLOSE_WRITE | IN_MOVED_TO)
        if wd < 0:
            return False
        self.wd[wd] = str(path)
        return True

    def unwatch_all(self) -> None:
        for wd in list(self.wd):
            try:
                self._libc.inotify_rm_watch(self.fd, wd)
            except Exception:
                pass
        self.wd.clear()

    def read(self, timeout: float) -> List[Tuple[str, str]]:
        if not self.ok:
            time.sleep(timeout)
            return []
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return []
        try:
            buf = os.read(self.fd, 65536)
        except BlockingIOError:
            return []
        out = []
        off = 0
        while off + 16 <= len(buf):
            wd, _mask, _cookie, ln = struct.unpack_from("iIII", buf, off)
            name = buf[off + 16:off + 16 + ln].rstrip(b"\0").decode(errors="replace")
            off += 16 + ln
            if wd in self.wd and name:
                out.append((self.wd[wd], name))
        return out


# ── sending ───────────────────────────────────────────────────────────────


class Sender(threading.Thread):
    """Posts event batches to the VPS on its own thread; a batch that cannot be
    delivered within SEND_MAX_AGE_S is dropped (a live alert has no use for it)."""

    def __init__(self) -> None:
        super().__init__(daemon=True, name="fireball-sender")
        self.q: "queue.Queue[Tuple[float, List[Dict[str, Any]]]]" = queue.Queue(maxsize=QUEUE_MAX)
        self.sent = 0
        self.dropped = 0

    def put(self, events: List[Dict[str, Any]]) -> None:
        if not events:
            return
        try:
            self.q.put_nowait((time.time(), events))
        except queue.Full:
            self.dropped += len(events)

    def run(self) -> None:
        import rovimen_pusher as rp
        while True:
            created, events = self.q.get()
            delay = 2.0
            while True:
                try:
                    cfg = rp._load_config()
                    host = rp.resolve_host_key(cfg)
                    key = rp.resolve_ingest_key()
                    urls = [u for u in (rp.resolve_base_url(cfg), rp.resolve_fallback_url(cfg)) if u]
                    if not (host and key and urls):
                        self.dropped += len(events)
                        break
                    body = {"seq": int(time.time() * 1000), "sent_at": rp._now_rfc3339(),
                            "payload": {"events": events, "watcher": VERSION}}
                    res = None
                    for base in urls:
                        res = rp.post_json("%s/api/ingest/v1/%s/fireball" % (base, host), key, body, timeout=15)
                        if res.ok or (res.status is not None and 400 <= res.status < 500 and res.status != 429):
                            break
                    if res is not None and res.ok:
                        self.sent += len(events)
                        break
                    if res is not None and res.status is not None and 400 <= res.status < 500 and res.status != 429:
                        logger.warning("ingest refused %d events: HTTP %s", len(events), res.status)
                        self.dropped += len(events)
                        break
                except Exception:
                    logger.exception("send failed")
                if time.time() - created > SEND_MAX_AGE_S:
                    self.dropped += len(events)
                    break
                time.sleep(delay)
                delay = min(delay * 2, 30.0)


# ── main loop ─────────────────────────────────────────────────────────────


def _load_config() -> Dict[str, Any]:
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


def enabled(cfg: Dict[str, Any]) -> bool:
    svc = (cfg.get("services") or {}).get("fireball_watch") or {}
    return bool(svc.get("enabled", True)) if isinstance(svc, dict) else True


def _code_mtimes() -> Dict[str, float]:
    out = {}
    here = Path(__file__).resolve().parent
    for name in ("fireball_watch.py", "camera_fps_guard.py", "rovimen_pusher.py", "rovimen_rms.py", "rovimen_night.py"):
        try:
            out[name] = (here / name).stat().st_mtime
        except OSError:
            pass
    return out


class Watcher:
    def __init__(self, sender: Sender) -> None:
        self.sender = sender
        self.cams: Dict[str, Cam] = {}
        self.ino = Inotify()
        self.cfg: Dict[str, Any] = {}

    def refresh(self) -> None:
        self.cfg = _load_config()
        found = discover()
        for code in list(self.cams):
            if code not in found:
                del self.cams[code]
        for code, (fps, dirs) in found.items():
            cam = self.cams.get(code)
            if cam is None or cam.captured != dirs or abs(cam.fps - fps) > 0.01:
                cam = self.cams[code] = Cam(code, fps, dirs)
            sess = newest_session(cam)
            if sess is not None and sess != cam.session:
                self._start_session(cam, sess)
        wanted = sorted(str(c.session) for c in self.cams.values() if c.session is not None)
        if wanted != sorted(self.ino.wd.values()):
            self.ino.unwatch_all()
            for path in wanted:
                self.ino.watch(Path(path))
        self._check_platepars()

    def _start_session(self, cam: Cam, sess: Path) -> None:
        """Follow a new session; warm the detector on its latest blocks without sending."""
        logger.info("%s: following %s", cam.code, sess)
        cam.session = sess
        cam.det = FieldsumDetector(cam.code, cam.fps)
        try:
            names = sorted(os.listdir(str(sess)))
        except OSError:
            names = []
        cam.seen = set(names)
        cam.pending_fr.clear()
        for name in [n for n in names if _FS_RE.match(n)][-SEED_BLOCKS:]:
            self._fs(cam, name, emit=False)

    def _fs(self, cam: Cam, name: str, emit: bool = True) -> bool:
        try:
            data = (cam.session / name).read_bytes()
        except OSError:
            return False
        vals = parse_fs(data)
        t0 = block_time(name)
        if vals is None or t0 is None:
            return False
        evs = cam.det.feed(t0, vals, emit=emit)
        cam.last_fs = t0
        if evs:
            for ev in evs:
                ev["block"] = name
            logger.info("%s: %d fieldsum event(s), peak %s", cam.code, len(evs), ", ".join(
                "%+.0f%%%s" % (e["peak"] * 100, "" if e["qualifies"] else " (dropped)") for e in evs))
            self.sender.put(evs)
        return True

    def _fr(self, cam: Cam, name: str) -> bool:
        try:
            data = (cam.session / name).read_bytes()
        except OSError:
            return False
        lines = parse_fr(data)
        if lines is None:
            return False
        evs = fr_events(cam.code, name, lines, cam.fps)
        if evs:
            logger.info("%s: FR %s, %d line(s), max n250 %d", cam.code, name, len(evs),
                        max(e["n250_sum"] for e in evs))
            self.sender.put(evs)
        return True

    def handle(self, cam: Cam, name: str) -> None:
        if name in cam.seen:
            return
        if _FS_RE.match(name):
            if self._fs(cam, name):
                cam.seen.add(name)
        elif _FR_RE.match(name):
            if self._fr(cam, name):
                cam.seen.add(name)
                cam.pending_fr.pop(name, None)
            else:
                first = cam.pending_fr.setdefault(name, time.time())
                if time.time() - first > 60:
                    cam.seen.add(name)       # never parsed: give up
                    cam.pending_fr.pop(name, None)
        else:
            cam.seen.add(name)

    def rescan(self) -> None:
        for cam in self.cams.values():
            if cam.session is None:
                continue
            try:
                names = sorted(n for n in os.listdir(str(cam.session)) if n not in cam.seen)
            except OSError:
                continue
            for name in names:
                if _FS_RE.match(name) or _FR_RE.match(name):
                    self.handle(cam, name)
                else:
                    cam.seen.add(name)

    def on_events(self, items: Iterable[Tuple[str, str]]) -> None:
        by_dir = {str(c.session): c for c in self.cams.values() if c.session is not None}
        for d, name in sorted(items, key=lambda x: x[1]):
            cam = by_dir.get(d)
            if cam is not None:
                self.handle(cam, name)

    def _check_platepars(self) -> None:
        now = time.time()
        try:
            import rovimen_rms
        except Exception:
            return
        for cam in self.cams.values():
            if now - cam.pp_checked < PLATEPAR_CHECK_S:
                continue
            cam.pp_checked = now
            try:
                path = rovimen_rms.find_latest_platepar(cam.code, self.cfg)
            except Exception:
                path = None
            if path is None:
                continue
            try:
                mt = int(path.stat().st_mtime)
            except OSError:
                continue
            if mt != cam.pp_mtime:
                ev = platepar_event(cam.code, path)
                if ev is not None:
                    cam.pp_mtime = mt
                    self.sender.put([ev])

    def status_event(self) -> Dict[str, Any]:
        vaapi = None
        try:
            vaapi = json.loads(VAAPI_STATUS.read_text()).get("verdict")
        except (OSError, ValueError, AttributeError):
            pass
        return {"kind": "status", "id": "status_%d" % int(time.time()), "t": round(time.time(), 1),
                "version": VERSION, "inotify": self.ino.ok, "vaapi": vaapi,
                "sent": self.sender.sent, "dropped": self.sender.dropped,
                "cams": {c.code: {"session": c.session.name if c.session else None,
                                  "last_fs": c.last_fs} for c in self.cams.values()}}


def run() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sender = Sender()
    sender.start()
    watcher = Watcher(sender)
    logger.info("fireball_watch %s started (inotify %s)", VERSION, "on" if watcher.ino.ok else "off, polling")
    code0 = _code_mtimes()
    last_discover = last_rescan = last_status = last_code = 0.0
    while True:
        try:
            now = time.time()
            if now - last_code > 60:
                last_code = now
                if _code_mtimes() != code0:
                    logger.info("code changed on disk; exiting so systemd restarts the new version")
                    return 0
            if now - last_discover > DISCOVER_S:
                last_discover = now
                watcher.refresh()
                if not enabled(watcher.cfg):
                    logger.info("services.fireball_watch.enabled is false; idle")
                    watcher.cams.clear()
                    watcher.ino.unwatch_all()
            if not watcher.cams:
                time.sleep(POLL_S)
                continue
            if watcher.ino.ok and watcher.ino.wd:
                watcher.on_events(watcher.ino.read(POLL_S))
                if now - last_rescan > RESCAN_S:
                    last_rescan = now
                    watcher.rescan()
            else:
                time.sleep(POLL_S)
                watcher.rescan()
            if now - last_status > STATUS_S:
                last_status = now
                sender.put([watcher.status_event()])
        except Exception:
            logger.exception("watch loop error; sleeping 30 s")
            time.sleep(30)


if __name__ == "__main__":
    sys.exit(run())
