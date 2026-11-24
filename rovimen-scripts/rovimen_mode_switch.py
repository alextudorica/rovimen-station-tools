#!/usr/bin/env python
"""Out-of-band camera day/night mode switch.

For stations where RMS reads the video through a local RTSP relay
(mediamtx on 127.0.0.1): RMS's own switch_camera_modes extracts the camera
IP from the device URL (Utils/CameraControl.cameraControlV2), which is the
relay's loopback address there, so the dvrip mode switch must be driven
out-of-band. This script mirrors RMS.CaptureModeSwitcher: sun altitude
above -9 deg means day, below means night. Run from cron every 5 minutes
with the RMS venv python, cwd anywhere, as ~/rovimen_scripts/rovimen_mode_switch.py:
it imports rovimen_night from its own directory.

Usage:
  rovimen_mode_switch.py --rms-dir ~/RMS --lat 45.0 --lon 26.0 \
      --settings ~/camera_settings_rovimen.json --camera-ip IP [--camera-ip IP2]

State: ~/.cache/rovimen_mode_switch.json (per-IP last applied mode).
A failed switch stays unrecorded, so the next cron run retries it.
"""
import argparse
import inspect
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import rovimen_night

# Same threshold as RMS.CaptureModeSwitcher. RMS computes the sun with ephem;
# rovimen_night's altitude agrees with it to 0.01 deg around -9 deg, where
# ephem applies no refraction (a few seconds of switch time, on a 5 min cron).
SWITCH_HORIZON_DEG = -9.0

# The credentials RMS master's cameraControl logs in with (dvrip defaults).
CAMERA_USER = 'admin'
CAMERA_PWD = ''


def switch_mode(camera_control, ip: str, mode: str, settings: str) -> None:
    """Run SwitchMode through RMS's cameraControl, on either signature.

    RMS master has cameraControl(ip, cmd, opts, ...); the September 2026
    release (CroatianMeteorNetwork/RMS#1001) inserts camera_user and
    camera_pwd before cmd, so the old call raises TypeError there.
    """
    if 'camera_user' in inspect.signature(camera_control).parameters:
        camera_control(ip, CAMERA_USER, CAMERA_PWD, 'SwitchMode', [mode],
                       camera_settings_path=settings)
    else:
        camera_control(ip, 'SwitchMode', [mode], camera_settings_path=settings)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument('--rms-dir', required=True)
    p.add_argument('--lat', type=float, required=True)
    p.add_argument('--lon', type=float, required=True)
    p.add_argument('--settings', required=True)
    p.add_argument('--camera-ip', action='append', required=True)
    args = p.parse_args()

    rms_dir = os.path.expanduser(args.rms_dir)
    sys.path.insert(0, rms_dir)
    os.chdir(rms_dir)  # RMS loggers and relative paths expect this

    alt = rovimen_night.sun_altitude_deg(args.lat, args.lon, datetime.now(timezone.utc))
    mode = 'day' if alt > SWITCH_HORIZON_DEG else 'night'

    state_path = Path.home() / '.cache' / 'rovimen_mode_switch.json'
    state_path.parent.mkdir(exist_ok=True)
    try:
        state = json.loads(state_path.read_text())
    except Exception:
        state = {}

    settings = os.path.expanduser(args.settings)
    changed = False
    from RMS.Logger import LoggingManager  # noqa: F401 (quiets dvrip logging)
    from Utils.CameraControl import cameraControl
    for ip in args.camera_ip:
        if state.get(ip) == mode:
            continue
        print(f'sun_alt={alt:.1f} switching {ip} -> {mode}', flush=True)
        try:
            switch_mode(cameraControl, ip, mode, settings)
            state[ip] = mode
            changed = True
            print(f'{ip}: switched to {mode}', flush=True)
        except Exception as e:
            print(f'{ip}: switch FAILED: {e} (will retry next run)', flush=True)
    if changed:
        state_path.write_text(json.dumps(state))
    return 0


if __name__ == '__main__':
    sys.exit(main())
