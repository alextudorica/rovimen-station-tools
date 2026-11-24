"""The updater's main path: verify, back up, apply, then record the version.

Pins down the two ways a station got stuck on an old bundle or half an update:

* gmnro05 (Bistrița), 2026-11-06 and 11-17: bundle_verify recorded "build N
  verified", then the `cp -a` backup died because the janitor deleted FF
  thumbnails mid-copy. Every later run refused N as a rollback ("526 <= last
  verified build 526") until a newer build was published. Now the floor is the
  installed `.version`, the backup leaves runtime data out and cannot fail the
  update, and files vanishing during it are normal.
* `.version` arrived inside the apply rsync, so a run that died after the apply
  left the next run "Already up to date" without config migration or restarts.
  Now `.version` is written last.

`updater.sh` runs for real with ssh, rsync (remote pulls only), systemctl,
sudo, pgrep and sleep stubbed on PATH; the bundle is signed with a test key and
verified by the real bundle_verify.py. No test waits in real time.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "deployment"))
import sign_bundle  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
UPDATER = REPO / "rovimen-scripts" / "updater.sh"
VERIFIER = REPO / "rovimen-scripts" / "bundle_verify.py"

# Fake config_migrate.py shipped in the bundle: counts its runs and, when the
# test drops a kill file, SIGKILLs the updater (a power cut or a killed unit
# between the apply and the end of the run).
FAKE_MIGRATE = """import os, signal
from pathlib import Path
home = Path.home()
with open(home / "migrate_calls", "a") as fh:
    fh.write("x\\n")
kill = home / "kill_after_migrate"
if kill.exists():
    kill.unlink()
    os.kill(os.getppid(), signal.SIGKILL)
"""

STUB_SSH = r"""#!/usr/bin/env bash
cmd="${@: -1}"
case "$cmd" in
    *"test -f"*) exit 1 ;;
    *".version"*) cat "$STUB_BUNDLE_SRC/.version" ;;
    *) exit 0 ;;
esac
"""

# Remote pulls copy the fixture bundle; local copies (backup, apply, restore)
# run the real rsync. STUB_BACKUP_RC fakes the backup's exit code after a real
# copy (24 = files vanished, 23 = partial transfer); STUB_APPLY_RC does the same
# for the apply of staging over the live install, after the files went in.
STUB_RSYNC = r"""#!/usr/bin/env bash
dst="${@: -1}"
src="${@: -2:1}"
for a in "$@"; do
    case "$a" in
        *@*:*) mkdir -p "$dst"; cp -a "$STUB_BUNDLE_SRC"/. "$dst"/; exit 0 ;;
    esac
done
/usr/bin/rsync "$@"; rc=$?
case "$dst" in
    *rovimen_scripts.bak.new/) [ -n "${STUB_BACKUP_RC:-}" ] && exit "$STUB_BACKUP_RC" ;;
esac
case "$src" in
    *rovimen_scripts.staging/) [ -n "${STUB_APPLY_RC:-}" ] && exit "$STUB_APPLY_RC" ;;
esac
exit $rc
"""

# is-enabled / is-active: every service in $STUB_SERVICES is enabled and
# active. NRestarts: services listed in ~/crashloop gain one per query.
STUB_SYSTEMCTL = r"""#!/usr/bin/env bash
echo "$*" >> "$HOME/systemctl.log"
user=false
[ "$1" = --user ] && { user=true; shift; }
$user && exit 1
case "$1" in
    is-enabled|is-active)
        name="${@: -1}"
        for s in $STUB_SERVICES; do [ "$s" = "$name" ] && exit 0; done
        exit 1 ;;
    show)
        name="${@: -1}"
        f="$HOME/nrestarts_$name"
        n=$(cat "$f" 2>/dev/null || echo 0)
        echo "$n"
        grep -qx "$name" "$HOME/crashloop" 2>/dev/null && echo $((n + 1)) > "$f"
        exit 0 ;;
esac
exit 0
"""

STUB_SUDO = r"""#!/usr/bin/env bash
if [ "$1" != -n ]; then
    echo "$*" >> "$HOME/sudo_without_n.log"
    exit 1
fi
shift
echo "$*" >> "$HOME/sudo.log"
[ "$1" = systemctl ] && { shift; exec systemctl "$@"; }
exit 0
"""


def _keypair() -> tuple[str, str]:
    priv = Ed25519PrivateKey.generate()
    return (
        priv.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                           serialization.NoEncryption()).decode(),
        priv.public_key().public_bytes(serialization.Encoding.PEM,
                                       serialization.PublicFormat.SubjectPublicKeyInfo).decode(),
    )


class Station:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.priv, pub = _keypair()
        self.home = tmp_path / "home"
        self.live = self.home / "rovimen_scripts"
        self.live.mkdir(parents=True)
        (self.live / "bundle_verify.py").write_text(VERIFIER.read_text())
        (self.live / "config.json").write_text(json.dumps({"vps_host": "vps.invalid"}) + "\n")
        (self.live / "station_api.py").write_text("old station api, longer\n")
        (self.live / ".version").write_text("522.2026.11.04\n")
        # Runtime data the backup must leave out.
        (self.live / "ffthumbs" / "RO0006" / "20261109").mkdir(parents=True)
        (self.live / "ffthumbs" / "RO0006" / "20261109" / "a.jpg").write_bytes(b"jpg")
        (self.live / "logs").mkdir()
        (self.live / "logs" / "x.log").write_text("log\n")
        (self.live / "detections_index.db").write_bytes(b"db")
        (self.live / "detections_index.db-wal").write_bytes(b"")
        (self.live / "upload_state").mkdir()
        (self.live / "upload_state" / "state_json_sent.json").write_text("{}")
        (self.live / "cam_stall_state.json").write_text("{}")
        self.state = self.live / ".bundle_state.json"
        self.bundle = tmp_path / "vps-bundle"
        self.serve(526)
        self.pin = tmp_path / "pin.pem"
        self.pin.write_text(pub)
        self.mode = tmp_path / "mode"
        self.mode.write_text("enforce\n")
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        for name, body in (("ssh", STUB_SSH), ("rsync", STUB_RSYNC),
                           ("systemctl", STUB_SYSTEMCTL), ("sudo", STUB_SUDO),
                           ("pgrep", "#!/bin/sh\nexit 1\n"),
                           ("sleep", "#!/bin/sh\necho \"$*\" >> \"$HOME/sleep.log\"\n")):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)

    def serve(self, build: int) -> None:
        """Publish a signed bundle with this build number on the fake VPS."""
        version = f"{build}.2026.11.21"
        if self.bundle.exists():
            subprocess.run(["rm", "-rf", str(self.bundle)], check=True)
        self.bundle.mkdir()
        (self.bundle / "station_api.py").write_text(f"api {build}\n")
        (self.bundle / "bundle_verify.py").write_text(VERIFIER.read_text())
        (self.bundle / "config_migrate.py").write_text(FAKE_MIGRATE)
        (self.bundle / ".version").write_text(version + "\n")
        sign_bundle.sign_bundle(self.bundle, version=version, channel="main",
                                build=build, private_pem=self.priv)
        self.version = version

    def run(self, **extra: str) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if k != "SUDO_USER"}
        env.update(
            HOME=str(self.home),
            PATH=f"{self.bin}{os.pathsep}{env['PATH']}",
            STUB_BUNDLE_SRC=str(self.bundle),
            STUB_SERVICES="color-capture rovimen-station-api",
            ROVIMEN_BUNDLE_PUBKEY=str(self.pin),
            ROVIMEN_UPDATE_VERIFY_MODE_FILE=str(self.mode),
            ROVIMEN_BUNDLE_STATE=str(self.state),
            ROVIMEN_UPDATE_JITTER_MAX="0",
            ROVIMEN_UPDATE_HEALTH_WAIT="0",
        )
        env.update(extra)
        return subprocess.run(["bash", str(UPDATER), "--now"], env=env, text=True,
                              capture_output=True, stdin=subprocess.DEVNULL, timeout=120)

    # helpers
    def installed(self) -> str:
        return (self.live / ".version").read_text().strip()

    def migrations(self) -> int:
        f = self.home / "migrate_calls"
        return len(f.read_text().splitlines()) if f.exists() else 0

    def restarts(self) -> list[str]:
        f = self.home / "systemctl.log"
        return [ln for ln in f.read_text().splitlines() if ln.startswith("restart ")] \
            if f.exists() else []

    def update_record(self) -> dict:
        return json.loads(self.state.read_text()).get("update") or {}

    def write_state(self, **fields: object) -> None:
        base = {"channel": "main", "last_verify": "ok", "mode": "enforce", "failures": []}
        base.update(fields)
        self.state.write_text(json.dumps(base))


@pytest.fixture
def station(tmp_path: Path) -> Station:
    return Station(tmp_path)


def _out(r: subprocess.CompletedProcess) -> str:
    return r.stdout + r.stderr


# ── anti-rollback against the installed build ────────────────────────────────


@pytest.mark.parametrize("verified,installed", [
    (526, "522.2026.11.04"),   # gmnro05's stuck shape: verified 526, running 522
    (522, "522.2026.11.04"),   # in sync, old state format
])
def test_old_format_state_updates_and_the_stuck_shape_heals(station, verified, installed):
    station.write_state(build=verified, version=f"{verified}.2026.11.06")
    (station.live / ".version").write_text(installed + "\n")
    r = station.run()
    assert r.returncode == 0, _out(r)
    assert "rollback rejected" not in _out(r)
    assert station.installed() == station.version
    assert (station.live / "station_api.py").read_text() == "api 526\n"
    assert station.migrations() == 1
    assert station.update_record()["status"] == "ok"


def test_build_lower_than_installed_is_still_refused(station):
    (station.live / ".version").write_text("549.2026.11.19\n")
    station.write_state(build=549, version="549.2026.11.19")
    station.serve(548)
    r = station.run()
    assert r.returncode == 1
    assert "ROVIMEN_BUNDLE_REJECTED" in _out(r)
    assert "bundle build 548 < installed build 549" in _out(r)
    assert station.installed() == "549.2026.11.19"
    assert (station.live / "station_api.py").read_text() == "old station api, longer\n"
    assert station.migrations() == 0


# ── backup ───────────────────────────────────────────────────────────────────


def test_backup_failure_does_not_stop_the_update(station):
    """The gmnro05 case under the new code: backup fails, the update applies."""
    station.write_state(build=522, version="522.2026.11.04")
    # A partial copy left by an older updater that died mid-backup.
    (station.home / "rovimen_scripts.bak.new").mkdir()
    (station.home / "rovimen_scripts.bak.new" / "stale").write_text("x")
    r = station.run(STUB_BACKUP_RC="23")
    assert r.returncode == 0, _out(r)
    assert "WARN: backup failed (rsync exit 23)" in _out(r)
    assert station.installed() == station.version
    assert not (station.home / "rovimen_scripts.bak.new").exists()
    r2 = station.run()
    assert r2.returncode == 0, _out(r2)
    assert "Already up to date" in _out(r2)


# ── failed verify or apply: the same build installs on the next pass ─────────


def test_rejected_bundle_leaves_install_and_state_and_the_same_build_installs_later(station):
    """A bundle rejected in enforce mode (here a file that does not match its
    manifest) changes nothing, and does not lock its build out: once the VPS
    serves an intact copy of the same build, the next pass installs it."""
    station.write_state(build=522, version="522.2026.11.04")
    (station.bundle / "station_api.py").write_text("tampered\n")
    r = station.run()
    assert r.returncode == 1
    assert "ROVIMEN_BUNDLE_REJECTED" in _out(r)
    assert station.installed() == "522.2026.11.04"
    assert (station.live / "station_api.py").read_text() == "old station api, longer\n"
    state = json.loads(station.state.read_text())
    assert (state["build"], state["version"]) == (522, "522.2026.11.04")
    assert not (station.home / "rovimen_scripts.bak").exists()
    assert not (station.home / "rovimen_scripts.staging").exists()

    station.serve(526)
    r2 = station.run()
    assert r2.returncode == 0, _out(r2)
    assert "rollback rejected" not in _out(r2)
    assert station.installed() == station.version
    assert (station.live / "station_api.py").read_text() == "api 526\n"
    assert station.update_record()["status"] == "ok"


def test_apply_failure_restores_the_install_and_the_same_build_retries(station):
    """The apply rsync fails after the files went in: the code comes back from
    the backup, .version stays at the installed build, and the next pass in
    enforce mode installs the same build rather than refusing it as a rollback
    (the verify before the failed apply already recorded it)."""
    station.write_state(build=522, version="522.2026.11.04")
    r = station.run(STUB_APPLY_RC="23")
    assert r.returncode == 1
    assert "ERROR: apply failed — restoring the code from" in _out(r)
    assert station.installed() == "522.2026.11.04"
    assert (station.live / "station_api.py").read_text() == "old station api, longer\n"
    assert station.migrations() == 0
    assert station.restarts() == []
    rec = station.update_record()
    assert (rec["status"], rec["version"]) == ("failed", station.version)
    assert json.loads(station.state.read_text())["last_verify"] == "ok"

    r2 = station.run()
    assert r2.returncode == 0, _out(r2)
    assert "rollback rejected" not in _out(r2)
    assert station.installed() == station.version
    assert (station.live / "station_api.py").read_text() == "api 526\n"
    assert station.migrations() == 1
    assert station.update_record()["status"] == "ok"


def test_files_vanishing_during_backup_is_not_an_error(station):
    r = station.run(STUB_BACKUP_RC="24")
    assert r.returncode == 0, _out(r)
    assert "Backed up to" in _out(r)
    assert (station.home / "rovimen_scripts.bak" / "station_api.py").read_text() == "old station api, longer\n"


def test_backup_keeps_code_and_state_but_not_runtime_data(station):
    station.write_state(build=522)
    r = station.run()
    assert r.returncode == 0, _out(r)
    bak = station.home / "rovimen_scripts.bak"
    for kept in ("config.json", "station_api.py", ".version", ".bundle_state.json",
                 "cam_stall_state.json", "bundle_verify.py"):
        assert (bak / kept).exists(), kept
    for left_out in ("ffthumbs", "logs", "detections_index.db", "detections_index.db-wal",
                     "upload_state"):
        assert not (bak / left_out).exists(), left_out
    # The live runtime data is untouched by the update.
    assert (station.live / "ffthumbs" / "RO0006" / "20261109" / "a.jpg").exists()
    assert (station.live / "detections_index.db").read_bytes() == b"db"


def test_backup_keeps_one_generation(station):
    assert station.run().returncode == 0
    station.serve(527)
    assert station.run().returncode == 0
    assert (station.home / "rovimen_scripts.bak" / "station_api.py").read_text() == "api 526\n"
    assert [p.name for p in station.home.iterdir() if p.name.startswith("rovimen_scripts.bak")] \
        == ["rovimen_scripts.bak"]


# ── .version last: an interrupted update repeats itself ──────────────────────


def test_abort_after_apply_repeats_migration_and_restarts(station):
    station.write_state(build=522)
    (station.home / "kill_after_migrate").write_text("")
    r1 = station.run()
    assert r1.returncode != 0
    # Files are new, but the version is not recorded and nothing restarted.
    assert (station.live / "station_api.py").read_text() == "api 526\n"
    assert station.installed() == "522.2026.11.04"
    assert station.restarts() == []
    assert station.update_record()["status"] == "started"

    r2 = station.run()
    assert r2.returncode == 0, _out(r2)
    assert "Already up to date" not in _out(r2)
    assert "rollback rejected" not in _out(r2)
    assert station.migrations() == 2
    assert station.restarts() == ["restart color-capture", "restart rovimen-station-api"]
    assert station.installed() == station.version
    assert station.update_record()["status"] == "ok"


def test_completed_update_is_not_repeated(station):
    assert station.run().returncode == 0
    r = station.run()
    assert r.returncode == 0, _out(r)
    assert "Already up to date" in _out(r)
    assert station.migrations() == 1


def test_version_is_written_after_the_restarts(station):
    r = station.run()
    out = _out(r)
    assert out.index("Restarted rovimen-station-api") < out.index(f"Scripts updated to {station.version}")
    assert out.index("Files applied for") < out.index("Restarted color-capture")


# ── health check ─────────────────────────────────────────────────────────────


def test_crash_looping_service_is_recorded_as_unhealthy(station):
    (station.home / "crashloop").write_text("rovimen-station-api\n")
    r = station.run(ROVIMEN_UPDATE_HEALTH_WAIT="7")
    assert r.returncode == 0, _out(r)
    assert "failed health check" in _out(r)
    rec = station.update_record()
    assert rec["status"] == "unhealthy"
    assert rec["failed_services"] == ["rovimen-station-api"]
    assert rec["version"] == station.version
    # The wait went through the (stubbed) sleep, not real time.
    assert (station.home / "sleep.log").read_text().split() == ["7"]

    # Next run, services healthy again: the verdict clears on the up-to-date path.
    (station.home / "crashloop").unlink()
    r2 = station.run()
    assert "Already up to date" in _out(r2)
    assert station.update_record()["status"] == "ok"


def test_healthy_update_records_ok(station):
    r = station.run()
    assert "Health check passed" in _out(r)
    assert station.update_record() ["status"] == "ok"


# ── sudo -n ──────────────────────────────────────────────────────────────────


def test_every_sudo_call_in_a_full_run_uses_n(station):
    assert station.run().returncode == 0
    assert not (station.home / "sudo_without_n.log").exists(), \
        (station.home / "sudo_without_n.log").read_text()
    assert "systemctl restart color-capture" in (station.home / "sudo.log").read_text()


def test_no_sudo_without_n_in_the_updater_source():
    bad = []
    for n, line in enumerate(UPDATER.read_text().splitlines(), 1):
        code = line.split("#", 1)[0] if not line.lstrip().startswith("#") else ""
        if re.search(r'(log|echo) "', code):
            continue
        if re.search(r"(^|[\s;&|(])sudo\s+(?!-n\b)", code):
            bad.append(f"{n}: {line.strip()}")
    assert not bad, bad
