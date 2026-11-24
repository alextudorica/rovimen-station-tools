"""VAAPI ground rule: runtime libva driver selection, the install/update script,
and the stacker-lane 'auto' default that ships with it.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import color_capture
import config_migrate
import encoder
import rovimen_media

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "rovimen-scripts"
ENSURE = SCRIPTS / "ensure_vaapi.sh"


# ---------------------------------------------------------------------------
# encoder: libva driver selection
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_probe(monkeypatch):
    """Reset the per-process VAAPI probe caches around each test."""
    monkeypatch.setattr(rovimen_media, "_vaapi_ok", {})
    monkeypatch.setattr(rovimen_media, "_vaapi_device_cache", None)
    monkeypatch.setattr(rovimen_media, "_vaapi_driver_cache", "")
    monkeypatch.setattr(rovimen_media, "render_nodes",
                        lambda: [Path("/dev/dri/renderD128")])
    monkeypatch.delenv("LIBVA_DRIVER_NAME", raising=False)


def _ffmpeg_ok_only_with(driver: str | None):
    """Fake subprocess.run: the probe succeeds only under *driver*."""
    calls: list[str | None] = []

    def fake_run(cmd, **kwargs):
        got = (kwargs.get("env") or {}).get("LIBVA_DRIVER_NAME")
        calls.append(got)
        return subprocess.CompletedProcess(cmd, 0 if got == driver else 1)

    return fake_run, calls


class TestDriverCandidates:

    def test_auto_tries_default_then_i965(self):
        assert rovimen_media.vaapi_driver_candidates({}) == ["", "i965"]
        assert rovimen_media.vaapi_driver_candidates({"vaapi_driver": "auto"}) == ["", "i965"]
        assert rovimen_media.vaapi_driver_candidates(None) == ["", "i965"]

    def test_explicit_driver_is_used_alone(self):
        assert rovimen_media.vaapi_driver_candidates({"vaapi_driver": "iHD"}) == ["iHD"]
        assert rovimen_media.vaapi_driver_candidates({"vaapi_driver": " radeonsi "}) == ["radeonsi"]

    def test_garbage_falls_back_to_auto(self):
        assert rovimen_media.vaapi_driver_candidates({"vaapi_driver": 3}) == ["", "i965"]
        assert rovimen_media.vaapi_driver_candidates({"vaapi_driver": ""}) == ["", "i965"]


class TestFindVaapiDevice:

    def test_default_driver_works(self, fresh_probe):
        fake, calls = _ffmpeg_ok_only_with(None)
        with patch.object(encoder.subprocess, "run", side_effect=fake):
            assert encoder._find_vaapi_device({}) == "/dev/dri/renderD128"
        assert calls == [None]
        assert "LIBVA_DRIVER_NAME" not in encoder.vaapi_env({})

    def test_auto_retries_i965_when_default_fails(self, fresh_probe):
        # Haswell: libva picks iHD, which cannot init; i965 works.
        fake, calls = _ffmpeg_ok_only_with("i965")
        with patch.object(encoder.subprocess, "run", side_effect=fake):
            assert encoder._find_vaapi_device({}) == "/dev/dri/renderD128"
        assert calls == [None, "i965"]
        assert encoder.vaapi_env({})["LIBVA_DRIVER_NAME"] == "i965"

    def test_explicit_driver_only_probes_that_driver(self, fresh_probe):
        fake, calls = _ffmpeg_ok_only_with("i965")
        with patch.object(encoder.subprocess, "run", side_effect=fake):
            assert encoder._find_vaapi_device({"vaapi_driver": "iHD"}) is None
        assert calls == ["iHD"]
        assert "LIBVA_DRIVER_NAME" not in encoder.vaapi_env({})

    def test_nothing_works_caches_negative(self, fresh_probe):
        fake, calls = _ffmpeg_ok_only_with("nope")
        with patch.object(encoder.subprocess, "run", side_effect=fake):
            assert encoder._find_vaapi_device({}) is None
            assert encoder._find_vaapi_device({}) is None
        assert calls == [None, "i965"]  # probed once, then cached

    def test_no_render_node(self, fresh_probe, monkeypatch):
        monkeypatch.setattr(rovimen_media, "render_nodes", lambda: [])
        with patch.object(encoder.subprocess, "run") as run:
            assert encoder._find_vaapi_device({}) is None
        run.assert_not_called()

    def test_probe_exception_counts_as_failure(self, fresh_probe):
        with patch.object(encoder.subprocess, "run", side_effect=OSError("no ffmpeg")):
            assert encoder._find_vaapi_device({}) is None

    def test_vaapi_env_keeps_base(self, fresh_probe, monkeypatch):
        monkeypatch.setattr(rovimen_media, "_vaapi_driver_cache", "i965")
        env = encoder.vaapi_env({"PATH": "/usr/bin"})
        assert env == {"PATH": "/usr/bin", "LIBVA_DRIVER_NAME": "i965"}


# ---------------------------------------------------------------------------
# color_capture: stacker lanes 'auto'
# ---------------------------------------------------------------------------


class TestStackerWorkers:

    @pytest.mark.parametrize("cams,cores,expected", [
        (4, 6, 2),   # gmnro04: 4 cams, 6 cores
        (6, 8, 2),   # gmnro01
        (3, 8, 1),   # too few cameras
        (4, 4, 1),   # too few cores (Pi-class)
        (1, 2, 1),
    ])
    def test_auto(self, cams, cores, expected):
        assert color_capture.resolve_stacker_workers("auto", cams, cores) == expected

    def test_explicit_value_wins(self):
        assert color_capture.resolve_stacker_workers(1, 6, 12) == 1
        assert color_capture.resolve_stacker_workers(3, 1, 2) == 3
        assert color_capture.resolve_stacker_workers("2", 1, 2) == 2

    def test_invalid_values_mean_auto(self):
        assert color_capture.resolve_stacker_workers(0, 4, 6) == 2
        assert color_capture.resolve_stacker_workers(None, 4, 6) == 2
        assert color_capture.resolve_stacker_workers(True, 1, 2) == 1
        assert color_capture.resolve_stacker_workers("lots", 1, 2) == 1

    def test_uses_cpu_count_when_not_given(self, monkeypatch):
        monkeypatch.setattr(color_capture.os, "cpu_count", lambda: 6)
        assert color_capture.resolve_stacker_workers("auto", 4) == 2
        monkeypatch.setattr(color_capture.os, "cpu_count", lambda: None)
        assert color_capture.resolve_stacker_workers("auto", 4) == 1


# ---------------------------------------------------------------------------
# config defaults / migration
# ---------------------------------------------------------------------------


class TestDefaults:

    def test_defaults_declare_auto(self):
        defaults = json.loads((SCRIPTS / "config_defaults.json").read_text())
        assert defaults["vaapi_driver"] == "auto"
        assert defaults["stacker_max_concurrent"] == "auto"

    def test_migration_keeps_explicit_lane_count(self):
        defaults = json.loads((SCRIPTS / "config_defaults.json").read_text())
        merged, added = config_migrate.deep_merge({"stacker_max_concurrent": 2}, defaults)
        assert merged["stacker_max_concurrent"] == 2
        assert "stacker_max_concurrent" not in added
        assert merged["vaapi_driver"] == "auto"

    def test_top_level_vaapi_driver_is_not_purged(self):
        # capabilities.vaapi_driver is a removed legacy key; the live one is top-level.
        assert "vaapi_driver" not in config_migrate.REMOVED_KEYS


# ---------------------------------------------------------------------------
# ensure_vaapi.sh
# ---------------------------------------------------------------------------


def _write_exe(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def fake_gpu(tmp_path):
    """A fake render node (a plain rw file) + sysfs vendor + PATH with a stub ffmpeg."""
    dri = tmp_path / "dri"
    dri.mkdir()
    (dri / "renderD128").write_text("")
    sysfs = tmp_path / "sysfs" / "renderD128" / "device"
    sysfs.mkdir(parents=True)
    (sysfs / "vendor").write_text("0x8086\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    env = {
        **os.environ,
        "ROVIMEN_DRI_DIR": str(dri),
        "ROVIMEN_DRM_SYSFS": str(tmp_path / "sysfs"),
        "PATH": f"{bindir}:{os.environ.get('PATH', '')}",
        "HOME": str(tmp_path / "home"),
    }
    env.pop("LIBVA_DRIVER_NAME", None)
    return dri, bindir, env


def _run_ensure(env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(ENSURE), *args], env=env,
                          capture_output=True, text=True, timeout=60)


needs_unprivileged = pytest.mark.skipif(os.geteuid() == 0,
                                        reason="ensure_vaapi.sh refuses to run as root")


class TestEnsureVaapiScript:

    def test_syntax(self):
        subprocess.run(["bash", "-n", str(ENSURE)], check=True)
        subprocess.run(["bash", "-n", str(SCRIPTS / "updater.sh")], check=True)
        subprocess.run(["bash", "-n", str(SCRIPTS / "hardware_assessment.sh")], check=True)

    def test_sudoers_rules_escape_and_parse(self, tmp_path):
        out = subprocess.run(["bash", str(ENSURE), "--print-sudoers", "gmn"],
                             capture_output=True, text=True, check=True).stdout
        assert "-aG render\\,video gmn" in out
        assert "DPkg\\:\\:Lock\\:\\:Timeout\\=120" in out
        visudo = shutil.which("visudo") or ("/usr/sbin/visudo" if Path("/usr/sbin/visudo").exists() else None)
        if visudo:
            f = tmp_path / "rovimen"
            f.write_text(out)
            r = subprocess.run([visudo, "-c", "-q", "-f", str(f)], capture_output=True, text=True)
            assert r.returncode == 0, r.stdout + r.stderr

    def test_sudoers_rules_match_the_commands_the_script_runs(self):
        text = ENSURE.read_text()
        # The privileged calls must use exactly the arrays the rules print.
        assert '"$APT_GET" "${APT_OPTS[@]}" "${pkgs[@]}"' in text
        assert '"$USERMOD" -aG render,video "$STATION_USER"' in text

    @needs_unprivileged
    def test_not_capable_without_render_node(self, tmp_path, fake_gpu):
        _, _, env = fake_gpu
        env["ROVIMEN_DRI_DIR"] = str(tmp_path / "empty")
        r = _run_ensure(env, "--check")
        assert r.returncode == 0
        assert r.stdout.strip().splitlines()[-1] == "VAAPI: NOT-CAPABLE reason=no-render-node"

    @needs_unprivileged
    def test_pass_with_default_driver(self, fake_gpu):
        _, bindir, env = fake_gpu
        _write_exe(bindir / "ffmpeg", 'exit 0\n')
        r = _run_ensure(env, "--check")
        assert r.returncode == 0, r.stdout + r.stderr
        last = r.stdout.strip().splitlines()[-1]
        assert last.startswith("VAAPI: PASS ") and "driver=default" in last and "vendor=intel" in last

    @needs_unprivileged
    def test_pass_with_i965_fallback(self, fake_gpu):
        _, bindir, env = fake_gpu
        _write_exe(bindir / "ffmpeg", '[ "${LIBVA_DRIVER_NAME:-}" = i965 ]\n')
        r = _run_ensure(env, "--check")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "driver=i965" in r.stdout.strip().splitlines()[-1]

    @needs_unprivileged
    def test_not_capable_when_no_h264_encode_entrypoint(self, fake_gpu):
        # Q35-class: encode fails and vainfo lists no H.264 encode entrypoint.
        _, bindir, env = fake_gpu
        _write_exe(bindir / "ffmpeg", 'exit 1\n')
        _write_exe(bindir / "vainfo", 'echo "VAProfileMPEG2Main : VAEntrypointVLD"\n')
        r = _run_ensure(env, "--check")
        assert r.returncode == 0
        assert "NOT-CAPABLE" in r.stdout.strip().splitlines()[-1]

    @needs_unprivileged
    def test_fail_when_driver_can_encode_but_test_fails(self, fake_gpu):
        _, bindir, env = fake_gpu
        _write_exe(bindir / "ffmpeg", 'exit 1\n')
        _write_exe(bindir / "vainfo", 'echo "VAProfileH264Main : VAEntrypointEncSlice"\n')
        r = _run_ensure(env, "--check")
        assert r.returncode == 1
        assert r.stdout.strip().splitlines()[-1].startswith("VAAPI: FAIL ")

    @needs_unprivileged
    def test_pending_when_group_granted_but_not_active_in_this_process(self, fake_gpu):
        # 2026-09-24: gmnro12/13/18 were reported NOT-CAPABLE/FAIL right after
        # the usermod, although a fresh session encoded fine.
        dri, bindir, env = fake_gpu
        (dri / "renderD128").chmod(0)
        _write_exe(bindir / "ffmpeg", 'exit 1\n')
        _write_exe(bindir / "sg", 'exit 1\n')
        _write_exe(bindir / "vainfo", 'echo "VAProfileH264Main : VAEntrypointEncSlice"\n')
        _write_exe(bindir / "id", 'case "$1" in -nG) echo "users render";; -un) echo gmn;; -u) echo 1000;; *) /usr/bin/id "$@";; esac\n')
        r = _run_ensure(env, "--check")
        assert r.returncode == 0, r.stdout + r.stderr
        assert r.stdout.strip().splitlines()[-1] == "VAAPI: PENDING reason=render-group-granted-verify-next-pass"

    @needs_unprivileged
    def test_check_mode_writes_no_status_file(self, fake_gpu):
        _, bindir, env = fake_gpu
        _write_exe(bindir / "ffmpeg", 'exit 0\n')
        _run_ensure(env, "--check")
        assert not (Path(env["HOME"]) / "rovimen_scripts" / "vaapi_status.json").exists()


# ---------------------------------------------------------------------------
# updater wiring
# ---------------------------------------------------------------------------


class TestUpdaterWiring:

    def test_ensure_step_is_gated_on_the_full_night_policy_marker(self):
        text = (SCRIPTS / "updater.sh").read_text()
        assert 'VAAPI_POLICY_MARKER="FULL_NIGHT_DEADLINE_POLICY"' in text
        body = text.split("_ensure_vaapi_ground_rule() {", 1)[1].split("\n}\n", 1)[0]
        assert "dawn_process.py" in body and "return 0" in body
        assert "--non-interactive" in body

    def test_ensure_step_runs_before_service_restarts(self):
        text = (SCRIPTS / "updater.sh").read_text()
        main = text.split("# Reconcile sudoers — keep NOPASSWD rules current", 1)[1]
        assert main.index("_ensure_vaapi_ground_rule") < main.index("Restart running services")

    def test_up_to_date_path_refreshes_sudoers_before_the_vaapi_step(self):
        text = (SCRIPTS / "updater.sh").read_text()
        block = text.split('if [ "$LOCAL_VERSION" = "$REMOTE_VERSION" ]; then', 1)[1].split("exit 0", 1)[0]
        assert block.index("_ensure_sudoers") < block.index("_ensure_vaapi_ground_rule")

    def test_manual_sudo_install_writes_rules_for_the_station_user(self):
        text = (SCRIPTS / "updater.sh").read_text()
        # Root (`sudo updater.sh`) -> the invoking operator; `sudo -u rmsuser` ->
        # rmsuser, not the admin who ran sudo (#1268, gmnro08 2026-09-25).
        body = text.split("_ensure_sudoers() {", 1)[1].split("\n}\n", 1)[0]
        assert '[ "$(id -u)" -eq 0 ] && [ -n "${SUDO_USER:-}" ]' in body
        assert "U=$SUDO_USER" in body and "U=$(whoami)" in body
        assert 'LOCAL_DIR="$STATION_HOME/rovimen_scripts"' in text

    def test_sudoers_carries_vaapi_rules(self):
        assert "ensure_vaapi.sh\" --print-sudoers" in (SCRIPTS / "updater.sh").read_text()
