#!/usr/bin/env python3
"""Replay camera_stall_guard over a past night, on a station, without touching a camera.

Runs ``check()`` every STEP minutes against the real RMS log and FF files, cut
off at each simulated "now", with a fake reboot that only records the call. The
data after a simulated reboot is what really happened, not what the reboot
would have changed, so this shows when the guard would act, not the outcome.

    python3 replay_cam_stall.py <RMS .config> <RMS log> <start ISO> <end ISO> [step_min]

Copy it next to camera_stall_guard.py, camera_fps_guard.py and rovimen_night.py.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List

import camera_fps_guard as fps
import camera_stall_guard as g


def main() -> int:
    config, log = Path(sys.argv[1]), Path(sys.argv[2])
    start = datetime.fromisoformat(sys.argv[3]).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(sys.argv[4]).replace(tzinfo=timezone.utc)
    step = timedelta(minutes=int(sys.argv[5]) if len(sys.argv) > 5 else 3)
    rms = fps.parse_rms_config(config)
    assert rms is not None
    cam = g.Camera(rms, fps.RmsProcess(config, sys.executable, None), g.camera_ip(config))
    lines_all = log.read_text(errors='replace').splitlines()
    ff_all = g.ff_times(rms, start - timedelta(hours=1))
    now = start
    real_ff_times = g.ff_times

    def ff_until_now(c: fps.RmsCamera, since: datetime) -> List[datetime]:
        return [t for t in ff_all if since < t <= now]

    g.ff_times = ff_until_now
    fps._tail_lines = lambda path, nbytes=0: [l for l in lines_all if l[:19] <= now.strftime('%Y/%m/%d %H:%M:%S')]
    state: dict = {}
    calls: List[str] = []
    while now <= end:
        out = g.check([cam], 'fix', state, now, reboot=lambda c: calls.append(now.isoformat()) or 'rebooted')
        for line in out:
            if 'camera reboot via' in line or 'delivering frames again' in line:
                print(now.strftime('%m-%d %H:%M'), line)
        now += step
    g.ff_times = real_ff_times
    print('reboots:', len(calls), [c[11:16] for c in calls])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
