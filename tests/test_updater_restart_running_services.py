"""The updater restarts every running system service after an update.

gmnro06 (Cluj), 2026-11-23: rovimen-station-api was disabled but ran every
boot, started as a dependency of the pusher and the indexer. The updater
skipped disabled units, so the API kept the previous bundle in memory while
the files on disk changed; its lazy imports then mixed versions and every
clip render failed until a manual restart.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

UPDATER = Path(__file__).resolve().parent.parent / "rovimen-scripts" / "updater.sh"

# systemctl that answers from FAKE_ACTIVE / FAKE_ENABLED (space-separated unit names).
FAKE_SYSTEMCTL = """#!/usr/bin/env bash
unit="${@: -1}"
case "$1" in
    is-active)  [[ " $FAKE_ACTIVE " == *" $unit "* ]] ;;
    is-enabled) [[ " $FAKE_ENABLED " == *" $unit "* ]] ;;
    show)       echo 0 ;;
    *)          exit 0 ;;
esac
"""

FAKE_SUDO = """#!/usr/bin/env bash
[ "$1" = -n ] && shift
echo "$*" >> "$FAKE_SUDO_LOG"
"""

HARNESS = f"""
set -euo pipefail
log() {{ echo "$*"; }}
_nrestarts() {{ echo 0; }}
SERVICES=(color-capture rovimen-station-api detection-indexer rovimen-pusher)
_ffmpeg_active=${{FFMPEG_ACTIVE:-false}}
eval "$(sed -n '/^_restart_system_services() {{/,/^}}/p' {UPDATER})"
restarted=()
declare -A restart_base=()
_restart_system_services
echo "RESTARTED ${{restarted[*]:-}}"
"""


def _run(tmp_path: Path, *, active: str, enabled: str, ffmpeg: bool = False) -> tuple[list[str], list[str]]:
    fakebin = tmp_path / "bin"
    fakebin.mkdir(exist_ok=True)
    for name, body in (("systemctl", FAKE_SYSTEMCTL), ("sudo", FAKE_SUDO)):
        (fakebin / name).write_text(body)
        (fakebin / name).chmod(0o755)
    log = tmp_path / "sudo.log"
    env = {**os.environ, "PATH": f"{fakebin}:{os.environ['PATH']}", "FAKE_ACTIVE": active,
           "FAKE_ENABLED": enabled, "FAKE_SUDO_LOG": str(log),
           "FFMPEG_ACTIVE": "true" if ffmpeg else "false"}
    r = subprocess.run(["bash", "-c", HARNESS], env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr
    restarted = r.stdout.strip().splitlines()[-1].removeprefix("RESTARTED").split()
    return restarted, log.read_text().splitlines() if log.exists() else []


def test_a_running_api_started_as_a_dependency_is_restarted(tmp_path: Path) -> None:
    restarted, sudo = _run(tmp_path,
                           active="rovimen-station-api detection-indexer rovimen-pusher",
                           enabled="detection-indexer rovimen-pusher")
    assert "rovimen-station-api" in restarted
    assert "systemctl restart rovimen-station-api" in sudo


def test_a_service_turned_off_is_left_alone(tmp_path: Path) -> None:
    # toggle_rovimen off stops and disables, so the unit is inactive.
    restarted, sudo = _run(tmp_path, active="rovimen-station-api",
                           enabled="rovimen-station-api")
    assert restarted == ["rovimen-station-api"]
    assert sudo == ["systemctl restart rovimen-station-api"]


def test_colour_capture_is_not_restarted_while_ffmpeg_records(tmp_path: Path) -> None:
    restarted, sudo = _run(tmp_path, active="color-capture rovimen-station-api",
                           enabled="color-capture rovimen-station-api", ffmpeg=True)
    assert restarted == ["rovimen-station-api"]
    assert "systemctl restart color-capture" not in sudo
