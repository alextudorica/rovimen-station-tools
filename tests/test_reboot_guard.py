"""Tests for rovimen-scripts/reboot_guard.sh — the daily pre-capture reboot gate.

The functional tests run the real script against a fixture HOME/config.json with
stub sudo/sleep/date binaries first on PATH. The sudo stub only *records* the
call (it must never exec /sbin/reboot), so "did the guard reboot?" is simply
whether the call log names it. The date stub honours FAKE_NOW so a test can
place the run at any hour of any day without touching the system clock.

The property that matters most: the guard reboots only when it has positive
evidence that morning processing finished. Every other outcome — an incomplete
night, a timeout, a station opted out of reboots, no stations at all — must
leave the station running.
"""

import json
import os
import stat
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GUARD = REPO_ROOT / "rovimen-scripts" / "reboot_guard.sh"
TOGGLE = REPO_ROOT / "rovimen-scripts" / "toggle_rovimen.sh"

# The guard must never actually reboot the machine running the tests.
SUDO_STUB = """#!/usr/bin/env bash
echo "sudo $*" >> "$FAKE_LOG"
exit 0
"""

# The poll interval is a real 60 s sleep; the tests must not serve it.
SLEEP_STUB = """#!/usr/bin/env bash
echo "sleep $*" >> "$FAKE_LOG"
exit 0
"""

# Stub date pinned to FAKE_NOW (epoch seconds) so the tests can run the guard at
# 09:00 or at 16:00 without waiting for the clock. Relative dates the script
# asks for are resolved against FAKE_NOW, not against the real now; everything
# else is delegated to the real date.
DATE_STUB = """#!/usr/bin/env bash
real=""
for candidate in /bin/date /usr/bin/date; do
    [ -x "$candidate" ] && { real="$candidate"; break; }
done
now="${FAKE_NOW:-$($real -u +%s)}"
opts=(); fmt=(); ref="@$now"
while [ $# -gt 0 ]; do
    case "$1" in
        -u) opts+=(-u); shift ;;
        -d) case "$2" in
                yesterday) ref="@$((now - 86400))" ;;
                *)         ref="$2" ;;
            esac
            shift 2 ;;
        +*) fmt+=("$1"); shift ;;
        *)  opts+=("$1"); shift ;;
    esac
done
exec "$real" "${opts[@]}" -d "$ref" "${fmt[@]}"
"""

# A fixed reference day, so the expected night directory is spelled out in the
# tests rather than recomputed by the same rule the script uses.
REF_DAY = datetime(2026, 3, 23, tzinfo=timezone.utc)
LAST_COMPLETED_NIGHT = "20260322"


def _epoch_at(hour: int) -> str:
    return str(int(REF_DAY.replace(hour=hour).timestamp()))


@pytest.fixture()
def guard_env(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_dir = tmp_path / "fake"
    home = tmp_path / "home"
    for d in (fake_bin, fake_dir, home):
        d.mkdir()

    for name, body in (("sudo", SUDO_STUB), ("sleep", SLEEP_STUB), ("date", DATE_STUB)):
        p = fake_bin / name
        p.write_text(body)
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    fake_log = fake_dir / "calls.log"
    fake_log.write_text("")

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "HOME": str(home),
            "FAKE_LOG": str(fake_log),
            "FAKE_NOW": _epoch_at(12),
            "ROVIMEN_CONFIG": str(tmp_path / "config.json"),
            # Three polls, not four hours: the timeout path has to be reachable.
            "ROVIMEN_REBOOT_MAX_WAIT": "180",
            "ROVIMEN_REBOOT_POLL": "60",
        }
    )
    return env, tmp_path, fake_log


def write_config(
    tmp_path: Path,
    stations: list[str],
    *,
    allow_reboot: bool | None = None,
    path_key: str = "videocapture_path",
) -> Path:
    """Write a station config and return the capture root it points at."""
    capture = tmp_path / "capture"
    capture.mkdir(exist_ok=True)
    cfg: dict = {
        path_key: str(capture),
        "stations": {code: {"rms_data_path": f"/data/{code}"} for code in stations},
    }
    if allow_reboot is not None:
        cfg["watchdog"] = {"allow_reboot": allow_reboot}
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    return capture


def mark_night(capture: Path, station: str, night: str, *, morning_done: bool = True) -> None:
    night_dir = capture / station / night
    night_dir.mkdir(parents=True, exist_ok=True)
    (night_dir / "state.json").write_text(json.dumps({"morning_done": morning_done}))


def run_guard(env) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(GUARD)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def rebooted(fake_log: Path) -> bool:
    return "/sbin/reboot" in fake_log.read_text()


def test_reboot_guard_bash_syntax():
    subprocess.run(["bash", "-n", str(GUARD)], check=True)


# ---------------------------------------------------------------------------
# night_date — the night directory is named for the evening capture started
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("hour", [9, 12, 13, 16])
def test_night_date_is_always_the_last_completed_night(guard_env, hour):
    """Whatever the hour, the guard waits on the night that ended this morning.

    Night directories are named for the date capture STARTED, so the night that
    finished this morning is always yesterday's. The old code read the clock and
    flipped to today's date at 12:00 — the exact minute cron fires it — which
    made it wait on a directory that does not exist until dusk.
    """
    env, tmp_path, fake_log = guard_env
    env["FAKE_NOW"] = _epoch_at(hour)
    capture = write_config(tmp_path, ["RO0010"])
    mark_night(capture, "RO0010", LAST_COMPLETED_NIGHT)

    result = run_guard(env)

    assert f"Night date: {LAST_COMPLETED_NIGHT}" in result.stdout
    # Not merely logged — the guard read that night's marker and acted on it.
    assert f"OK: RO0010/{LAST_COMPLETED_NIGHT}" in result.stdout
    assert rebooted(fake_log), result.stdout


def test_todays_directory_is_never_the_one_awaited(guard_env):
    """A marker under today's date must not satisfy the guard.

    This is the shape of the original bug: tonight's directory is empty (or
    absent) at noon, so treating it as the night under test can only time out.
    """
    env, tmp_path, fake_log = guard_env
    capture = write_config(tmp_path, ["RO0010"])
    mark_night(capture, "RO0010", REF_DAY.strftime("%Y%m%d"))

    result = run_guard(env)

    assert not rebooted(fake_log), result.stdout
    assert result.returncode != 0


# ---------------------------------------------------------------------------
# Completion → reboot
# ---------------------------------------------------------------------------


def test_reboots_once_every_station_is_done(guard_env):
    env, tmp_path, fake_log = guard_env
    capture = write_config(tmp_path, ["RO0010", "RO000Y"])
    for station in ("RO0010", "RO000Y"):
        mark_night(capture, station, LAST_COMPLETED_NIGHT)

    result = run_guard(env)

    assert result.returncode == 0, result.stderr
    assert "sudo /sbin/reboot" in fake_log.read_text()
    assert "All processes complete — rebooting" in result.stdout


def test_decommissioned_camera_is_not_waited_on(guard_env):
    """dawn_process skips decommissioned cameras and never marks them done, so a
    guard that waited on them would never reboot the station."""
    env, tmp_path, fake_log = guard_env
    capture = write_config(tmp_path, ["RO0010", "RO000Y"])
    cfg = json.loads((tmp_path / "config.json").read_text())
    cfg["stations"]["RO000Y"]["decommissioned"] = True
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    mark_night(capture, "RO0010", LAST_COMPLETED_NIGHT)

    result = run_guard(env)

    assert rebooted(fake_log), result.stdout
    assert "RO000Y" not in result.stdout


def test_guard_waits_on_exactly_the_cameras_dawn_marks_done(guard_env):
    import dawn_process  # conftest puts rovimen-scripts on sys.path

    env, tmp_path, fake_log = guard_env
    write_config(tmp_path, ["RO0010", "RO000Y", "GH662S"])
    cfg = json.loads((tmp_path / "config.json").read_text())
    cfg["stations"]["RO000Y"]["decommissioned"] = True
    cfg["stations"]["GH662S"]["rms"] = False
    (tmp_path / "config.json").write_text(json.dumps(cfg))

    result = run_guard(env)

    guard_stations = next(line.split("Stations:", 1)[1].split()
                          for line in result.stdout.splitlines() if "Stations:" in line)
    assert set(guard_stations) == set(dawn_process._active_stations(cfg["stations"]))


def test_capture_root_follows_the_key_dawn_process_wrote_under(guard_env):
    """Stations configured with color_video_path must still be found.

    flags_manager resolves the capture root through a longer key chain than the
    guard used to; a station on the legacy key polled a directory nothing ever
    writes to and could only ever time out.
    """
    env, tmp_path, fake_log = guard_env
    capture = write_config(tmp_path, ["RO0010"], path_key="color_video_path")
    mark_night(capture, "RO0010", LAST_COMPLETED_NIGHT)

    result = run_guard(env)

    assert result.returncode == 0, result.stderr
    assert f"state: {capture}/RO0010/{LAST_COMPLETED_NIGHT}/state.json" in result.stdout
    # The marker was actually read, not merely stepped over on the way out.
    assert f"OK: RO0010/{LAST_COMPLETED_NIGHT}" in result.stdout
    assert rebooted(fake_log), result.stdout


def test_dead_colour_disk_waits_where_dawn_writes_on_the_fallback(guard_env):
    """Configured root unwritable (dead disk): dawn writes state.json under the
    fallback root, so the guard must look there.

    gmnro01 (Ghirdoveni) polled /mnt/data/color_capture/... for a week after
    its colour disk died and never rebooted, while every night was marked done
    under ~/color_capture."""
    env, tmp_path, fake_log = guard_env
    capture = write_config(tmp_path, ["RO000A"])
    capture.chmod(0o555)
    try:
        fallback = Path(env["HOME"]) / "color_capture"
        mark_night(fallback, "RO000A", LAST_COMPLETED_NIGHT)

        result = run_guard(env)
    finally:
        capture.chmod(0o755)

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"state: {fallback}/RO000A/{LAST_COMPLETED_NIGHT}/state.json" in result.stdout
    assert rebooted(fake_log), result.stdout


# ---------------------------------------------------------------------------
# Timeout → no reboot
# ---------------------------------------------------------------------------


def test_timeout_does_not_reboot(guard_env):
    """A timeout means morning processing is wedged — the worst moment to reboot."""
    env, tmp_path, fake_log = guard_env
    write_config(tmp_path, ["RO0010"])  # no state.json at all

    result = run_guard(env)

    assert not rebooted(fake_log), result.stdout
    assert result.returncode != 0
    assert "ERROR" in result.stdout
    assert "NOT rebooting" in result.stdout
    assert "rebooting anyway" not in result.stdout


def test_timeout_when_morning_done_is_false(guard_env):
    """A present-but-unfinished state.json is not completion."""
    env, tmp_path, fake_log = guard_env
    capture = write_config(tmp_path, ["RO0010"])
    mark_night(capture, "RO0010", LAST_COMPLETED_NIGHT, morning_done=False)

    result = run_guard(env)

    assert not rebooted(fake_log), result.stdout
    assert result.returncode != 0


def test_one_lagging_station_holds_the_reboot(guard_env):
    env, tmp_path, fake_log = guard_env
    capture = write_config(tmp_path, ["RO0010", "RO000Y"])
    mark_night(capture, "RO0010", LAST_COMPLETED_NIGHT)

    result = run_guard(env)

    assert not rebooted(fake_log), result.stdout
    assert result.returncode != 0
    assert f"Waiting for RO000Y/{LAST_COMPLETED_NIGHT}" in result.stdout


# ---------------------------------------------------------------------------
# Per-station opt-out
# ---------------------------------------------------------------------------


def test_allow_reboot_false_never_reboots(guard_env):
    """watchdog.allow_reboot=false wins even when the night is complete.

    Some station hardware hangs at POST and only comes back with an on-site
    power cycle, so the flag is load-bearing — rovimen_watchdog.sh honours it
    and the guard has to as well.
    """
    env, tmp_path, fake_log = guard_env
    capture = write_config(tmp_path, ["RO0010"], allow_reboot=False)
    mark_night(capture, "RO0010", LAST_COMPLETED_NIGHT)

    result = run_guard(env)

    assert result.returncode == 0, result.stderr
    assert not rebooted(fake_log), result.stdout
    assert "allow_reboot=false" in result.stdout


def test_allow_reboot_true_is_the_default(guard_env):
    """A config with no watchdog block keeps the fleet's normal behaviour."""
    env, tmp_path, fake_log = guard_env
    capture = write_config(tmp_path, ["RO0010"], allow_reboot=True)
    mark_night(capture, "RO0010", LAST_COMPLETED_NIGHT)

    result = run_guard(env)

    assert result.returncode == 0, result.stderr
    assert rebooted(fake_log), result.stdout


# ---------------------------------------------------------------------------
# Empty station list
# ---------------------------------------------------------------------------


def test_empty_stations_does_not_reboot(guard_env):
    """With nothing to wait for there is nothing to conclude — never reboot."""
    env, tmp_path, fake_log = guard_env
    write_config(tmp_path, [])

    result = run_guard(env)

    assert result.returncode == 0, result.stderr
    assert not rebooted(fake_log), result.stdout
    assert "No stations in config" in result.stdout


# ---------------------------------------------------------------------------
# The cron entry toggle_rovimen.sh installs
# ---------------------------------------------------------------------------


def _cron_block(name: str) -> str:
    """Return the heredoc body toggle_rovimen.sh writes for one cron.d file."""
    body = TOGGLE.read_text()
    start = body.index(f'cat > "$SCRIPTS_DIR/.rovimen_cron_{name}" <<EOF\n')
    start = body.index("\n", start) + 1
    return body[start : body.index("\nEOF\n", start)]


def test_toggle_rovimen_bash_syntax():
    subprocess.run(["bash", "-n", str(TOGGLE)], check=True)


def test_reboot_cron_entry_shape():
    """cron.d format, 13:45 UTC, running the guard — 34 min before the
    earliest fleet sunset (14:19 UTC) and 30 min after the 13:15 dawn deadline."""
    lines = [ln for ln in _cron_block("reboot").splitlines() if not ln.startswith("#")]
    assert len(lines) == 1, lines
    fields = lines[0].split()
    assert fields[:5] == ["45", "13", "*", "*", "*"]
    # Sixth field is the user: these are cron.d files, not a user crontab.
    assert fields[5] == "$INSTALL_USER"
    assert "reboot_guard.sh" in lines[0]


@pytest.mark.parametrize("name", ["dawn", "janitor", "reboot"])
def test_cron_files_do_not_claim_a_timezone_cron_ignores(name):
    """CRON_TZ must not appear: Debian/Ubuntu cron does not implement it.

    The daemon (ISC/Vixie 3.0pl1) evaluates schedules in its own timezone and
    the man page lists per-crontab timezones under LIMITATIONS; TZ= reaches the
    child process only. A CRON_TZ line would read as a pinned schedule while
    doing nothing, which is worse than the comment that is there instead.
    """
    block = _cron_block(name)
    assert "CRON_TZ" not in block
    assert not any(ln.startswith("TZ=") for ln in block.splitlines())
