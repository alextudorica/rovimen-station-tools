"""Station maintenance must work with only the rules in /etc/sudoers.d/rovimen.

Some stations give their user no blanket sudo, only the exact-argument rules
that `updater.sh` writes to /etc/sudoers.d/rovimen (gmnro07 București, gmnro08
Brașov and gmnde00 Johannismühle in September 2026). Anything the unattended
updater runs through `sudo -n` must therefore match one of those rules, or it
silently does nothing there:

- `toggle_rovimen.sh --ensure-crons` probed with `sudo -n true`, which no rule
  allows, so those three stations kept March-July crons and still rebooted at
  12:00 after the fleet moved to 13:45.
- `_ensure_sudoers` read the file back with `sudo -n cat`, which no rule
  allowed either, so it saw a mismatch and rewrote the file on every run,
  logging "a password is required" each time.

The persistent-journal drop-in is written the same way, so it gets the same
check.

These tests run the real scripts under a fake `sudo` that allows exactly the
commands in the sudoers text the updater generates, and fails the rest the way
sudo does.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "rovimen-scripts"
UPDATER = SCRIPTS / "updater.sh"

# Allows a command only if "<resolved path> <args>" equals the command part of
# a NOPASSWD rule in $FAKE_SUDO_RULES, i.e. exact-argument matching (a rule
# naming the command alone allows any arguments, as sudoers does). `cat` of
# the sudoers file prints the rules file, an allowed `tee` stores stdin next to
# the log; everything else that is allowed just succeeds without touching the
# system.
FAKE_SUDO = r"""#!/usr/bin/env bash
args=("$@")
[ "${args[0]}" = "-n" ] && args=("${args[@]:1}")
cmd="${args[0]}"
full=$(command -v "$cmd" || echo "$cmd")
line="$full"
[ "${#args[@]}" -gt 1 ] && line="$full ${args[*]:1}"
if [ "$FAKE_SUDO_MODE" = capture ]; then
    # First pass: no rules exist yet; record what the updater wants to install.
    if [ "$cmd" = tee ] && [ "${args[1]}" = /etc/sudoers.d/rovimen ]; then
        cat > "$FAKE_SUDO_RULES"; exit 0
    fi
    exit 1
fi
# A rule with no arguments allows any arguments, as in sudoers (the systemctl rule).
if sed -n 's/^[^#].*NOPASSWD: //p' "$FAKE_SUDO_RULES" | grep -qxF -e "$line" -e "$full"; then
    echo "ALLOWED $line" >> "$FAKE_SUDO_LOG"
    case "$cmd" in
        cat) cat "$FAKE_SUDO_RULES" ;;
        tee) cat > "$FAKE_SUDO_LOG.$(basename "${args[1]}")" ;;
    esac
    exit 0
fi
echo "REFUSED $line" >> "$FAKE_SUDO_LOG"
echo "sudo: a password is required" >&2
exit 1
"""

# `toggle_rovimen.sh` also rewrites the user crontab; keep the test runner's.
FAKE_CRONTAB = """#!/usr/bin/env bash
[ "$1" = -l ] && exit 1
cat > /dev/null
"""

def _updater_function(name: str, local_dir: Path = SCRIPTS) -> str:
    """Bash that defines one function from updater.sh and runs it."""
    return f"""
set -euo pipefail
log() {{ echo "$*"; }}
LOCAL_DIR={local_dir}
eval "$(sed -n '/^{name}() {{/,/^}}/p' {UPDATER})"
{name}
"""


ENSURE_SUDOERS = _updater_function("_ensure_sudoers")
ENSURE_JOURNAL = _updater_function("_ensure_persistent_journal")


@pytest.fixture
def restricted(tmp_path: Path) -> dict:
    """Env whose `sudo` knows only the rules the updater generates."""
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    for name, body in (("sudo", FAKE_SUDO), ("crontab", FAKE_CRONTAB)):
        (fakebin / name).write_text(body)
        (fakebin / name).chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k != "SUDO_USER"}
    env.update(
        PATH=f"{fakebin}:{env['PATH']}",
        HOME=str(tmp_path),
        FAKE_SUDO_RULES=str(tmp_path / "sudoers"),
        FAKE_SUDO_LOG=str(tmp_path / "sudo.log"),
        FAKE_SUDO_MODE="capture",
    )
    r = _run(["bash", "-c", ENSURE_SUDOERS], env)
    assert (tmp_path / "sudoers").is_file(), r.stdout + r.stderr
    env["FAKE_SUDO_MODE"] = "enforce"
    return env


def _run(cmd: list[str], env: dict) -> subprocess.CompletedProcess:
    # stdin not a TTY, as under cron: toggle_rovimen.sh must not prompt.
    return subprocess.run(cmd, env=env, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=60)


def _sudo_log(env: dict) -> list[str]:
    p = Path(env["FAKE_SUDO_LOG"])
    return p.read_text().splitlines() if p.exists() else []


class TestSudoersSelfUpdate:
    def test_unchanged_file_is_left_alone(self, restricted: dict) -> None:
        r = _run(["bash", "-c", ENSURE_SUDOERS], restricted)
        log = _sudo_log(restricted)
        assert "Sudoers already current" in r.stdout, r.stdout + r.stderr
        assert not [ln for ln in log if ln.startswith("REFUSED")], log
        assert not [ln for ln in log if " tee " in ln], log


CRON_JOBS = ("dawn", "janitor", "reboot")


@pytest.fixture
def cron_station(restricted: dict, tmp_path: Path) -> tuple[dict, Path]:
    """A copy of the scripts in ~/rovimen_scripts (the script chmods
    reboot_guard.sh next to itself), with the sudoers rules the updater
    generates for that directory, as on a station."""
    scripts = tmp_path / "rovimen_scripts"
    scripts.mkdir()
    for name in ("toggle_rovimen.sh", "reboot_guard.sh"):
        (scripts / name).write_text((SCRIPTS / name).read_text())
    env = {**restricted, "FAKE_SUDO_MODE": "capture"}
    _run(["bash", "-c", _updater_function("_ensure_sudoers", scripts)], env)
    return restricted, scripts


def _drop_rules(env: dict, needle: str) -> None:
    rules = Path(env["FAKE_SUDO_RULES"])
    kept = [ln for ln in rules.read_text().splitlines() if needle not in ln]
    rules.write_text("\n".join(kept) + "\n")


def _no_staging_left(scripts: Path) -> None:
    assert not list(scripts.glob(".rovimen_cron_*"))


class TestCronInstall:
    def test_crons_install_from_the_scripts_dir_with_the_current_rules(
            self, cron_station: tuple[dict, Path]) -> None:
        """Root copies the cron files from the station user's own directory: a
        fixed /tmp name can be created first by any other local user."""
        env, scripts = cron_station
        r = _run(["bash", str(scripts / "toggle_rovimen.sh"), "--ensure-crons"], env)
        log = _sudo_log(env)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "Cron files updated" in r.stdout, r.stdout + r.stderr
        assert not [ln for ln in log if ln.startswith("REFUSED")], log
        for job in CRON_JOBS:
            assert f"ALLOWED {shutil.which('cp')} {scripts}/.rovimen_cron_{job} " \
                   f"/etc/cron.d/rovimen-{job}" in log, log
        assert not [ln for ln in log if " /tmp/rovimen_cron_" in ln], log
        _no_staging_left(scripts)

    def test_crons_install_on_a_station_whose_sudoers_still_has_only_the_tmp_rules(
            self, cron_station: tuple[dict, Path]) -> None:
        """gmnro07 (București), gmnro08 (Brașov) and gmnde00 (Johannismühle)
        get the new rules only when the updater rewrites /etc/sudoers.d/rovimen,
        which happens after the cron step of the same pass."""
        env, scripts = cron_station
        _drop_rules(env, "/.rovimen_cron_")
        r = _run(["bash", str(scripts / "toggle_rovimen.sh"), "--ensure-crons"], env)
        log = _sudo_log(env)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "Cron files updated" in r.stdout, r.stdout + r.stderr
        refused = [ln for ln in log if ln.startswith("REFUSED")]
        assert refused and all("/.rovimen_cron_" in ln for ln in refused), log
        for job in CRON_JOBS:
            assert f"ALLOWED {shutil.which('cp')} /tmp/rovimen_cron_{job} " \
                   f"/etc/cron.d/rovimen-{job}" in log, log
        _no_staging_left(scripts)
        assert not [j for j in CRON_JOBS if Path(f"/tmp/rovimen_cron_{j}").exists()]

    def test_crons_install_once_the_tmp_rules_are_gone(
            self, cron_station: tuple[dict, Path]) -> None:
        env, scripts = cron_station
        _drop_rules(env, "/tmp/rovimen_cron_")
        r = _run(["bash", str(scripts / "toggle_rovimen.sh"), "--ensure-crons"], env)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "Cron files updated" in r.stdout, r.stdout + r.stderr
        assert not [ln for ln in _sudo_log(env) if ln.startswith("REFUSED")]

    def test_the_updater_allows_both_staging_paths_during_the_changeover(
            self, cron_station: tuple[dict, Path]) -> None:
        env, scripts = cron_station
        rules = Path(env["FAKE_SUDO_RULES"]).read_text()
        for job in CRON_JOBS:
            assert f" {scripts}/.rovimen_cron_{job} /etc/cron.d/rovimen-{job}\n" in rules
            assert f" /tmp/rovimen_cron_{job} /etc/cron.d/rovimen-{job}\n" in rules


class TestPersistentJournal:
    @pytest.mark.skipif(
        Path("/etc/systemd/journald.conf.d/50-rovimen-persistent.conf").exists(),
        reason="host already has the drop-in, so the write path is not taken")
    def test_drop_in_is_written_with_only_the_rovimen_rules(self, restricted: dict) -> None:
        r = _run(["bash", "-c", ENSURE_JOURNAL], restricted)
        log = _sudo_log(restricted)
        assert "Persistent journal configured" in r.stdout, r.stdout + r.stderr
        assert not [ln for ln in log if ln.startswith("REFUSED")], log
        written = Path(restricted["FAKE_SUDO_LOG"] + ".50-rovimen-persistent.conf").read_text()
        assert written.splitlines()[1:] == ["[Journal]", "Storage=persistent"], written


# rovimen_watchdog.sh under the same rules: everything its recovery runs through
# sudo must be allowed by /etc/sudoers.d/rovimen, and a station whose file
# predates a rule must fail at once and be logged, never prompt.

# The uplink's NetworkManager connection, as `nmcli -g GENERAL.CON-UUID` prints it.
UPLINK_UUID = "7d1c6c3e-35bd-4a3c-9d46-0f3a3e1b9c21"

WATCHDOG_STUBS = {
    "ping": "exit 1\n",
    "ip": "exit 0\n",
    "tailscale": 'echo \'{"BackendState": "Running", "Self": {"Online": false}}\'\n',
    "systemctl": (
        'case "$*" in\n'
        '  "list-units --type=service") echo "NetworkManager.service loaded active running" ;;\n'
        '  "list-unit-files tailscaled.service --no-legend") echo "tailscaled.service enabled enabled" ;;\n'
        'esac\nexit 0\n'
    ),
    "getent": "exit 2\n",
    "sync": "exit 0\n",
    "nmcli": (
        'echo "nmcli $*" >> "$HOME/nmcli.log"\n'
        'case "$*" in\n'
        f'  *"GENERAL.CON-UUID device show"*) echo "{UPLINK_UUID}" ;;\n'
        '  *"device show"*) echo "Wired 1" ;;\n'
        'esac\nexit 0\n'
    ),
}


@pytest.fixture
def restricted_station(tmp_path: Path) -> dict:
    """A station whose `sudo` knows only the updater's rules, with network stubs."""
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    bodies = {"sudo": FAKE_SUDO, "crontab": FAKE_CRONTAB}
    bodies.update({n: "#!/usr/bin/env bash\n" + b for n, b in WATCHDOG_STUBS.items()})
    for name, body in bodies.items():
        (fakebin / name).write_text(body)
        (fakebin / name).chmod(0o755)
    home = tmp_path / "home"
    (home / "rovimen_scripts").mkdir(parents=True)
    for mod in ("rovimen_config.py", "rovimen_night.py"):
        (home / "rovimen_scripts" / mod).write_text((SCRIPTS / mod).read_text())
    (home / "rovimen_scripts" / "config.json").write_text('{"latitude": 45.0, "longitude": 25.0}')
    env = {k: v for k, v in os.environ.items() if k != "SUDO_USER"}
    env.update(
        PATH=f"{fakebin}:{env['PATH']}",
        HOME=str(home),
        FAKE_SUDO_RULES=str(tmp_path / "sudoers"),
        FAKE_SUDO_LOG=str(tmp_path / "sudo.log"),
        FAKE_SUDO_MODE="capture",
        ROVIMEN_WATCHDOG_NOW="2026-11-21T10:00:00Z",
        ROVIMEN_WATCHDOG_RECOVERY_WAIT="0",
        ROVIMEN_WATCHDOG_RECHECK_WAIT="0",
        ROVIMEN_WATCHDOG_SETTLE_WAIT="0",
        ROVIMEN_DAWN_LOCK=str(tmp_path / "dawn.lock"),
        ROVIMEN_RESCUE_HELPER=str(tmp_path / "no-helper"),
        _BIN=str(fakebin),
    )
    _run(["bash", "-c", ENSURE_SUDOERS], env)
    assert (tmp_path / "sudoers").is_file()
    env["FAKE_SUDO_MODE"] = "enforce"
    return env


def _stub(env: dict, name: str, body: str) -> None:
    (Path(env["_BIN"]) / name).write_text("#!/usr/bin/env bash\n" + body)


class TestWatchdogLadder:
    def test_networking_restart_and_reboot_use_only_the_rovimen_rules(
            self, restricted_station: dict) -> None:
        state = Path(restricted_station["HOME"]) / "rovimen_scripts/logs/.watchdog_state"
        for _ in range(3):
            r = _run(["bash", str(SCRIPTS / "rovimen_watchdog.sh")], restricted_station)
            assert r.returncode == 0, r.stderr
            # Past the 30-min minimum outage, so the third run reaches the reboot.
            with state.open("a") as f:
                f.write(f"outage_start={int(time.time()) - 1900}\n")
        log = _sudo_log(restricted_station)
        assert not [ln for ln in log if ln.startswith("REFUSED")], log
        assert any(ln.endswith("systemctl restart NetworkManager") for ln in log), log
        assert any(ln.endswith("systemctl restart tailscaled") for ln in log), log
        assert any(ln.startswith("ALLOWED ") and ln.endswith("reboot") for ln in log), log

    def test_dns_rung_without_an_nmcli_rule_falls_back_without_prompting(
            self, restricted_station: dict) -> None:
        """A station whose /etc/sudoers.d/rovimen predates the DNS rule (the
        fixture generated it with no default route)."""
        env = _dns_failing(restricted_station)
        assert "connection up" not in Path(env["FAKE_SUDO_RULES"]).read_text()
        r = _run(["bash", str(SCRIPTS / "rovimen_watchdog.sh")], env)
        assert r.returncode == 0, r.stderr
        log = _sudo_log(env)
        assert [ln for ln in log if ln.startswith("REFUSED") and "nmcli" in ln], log
        assert not [ln for ln in log if ln.startswith("REFUSED") and "nmcli" not in ln], log
        assert (f"nmcli -w 30 connection up uuid {UPLINK_UUID}"
                in (Path(env["HOME"]) / "nmcli.log").read_text())
        wlog = (Path(env["HOME"]) / "rovimen_scripts/logs/watchdog.log").read_text()
        assert "trying nmcli without sudo" in wlog


def _dns_failing(env: dict) -> dict:
    """Internet answers by IP and Tailscale is up, names do not resolve; the
    default route runs over eno1."""
    _stub(env, "ping", "exit 0\n")
    _stub(env, "ip", '[ "$1 $2" = "route show" ] && echo "default via 10.0.0.1 dev eno1"\nexit 0\n')
    _stub(env, "tailscale", "echo '{\"BackendState\": \"Running\", \"Self\": {\"Online\": true}}'\n")
    return {**env, "ROVIMEN_WATCHDOG_DNS_FAIL_THRESHOLD": "1"}


def _regenerate_sudoers(env: dict) -> str:
    """What the updater writes to /etc/sudoers.d/rovimen on this station now."""
    _run(["bash", "-c", ENSURE_SUDOERS], {**env, "FAKE_SUDO_MODE": "capture"})
    return Path(env["FAKE_SUDO_RULES"]).read_text()


class TestDnsRecoveryRule:
    """gmnro07 (București), gmnro08 (Brașov) and gmnde00 (Johannismühle) have
    no sudo beyond these rules, so the DNS rung's `sudo -n nmcli` did nothing
    there until the updater generated a rule naming the uplink's connection."""

    def test_the_updater_writes_one_exact_rule_for_the_uplink_connection(
            self, restricted_station: dict) -> None:
        env = _dns_failing(restricted_station)
        rules = _regenerate_sudoers(env)
        nmcli = Path(env["_BIN"]) / "nmcli"
        assert [ln.split("NOPASSWD: ", 1)[1] for ln in rules.splitlines()
                if "connection up" in ln and not ln.startswith("#")] == [
            f"{nmcli} -w 30 connection up uuid {UPLINK_UUID}"]

    def test_no_networkmanager_connection_means_no_rule(self, restricted_station: dict) -> None:
        env = _dns_failing(restricted_station)
        _stub(env, "nmcli", "exit 0\n")
        assert "connection up" not in _regenerate_sudoers(env)

    def test_the_dns_rung_runs_under_its_rule(self, restricted_station: dict) -> None:
        env = _dns_failing(restricted_station)
        _regenerate_sudoers(env)
        r = _run(["bash", str(SCRIPTS / "rovimen_watchdog.sh")], env)
        assert r.returncode == 0, r.stderr
        log = _sudo_log(env)
        assert not [ln for ln in log if ln.startswith("REFUSED")], log
        assert any(ln.startswith("ALLOWED ")
                   and ln.endswith(f"nmcli -w 30 connection up uuid {UPLINK_UUID}")
                   for ln in log), log
        wlog = (Path(env["HOME"]) / "rovimen_scripts/logs/watchdog.log").read_text()
        assert "re-activating connection 'Wired 1' on eno1" in wlog
        assert "trying nmcli without sudo" not in wlog


# The focus helper (camera_focus.py, camera-focus.service) served live camera
# pulls and a root nmap scan on 0.0.0.0:8080 with no authentication. It left
# the bundle; the updater retires it on stations that still run it.
FOCUS_UNIT = "camera-focus.service"
FOCUS_FILE = f"/etc/systemd/system/{FOCUS_UNIT}"
RETIRE_FOCUS = "_retire_camera_focus"


def _focus_station(env: dict, tmp_path: Path, *, installed: bool) -> tuple[dict, Path]:
    """A station whose systemd does (or does not) know the focus unit, with
    the stale bundle copies still in ~/rovimen_scripts (the apply rsync has
    no --delete)."""
    fakebin = Path(env["PATH"].split(":")[0])
    listing = f'echo "{FOCUS_UNIT} enabled enabled"' if installed else "exit 1"
    (fakebin / "systemctl").write_text(
        "#!/usr/bin/env bash\n"
        'echo "systemctl $*" >> "$HOME/systemctl.log"\n'
        f'case "$*" in "list-unit-files {FOCUS_UNIT} --no-legend") {listing} ;; esac\n')
    (fakebin / "systemctl").chmod(0o755)
    # The rules as the updater writes them on this station (naming its systemctl).
    _run(["bash", "-c", ENSURE_SUDOERS], {**env, "FAKE_SUDO_MODE": "capture"})
    scripts = tmp_path / "rovimen_scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / "camera_focus.py").write_text("# stale\n")
    (scripts / FOCUS_UNIT).write_text("[Service]\n")
    return env, scripts


def _retire(env: dict, scripts: Path) -> subprocess.CompletedProcess:
    return _run(["bash", "-c", _updater_function(RETIRE_FOCUS, scripts)], env)


class TestFocusHelperRetirement:
    def test_a_station_running_it_stops_it_and_removes_the_unit(
            self, restricted: dict, tmp_path: Path) -> None:
        env, scripts = _focus_station(restricted, tmp_path, installed=True)
        r = _retire(env, scripts)
        assert r.returncode == 0, r.stdout + r.stderr
        log = _sudo_log(env)
        assert not [ln for ln in log if ln.startswith("REFUSED")], log
        assert any(ln.endswith(f"systemctl disable --now {FOCUS_UNIT}") for ln in log), log
        assert f"ALLOWED {shutil.which('rm')} -f {FOCUS_FILE}" in log, log
        assert any(ln.endswith("systemctl daemon-reload") for ln in log), log
        assert not (scripts / "camera_focus.py").exists()
        assert not (scripts / FOCUS_UNIT).exists()

    def test_a_restricted_station_with_old_rules_converges_on_the_next_pass(
            self, restricted: dict, tmp_path: Path) -> None:
        """gmnro07 (București), gmnro08 (Brașov) and gmnde00 (Johannismühle):
        the rm rule arrives with the sudoers rewrite at the end of the pass,
        so the first pass only stops and disables the unit."""
        env, scripts = _focus_station(restricted, tmp_path, installed=True)
        _drop_rules(env, FOCUS_FILE)
        r = _retire(env, scripts)
        assert r.returncode == 0, r.stdout + r.stderr
        log = _sudo_log(env)
        assert any(ln.startswith("ALLOWED ") and ln.endswith(f"systemctl disable --now {FOCUS_UNIT}")
                   for ln in log), log
        assert [ln for ln in log if ln.startswith("REFUSED")] == [
            f"REFUSED {shutil.which('rm')} -f {FOCUS_FILE}"], log
        assert "could not remove" in r.stdout

        # The end of the pass rewrites the sudoers file; the next pass finishes.
        _run(["bash", "-c", ENSURE_SUDOERS], {**env, "FAKE_SUDO_MODE": "capture"})
        Path(env["FAKE_SUDO_LOG"]).unlink()
        r = _retire(env, scripts)
        log = _sudo_log(env)
        assert not [ln for ln in log if ln.startswith("REFUSED")], log
        assert f"ALLOWED {shutil.which('rm')} -f {FOCUS_FILE}" in log, log

    def test_a_station_without_it_is_left_alone(self, restricted: dict, tmp_path: Path) -> None:
        env, scripts = _focus_station(restricted, tmp_path, installed=False)
        r = _retire(env, scripts)
        assert r.returncode == 0, r.stdout + r.stderr
        assert _sudo_log(env) == []
        assert "Focus helper" not in r.stdout
        assert not (scripts / "camera_focus.py").exists()
        assert not (scripts / FOCUS_UNIT).exists()

    def test_the_sudoers_carries_the_exact_rm_rule_and_no_install_rule(
            self, restricted: dict) -> None:
        rules = Path(restricted["FAKE_SUDO_RULES"]).read_text()
        assert f"NOPASSWD: {shutil.which('rm')} -f {FOCUS_FILE}\n" in rules
        assert f"/etc/systemd/system/{FOCUS_UNIT}" not in rules.replace(
            f"{shutil.which('rm')} -f {FOCUS_FILE}", "")
