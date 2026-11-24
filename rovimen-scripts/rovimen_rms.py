"""Shared RMS readers for station scripts: FTPdetectinfo and radiants
parsing, their join into detection rows, and RMS config/platepar lookup.

The parsers are detection_indexer's (the canonical station copy, mirrored by
dashboard/rms_parse); station_api's older copy used stricter column counts,
so the same meteor's duration changed when a night moved from station_api to
the index.

Stdlib only and Python 3.8 safe (see rovimen_config).
"""

from __future__ import annotations

import functools
import json
import logging
import math
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from rovimen_night import FF_NAME_RE, ftpdetect_files, scan_rms_sessions

logger = logging.getLogger(__name__)

JOIN_TOLERANCE_S = 2.0
PLATEPAR_NAME = 'platepar_cmn2010.cal'

# RMS's rule for the camera it controls over DVRIP (Utils/CameraControl.py,
# cameraControlV2): the first dotted-quad IP anywhere in the device URL.
_DEVICE_IP_RE = re.compile(r'[0-9]+(?:\.[0-9]+){3}')


def camera_ip_from_device(device: str) -> Optional[str]:
    """The camera an RMS ``device:`` URL points RMS's camera control at.

    On a relay station the device is ``rtsp://localhost:8554/<path>?camera=<IP>``
    (rtsp_relay), so this is the camera, not the relay; the older
    ``rtsp://127.0.0.1:8554/camN`` form gives 127.0.0.1, as it does to RMS.
    """
    m = _DEVICE_IP_RE.search(device or '')
    return m.group(0) if m else None

# ---------------------------------------------------------------------------
# FTPdetectinfo / radiants
# ---------------------------------------------------------------------------


def ff_block_start(ff_file: str) -> Optional[datetime]:
    """UTC start of the FF block named *ff_file* (naive), or None."""
    m = FF_NAME_RE.match(ff_file)
    if not m:
        return None
    date_s, time_s, ms_s = m.group(2), m.group(3), m.group(4)
    try:
        return datetime(
            int(date_s[:4]), int(date_s[4:6]), int(date_s[6:8]),
            int(time_s[:2]), int(time_s[2:4]), int(time_s[4:6]),
            int(ms_s) * 1000,
        )
    except ValueError:
        return None


def _opt(s: str) -> Optional[float]:
    s = s.strip()
    return None if s in ('None', '') else float(s)


def radiant_rows(path: Path) -> List[Dict[str, Any]]:
    """Rows of an RMS ``*_radiants.txt`` (``_begin_dt`` is a naive UTC datetime)."""
    results: List[Dict[str, Any]] = []
    try:
        text = Path(path).read_text(errors='ignore')
    except OSError:
        return results
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        parts = [p.strip() for p in line.split(',')]
        if len(parts) < 17:
            continue
        try:
            begin_dt = datetime.strptime(parts[0].strip(), '%Y%m%d %H:%M:%S.%f')
        except ValueError:
            try:
                begin_dt = datetime.strptime(parts[0].strip().split('.')[0], '%Y%m%d %H:%M:%S')
            except ValueError:
                continue
        try:
            shower = parts[3].strip() or 'SPO'
            if shower == '...':
                shower = 'SPO'
            results.append({
                '_begin_dt': begin_dt,
                'time_utc': iso_ms(begin_dt),
                'jd': float(parts[1]),
                'solar_lon': _opt(parts[2]),
                'shower': shower,
                'ra_beg': float(parts[4]),
                'dec_beg': float(parts[5]),
                'ra_end': float(parts[6]),
                'dec_end': float(parts[7]),
                'ra_radiant': _opt(parts[8]),
                'dec_radiant': _opt(parts[9]),
                'mag_apparent': _opt(parts[14]),
                'mag_absolute': _opt(parts[15]),
                'radiant_elev': _opt(parts[16]),
            })
        except (ValueError, IndexError):
            continue
    return results


def ftp_rows(path: Path) -> List[Dict[str, Any]]:
    """Per-meteor rows of an FTPdetectinfo file (first/last frame summary)."""
    results: List[Dict[str, Any]] = []
    try:
        lines = Path(path).read_text(errors='ignore').splitlines()
    except OSError:
        return results
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith('FF_') and line.endswith('.fits'):
            ff_file = line
            i += 1
            if i < len(lines) and 'Recalibrated' in lines[i]:
                i += 1
            if i >= len(lines):
                break
            header = lines[i].strip().split()
            if len(header) < 4:
                i += 1
                continue
            try:
                meteor_no = int(header[1])
                num_segments = int(header[2])
                fps = float(header[3])
            except (ValueError, IndexError):
                i += 1
                continue
            i += 1
            frames: List[Dict[str, float]] = []
            while i < len(lines):
                dl = lines[i].strip()
                if not dl or dl.startswith('-') or dl.startswith('FF_'):
                    break
                fp = dl.split()
                if len(fp) >= 9:
                    try:
                        frames.append({
                            'frame': float(fp[0]),
                            'x': float(fp[1]), 'y': float(fp[2]),
                            'ra': float(fp[3]), 'dec': float(fp[4]),
                            'azim': float(fp[5]), 'elev': float(fp[6]),
                            'mag': float(fp[8]),
                        })
                    except ValueError:
                        break
                else:
                    break
                i += 1
            if frames:
                duration_s = (frames[-1]['frame'] - frames[0]['frame']) / fps if fps > 0 else 0.0
                ang_vel = None
                if duration_s > 0 and len(frames) >= 2:
                    ra0, dec0 = math.radians(frames[0]['ra']), math.radians(frames[0]['dec'])
                    ra1, dec1 = math.radians(frames[-1]['ra']), math.radians(frames[-1]['dec'])
                    dlat, dlon = dec1 - dec0, ra1 - ra0
                    a = (math.sin(dlat / 2) ** 2
                         + math.cos(dec0) * math.cos(dec1) * math.sin(dlon / 2) ** 2)
                    ang_dist = math.degrees(2 * math.asin(min(1.0, math.sqrt(a))))
                    ang_vel = round(ang_dist / duration_s, 2)
                begin_dt = ff_block_start(ff_file)
                if begin_dt is not None and fps > 0:
                    begin_dt = begin_dt + timedelta(seconds=frames[0]['frame'] / fps)
                results.append({
                    '_begin_dt': begin_dt,
                    'ff_file': ff_file,
                    'meteor_no': meteor_no,
                    'duration_s': round(duration_s, 3),
                    'angular_velocity': ang_vel,
                    'num_segments': num_segments,
                    'fps': fps,
                    'peak_mag': min(f['mag'] for f in frames),
                    'ra_beg': frames[0]['ra'],
                    'dec_beg': frames[0]['dec'],
                    'ra_end': frames[-1]['ra'],
                    'dec_end': frames[-1]['dec'],
                    'azim_beg': frames[0]['azim'],
                    'elev_beg': frames[0]['elev'],
                    'azim_end': frames[-1]['azim'],
                    'elev_end': frames[-1]['elev'],
                    'x_beg': frames[0]['x'],
                    'y_beg': frames[0]['y'],
                })
            continue
        i += 1
    return results


def iso_ms(dt: Optional[datetime]) -> Optional[str]:
    """``YYYY-MM-DDTHH:MM:SS.mmm``. Detection times keep their milliseconds:
    cut to whole seconds they put a meteor up to 1 s off in its clip."""
    return dt.isoformat(timespec='milliseconds') if dt is not None else None


def _coalesce(*values: Optional[float]) -> Optional[float]:
    """First non-None value, preserving a literal 0.0."""
    for v in values:
        if v is not None:
            return v
    return None


def merge_row(ftp: Dict[str, Any], rad: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """One detection row from an FTPdetectinfo row and its matched radiant row."""
    # The meteor's first frame (FF block start + frame / fps) is the most
    # precise time there is; the radiant row's time is the fallback.
    bdt = ftp.get('_begin_dt')
    if rad is not None:
        time_utc = iso_ms(bdt) if bdt is not None else rad.get('time_utc')
        mag_apparent = rad.get('mag_apparent')
    else:
        time_utc = iso_ms(bdt) if bdt is not None else None
        mag_apparent = ftp.get('peak_mag')
    rad = rad or {}
    return {
        'ff_file': ftp['ff_file'], 'meteor_no': ftp['meteor_no'],
        'time_utc': time_utc, 'jd': rad.get('jd'), 'solar_lon': rad.get('solar_lon'),
        'shower': rad.get('shower'), 'mag_apparent': mag_apparent,
        'mag_absolute': rad.get('mag_absolute'), 'duration_s': ftp.get('duration_s'),
        'ra_beg': _coalesce(rad.get('ra_beg'), ftp.get('ra_beg')),
        'dec_beg': _coalesce(rad.get('dec_beg'), ftp.get('dec_beg')),
        'ra_end': _coalesce(rad.get('ra_end'), ftp.get('ra_end')),
        'dec_end': _coalesce(rad.get('dec_end'), ftp.get('dec_end')),
        'ra_radiant': rad.get('ra_radiant'), 'dec_radiant': rad.get('dec_radiant'),
        'radiant_elev': rad.get('radiant_elev'),
        'angular_velocity': ftp.get('angular_velocity'),
        'num_segments': ftp.get('num_segments'), 'fps': ftp.get('fps'),
        'azim_beg': ftp.get('azim_beg'), 'elev_beg': ftp.get('elev_beg'),
        'azim_end': ftp.get('azim_end'), 'elev_end': ftp.get('elev_end'),
        'x_beg': ftp.get('x_beg'), 'y_beg': ftp.get('y_beg'),
    }


def radiant_only_row(rad: Dict[str, Any]) -> Dict[str, Any]:
    """A detection row for a radiant with no FTPdetectinfo match."""
    bdt = rad['_begin_dt']
    return {
        'ff_file': f"RAD_{bdt.strftime('%Y%m%d_%H%M%S_%f')}", 'meteor_no': 1,
        'time_utc': rad.get('time_utc'), 'jd': rad.get('jd'), 'solar_lon': rad.get('solar_lon'),
        'shower': rad.get('shower'), 'mag_apparent': rad.get('mag_apparent'),
        'mag_absolute': rad.get('mag_absolute'), 'duration_s': None,
        'ra_beg': rad.get('ra_beg'), 'dec_beg': rad.get('dec_beg'),
        'ra_end': rad.get('ra_end'), 'dec_end': rad.get('dec_end'),
        'ra_radiant': rad.get('ra_radiant'), 'dec_radiant': rad.get('dec_radiant'),
        'radiant_elev': rad.get('radiant_elev'),
        'angular_velocity': None, 'num_segments': None, 'fps': None,
        'azim_beg': None, 'elev_beg': None, 'azim_end': None, 'elev_end': None,
        'x_beg': None, 'y_beg': None,
    }


def session_detections(session: Path) -> List[Dict[str, Any]]:
    """Every detection of one RMS session: FTPdetectinfo joined to radiants.

    Rows are matched by begin time within ``JOIN_TOLERANCE_S``; unmatched
    radiants become radiant-only rows.
    """
    ftp_by_key: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for f in ftpdetect_files(session):
        for row in ftp_rows(f):
            ftp_by_key[(row['ff_file'], row['meteor_no'])] = row
    ftps = list(ftp_by_key.values())

    radiants: List[Dict[str, Any]] = []
    for f in sorted(Path(session).glob('*_radiants.txt')):
        radiants.extend(radiant_rows(f))

    used = [False] * len(radiants)
    rows: List[Dict[str, Any]] = []
    for ftp in ftps:
        match: Optional[Dict[str, Any]] = None
        if ftp['_begin_dt'] is not None:
            best_idx, best_dt = -1, JOIN_TOLERANCE_S + 1.0
            for idx, rad in enumerate(radiants):
                if used[idx]:
                    continue
                diff = abs((ftp['_begin_dt'] - rad['_begin_dt']).total_seconds())
                if diff < best_dt:
                    best_dt, best_idx = diff, idx
            if best_idx >= 0 and best_dt <= JOIN_TOLERANCE_S:
                used[best_idx] = True
                match = radiants[best_idx]
        rows.append(merge_row(ftp, match))

    for idx, rad in enumerate(radiants):
        if not used[idx]:
            rows.append(radiant_only_row(rad))
    return rows


def night_detections(rms_data: Path, camera: str, night: str) -> List[Dict[str, Any]]:
    """Detections of one camera-night, each session read once (ArchivedFiles wins)."""
    sessions, _listed = scan_rms_sessions(rms_data, camera, night)
    rows: List[Dict[str, Any]] = []
    for name in sorted(sessions):
        rows.extend(session_detections(sessions[name]))
    return rows


# ---------------------------------------------------------------------------
# RMS config directory and platepar
# ---------------------------------------------------------------------------

_CONFIG_SCAN_SKIP = {'CapturedFiles', 'ArchivedFiles', 'logs', '.git', 'node_modules'}
_CONFIG_SCAN_MAX_DIRS = 300


def live_rms_config_dirs() -> List[Path]:
    """``.config`` directories of running ``RMS.StartCapture`` processes.

    Ground truth for a layout the directory heuristics cannot see: gmnro05
    (Bistrița) runs each camera as ``RMS.StartCapture -c
    ~/source/Stations/<cam>/.config``. Reads ``/proc/<pid>/cmdline`` directly
    rather than ``pgrep -f``, which would match whatever spawned this process.
    Silent on read failures: a process can exit between listdir and read.
    """
    out: List[Path] = []
    try:
        pids = [entry for entry in os.listdir('/proc') if entry.isdigit()]
    except OSError:  # pragma: no cover - /proc always exists on Linux
        return out
    for pid in pids:
        try:
            argv = Path('/proc', pid, 'cmdline').read_bytes().decode().split('\0')
        except (OSError, UnicodeDecodeError):
            continue
        if 'RMS.StartCapture' not in argv:
            continue
        flag = next((f for f in ('-c', '--config') if f in argv), None)
        if flag is None:
            # No -c: RMS reads ./.config, so the working directory is the
            # config directory (the stock single-camera install runs from
            # ~/source/RMS).
            try:
                cwd = Path(os.readlink(os.path.join('/proc', pid, 'cwd')))
            except OSError:
                continue
            if (cwd / '.config').is_file():
                out.append(cwd)
            continue
        idx = argv.index(flag)
        if idx + 1 < len(argv):
            cfg_path = Path(argv[idx + 1])
            if cfg_path.name == '.config':
                out.append(cfg_path.parent)
    return out


def candidate_config_dirs(scfg: Dict[str, Any], live_dirs: Optional[List[Path]] = None) -> List[Path]:
    """Directories that might hold a camera's live ``.config``, best first.

    ``~/RMS_*`` first, then the neighbourhood of ``rms_data_path`` (walked up
    to home and two levels back down: gmnde01 (Berlin) keeps everything under
    ``~/gmn_rms/``), then the directories live RMS processes read. Per-night
    trees are skipped: a session's copied ``.config`` declares the same
    stationID but is never the file to act on.
    """
    home = Path.home()
    out: List[Path] = []
    seen = set()

    def add(d: Path) -> None:
        if len(out) >= _CONFIG_SCAN_MAX_DIRS or d in seen:
            return
        if any(part in _CONFIG_SCAN_SKIP for part in d.parts):
            return
        try:
            if not d.is_dir():
                return
        except OSError:
            return
        seen.add(d)
        out.append(d)

    for d in sorted(home.glob('RMS_*')):
        add(d)

    data_path = Path(scfg.get('rms_data_path') or '')
    for ancestor in data_path.parents:
        if ancestor == home or home not in ancestor.parents:
            continue
        add(ancestor)
        try:
            children = sorted(c for c in ancestor.iterdir() if c.is_dir())
        except OSError:
            continue
        for child in children:
            add(child)
            try:
                for grandchild in sorted(g for g in child.iterdir() if g.is_dir()):
                    add(grandchild)
            except OSError:
                continue

    for d in (live_dirs if live_dirs is not None else live_rms_config_dirs()):
        add(d)

    # Stock RMS checkouts, last: a single-camera station keeps its live
    # .config and mask.bmp in the checkout itself (gmnro06 (Cluj):
    # ~/source/RMS). On multi-camera stations these declare a template or
    # stale stationID, and the ~/RMS_* match above wins anyway.
    for d in (home / 'source' / 'RMS', home / 'RMS'):
        add(d)
    return out


def station_id_of(config_dir: Path) -> Optional[str]:
    """The ``stationID`` an RMS ``.config`` in *config_dir* declares, upper-cased."""
    try:
        text = (Path(config_dir) / '.config').read_text()
    except OSError:
        return None
    m = re.search(r'^\s*stationID\s*:\s*(\S+)', text, re.MULTILINE)
    return m.group(1).strip().upper() if m else None


def resolve_rms_config_dir(camera_code: str, scfg: Dict[str, Any],
                           live_dirs: Optional[List[Path]] = None) -> Optional[Path]:
    """The RMS config directory for a camera (``.config``, platepar, mask).

    1. An explicit ``rms_config_path``.
    2. The candidate directory whose ``.config`` declares ``stationID:
       <camera_code>`` -- the binding RMS itself runs on.
    3. ``~/RMS_<basename of rms_data_path>``, only if it exists.
    """
    override = scfg.get('rms_config_path')
    if override:
        return Path(override)
    want = (camera_code or '').strip().upper()
    if want:
        for candidate in candidate_config_dirs(scfg, live_dirs):
            if station_id_of(candidate) == want:
                return candidate
    cam_suffix = Path(scfg.get('rms_data_path', '')).name
    if cam_suffix:
        fallback = Path.home() / f'RMS_{cam_suffix}'
        if fallback.is_dir():
            return fallback
    return None


def find_latest_platepar(camera_code: str, cfg: Dict[str, Any],
                         live_dirs: Optional[List[Path]] = None) -> Optional[Path]:
    """The newest ``platepar_cmn2010.cal`` for a camera.

    Candidates: the platepar in the camera's resolved RMS config directory
    (the one RMS loads, wherever the layout puts it), plus every session copy
    under ``rms_data_path``; newest mtime wins. ``~/source/Stations/<cam>``
    is NOT assumed live: on several stations it is an inert mirror, and
    reading it first showed a stale pointing after a manual recalibration.
    """
    stations = cfg.get('stations') if isinstance(cfg.get('stations'), dict) else {}
    scfg = stations.get(camera_code) or {}
    candidates: List[Path] = []
    config_dir = resolve_rms_config_dir(camera_code, scfg, live_dirs)
    if config_dir is not None:
        live = config_dir / PLATEPAR_NAME
        if live.is_file():
            candidates.append(live)
    rms_path = scfg.get('rms_data_path')
    if rms_path:
        sessions, _listed = scan_rms_sessions(Path(rms_path), None, None)
        for session in sessions.values():
            pp = session / PLATEPAR_NAME
            if pp.is_file():
                candidates.append(pp)

    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return -1.0

    return max(candidates, key=_mtime) if candidates else None


# The overlay's pointing fallback runs per chunk (stacker) and per clip
# (encoder) on every camera whose config carries no az/alt, at night, beside
# capture. Its platepar lookup is held per process and camera for up to an
# hour, so a refit is picked up within one.
POINTING_TTL_S = 3600


@functools.lru_cache(maxsize=64)
def _platepar_pointing(camera_code: str, rms_data_path: Optional[str],
                       rms_config_path: Optional[str],
                       _hour: int) -> Optional[Tuple[float, float]]:
    """(az_centre, alt_centre) of the platepar find_latest_platepar picks, or
    None. *_hour* is the cache bucket."""
    scfg = {k: v for k, v in (('rms_data_path', rms_data_path),
                              ('rms_config_path', rms_config_path)) if v}
    path = find_latest_platepar(camera_code, {'stations': {camera_code: scfg}})
    if path is None:
        return None
    try:
        data = json.loads(path.read_text())
        az, alt = data.get('az_centre'), data.get('alt_centre')
        if az is not None and alt is not None:
            return float(az), float(alt)
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return None


def enrich_station_cfg_pointing(camera_code: str, station_cfg: Dict[str, Any],
                                cfg: Dict[str, Any]) -> Dict[str, Any]:
    """*station_cfg* with ``az``/``alt`` filled from the camera's platepar when
    the config has none (the overlay's pointing field).

    Never raises: a lookup that fails (a session deleted mid-scan, an
    unreadable disk) leaves the overlay without pointing, not the encode or
    the stack without its product.
    """
    if 'az' in station_cfg and 'alt' in station_cfg:
        return station_cfg
    stations = cfg.get('stations') if isinstance(cfg.get('stations'), dict) else {}
    scfg = stations.get(camera_code) or {}
    try:
        pointing = _platepar_pointing(camera_code, scfg.get('rms_data_path'),
                                      scfg.get('rms_config_path'),
                                      int(time.time() // POINTING_TTL_S))
    except Exception as exc:
        logger.debug('%s: platepar pointing lookup failed: %s', camera_code, exc)
        return station_cfg
    if pointing is None:
        return station_cfg
    logger.debug('%s: az/alt from platepar: az=%.1f alt=%.1f', camera_code, *pointing)
    return {**station_cfg, 'az': pointing[0], 'alt': pointing[1]}
