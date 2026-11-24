"""Tests for the station API service-state probe (/api/status `services`).

The false alarm this pins down: a station that runs its RMS capture from a
per-user systemd unit (~/.config/systemd/user/rms-capture-cam2.service, cwd
~/RMS_cam2, argv `python -m RMS.StartCapture`) was reported `rms-cam2:
inactive` all night while both cameras were capturing — the unit lookup only
ever searched the system scope under a name that station never used, and the
process fallback only matched an `RMS_cam2` token in argv, which a
cwd-configured RMS does not carry.

The probe must stay honest in the other direction too: a camera with no active
unit *and* no capture process still reports inactive, and the watchdog still
alarms on it.
"""

from __future__ import annotations

from unittest.mock import patch

import station_api
import station_collect
import station_watchdog


# Real `systemctl list-units --type=service --all --no-legend --plain` and
# `systemctl list-unit-files --type=service --no-legend` rows, system scope:
# the pipeline units were left disabled there when the station moved to
# per-user units.
SYSTEM_UNITS = [
    "color-capture.service      loaded inactive dead    Color video capture for meteor clips",
    "detection-indexer.service  loaded active   running ROVIMEN Detection Metadata Indexer",
    "rovimen-station-api.service loaded active  running ROVIMEN Station API",
]
SYSTEM_UNIT_FILES = [
    "color-capture.service       disabled enabled",
    "detection-indexer.service   enabled  enabled",
    "rovimen-station-api.service disabled enabled",
]
# ... and the user scope, where the capture units actually live.
USER_UNITS = [
    "color-capture.service     loaded active running Color video capture for meteor clips",
    "rms-capture.service       loaded active running RMS Meteor Capture - RO000R",
    "rms-capture-cam2.service  loaded active running RMS Meteor Capture - RO000P",
]
USER_UNIT_FILES = [
    "color-capture.service    enabled enabled",
    "rms-capture.service      enabled enabled",
    "rms-capture-cam2.service enabled enabled",
]


def _fake_systemctl(system_units=(), system_files=(), user_units=(), user_files=()):
    """Stand in for _systemctl_lines, keyed on scope + subcommand."""
    def _lines(user_scope: bool, args: list[str]) -> list[str]:
        if args[0] == "list-units":
            return list(user_units if user_scope else system_units)
        return list(user_files if user_scope else system_files)
    return _lines


# ── Capture-process attribution ───────────────────────────────────────────


class TestCaptureMarker:
    """An RMS instance is identified by its argv *or* its working directory —
    RMS reads its .config from the cwd, so `python -m RMS.StartCapture`
    started in ~/RMS_cam2 has no RMS_cam2 token to match on."""

    def test_marker_from_argv(self):
        assert station_api._capture_marker(
            "python -m RMS.StartCapture -c /home/gmn/RMS_cam3/.config", "/home/gmn"
        ) == "RMS_cam3"

    def test_marker_from_cwd(self):
        assert station_api._capture_marker(
            "/home/rmsuser/vRMS/bin/python -m RMS.StartCapture", "/home/rmsuser/RMS_cam2"
        ) == "RMS_cam2"

    def test_non_numbered_layout_has_no_marker(self):
        assert station_api._capture_marker(
            "/home/rmsuser/vRMS/bin/python -m RMS.StartCapture", "/home/rmsuser/source/RMS"
        ) == ""

    def test_station_code_layout_has_no_marker(self):
        assert station_api._capture_marker(
            "python -m RMS.StartCapture", "/home/gmn/source/Stations/RO001E"
        ) == ""


class TestCaptureProcessMarkers:
    def test_markers_use_cwd_and_skip_non_capture_lines(self):
        pgrep_out = (
            "1398 /home/rmsuser/vRMS/bin/python -m RMS.StartCapture\n"
            "1402 /home/rmsuser/vRMS/bin/python -m RMS.StartCapture\n"
            "6001 bash -c grep StartCapture ~/logs/rms.log\n"
        )
        cwds = {"1398": "/home/rmsuser/RMS_cam2", "1402": "/home/rmsuser/source/RMS"}
        with patch.object(station_api.subprocess, "run") as run, \
                patch.object(station_collect, "_proc_cwd", side_effect=lambda pid: cwds.get(pid, "")):
            run.return_value.stdout = pgrep_out
            markers = station_api._capture_process_markers()
        # The shell line merely mentions StartCapture — it is not an instance.
        assert markers == ["RMS_cam2", ""]

    def test_no_capture_processes(self):
        with patch.object(station_api.subprocess, "run") as run:
            run.return_value.stdout = ""
            assert station_api._capture_process_markers() == []


class TestRmsServiceRunning:
    def test_numbered_service_matches_its_own_instance(self):
        markers = ["RMS_cam2", ""]
        assert station_api._rms_service_running("rms-cam2", markers, True) is True
        assert station_api._rms_service_running("rms-cam1", markers, True) is False

    def test_catch_all_claims_only_unattributed_instances(self):
        """With per-camera keys present, `rms-capture` stands for the instance
        running from a non-numbered layout. A healthy numbered camera must not
        keep it alive — otherwise one live camera masks its dead sibling."""
        assert station_api._rms_service_running("rms-capture", ["RMS_cam2", ""], True) is True
        assert station_api._rms_service_running("rms-capture", ["RMS_cam2"], True) is False

    def test_catch_all_matches_any_instance_when_alone(self):
        assert station_api._rms_service_running("rms-capture", ["RMS_cam2"], False) is True
        assert station_api._rms_service_running("rms-capture", [], False) is False


# ── systemd inventory ─────────────────────────────────────────────────────


class TestUnitInventory:
    def test_both_scopes_merged_best_state_wins(self):
        with patch.object(station_collect, "_systemctl_lines", _fake_systemctl(
                SYSTEM_UNITS, SYSTEM_UNIT_FILES, USER_UNITS, USER_UNIT_FILES)):
            inv = station_api._unit_inventory()
        # System scope says disabled/inactive, user scope says enabled/active.
        assert inv["color-capture"].active == "active"
        assert inv["color-capture"].enabled == "enabled"
        # Units that exist in one scope only are still found.
        assert inv["rms-capture-cam2"].active == "active"
        assert inv["detection-indexer"].active == "active"
        assert "rms-cam2" not in inv

    def test_unreachable_user_scope_leaves_system_units_intact(self):
        with patch.object(station_collect, "_systemctl_lines",
                          _fake_systemctl(SYSTEM_UNITS, SYSTEM_UNIT_FILES)):
            inv = station_api._unit_inventory()
        assert inv["detection-indexer"].active == "active"
        assert inv["color-capture"].enabled == "disabled"

    def test_malformed_rows_ignored(self):
        with patch.object(station_collect, "_systemctl_lines",
                          _fake_systemctl(["", "not-a-unit", "x.mount loaded active mounted /x"],
                                          ["short"])):
            assert station_api._unit_inventory() == {}

    def test_user_scope_gets_a_runtime_dir(self):
        """The API runs as a system unit (User=<station>) and inherits no
        XDG_RUNTIME_DIR, without which `systemctl --user` cannot reach the bus
        and every per-user unit looks absent."""
        with patch.object(station_api.subprocess, "run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = ""
            station_api._systemctl_lines(True, ["list-units"])
            env = run.call_args.kwargs["env"]
            assert env["XDG_RUNTIME_DIR"].startswith("/run/user/")
            assert "--user" in run.call_args.args[0]

    def test_system_scope_inherits_environment(self):
        with patch.object(station_api.subprocess, "run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = ""
            station_api._systemctl_lines(False, ["list-units"])
            assert run.call_args.kwargs["env"] is None

    def test_failing_scope_returns_no_rows(self):
        with patch.object(station_api.subprocess, "run") as run:
            run.return_value.returncode = 1
            run.return_value.stdout = "Failed to connect to bus\n"
            assert station_api._systemctl_lines(True, ["list-units"]) == []


class TestUnitCandidates:
    def test_rms_camera_unit_name_families(self):
        cands = station_api._service_unit_candidates("rms-cam2")
        assert cands[0] == "rms-cam2"
        assert "rms-capture-cam2" in cands
        assert "rms-capture" not in cands

    def test_first_camera_also_matches_the_plain_unit(self):
        assert "rms-capture" in station_api._service_unit_candidates("rms-cam1")

    def test_non_rms_service_keeps_its_own_name(self):
        assert station_api._service_unit_candidates("color-capture") == ["color-capture"]

    def test_resolve_unit_follows_the_alias(self):
        with patch.object(station_collect, "_systemctl_lines", _fake_systemctl(
                SYSTEM_UNITS, SYSTEM_UNIT_FILES, USER_UNITS, USER_UNIT_FILES)):
            inv = station_api._unit_inventory()
        assert station_api._resolve_unit("rms-cam2", inv).active == "active"
        assert station_api._resolve_unit("rms-cam7", inv) == station_api._UnitInfo()


# ── State decision ────────────────────────────────────────────────────────


class TestServiceState:
    def test_active_unit_is_active(self):
        info = station_api._UnitInfo(active="active", enabled="enabled")
        assert station_api._service_state(info, False) == station_api.SVC_ACTIVE

    def test_restart_gap_is_not_a_failure(self):
        info = station_api._UnitInfo(active="activating", enabled="enabled")
        assert station_api._service_state(info, False) == station_api.SVC_ACTIVE

    def test_running_without_a_unit_is_unmanaged_not_dead(self):
        assert station_api._service_state(station_api._UnitInfo(), True) == \
            station_api.SVC_UNMANAGED

    def test_running_under_an_inactive_unit_is_unmanaged(self):
        info = station_api._UnitInfo(active="inactive", enabled="enabled")
        assert station_api._service_state(info, True) == station_api.SVC_UNMANAGED

    def test_dead_unit_with_no_process_is_inactive(self):
        info = station_api._UnitInfo(active="inactive", enabled="enabled")
        assert station_api._service_state(info, False) == station_api.SVC_INACTIVE

    def test_nothing_anywhere_is_inactive(self):
        assert station_api._service_state(station_api._UnitInfo(), False) == \
            station_api.SVC_INACTIVE

    def test_failed_unit_reported_as_failed(self):
        info = station_api._UnitInfo(active="failed", enabled="enabled")
        assert station_api._service_state(info, False) == station_api.SVC_FAILED

    def test_deliberately_disabled_service_is_not_reported(self):
        info = station_api._UnitInfo(active="inactive", enabled="disabled")
        assert station_api._service_state(info, False) is None
        assert station_api._service_state(
            station_api._UnitInfo(active="inactive", enabled="masked"), False) is None

    def test_disabled_unit_with_a_live_process_is_still_reported(self):
        info = station_api._UnitInfo(active="inactive", enabled="disabled")
        assert station_api._service_state(info, True) == station_api.SVC_UNMANAGED


# ── RMS instance enumeration ──────────────────────────────────────────────


class TestRmsServiceNames:
    def test_numbered_directories_enumerated(self, tmp_path):
        (tmp_path / "RMS_cam1").mkdir()
        (tmp_path / "RMS_cam2").mkdir()
        assert station_api._rms_service_names(tmp_path, {}, []) == ["rms-cam1", "rms-cam2"]

    def test_mixed_layout_reports_the_source_rms_instance_too(self, tmp_path):
        """~/RMS_cam2 for the second camera, ~/source/RMS for the first: the
        directory scan alone never enumerated the first camera, so its death
        could not raise an alarm. Its unit file supplies the key."""
        (tmp_path / "RMS_cam2").mkdir()
        inv = {"rms-capture": station_api._UnitInfo(active="active", enabled="enabled")}
        assert station_api._rms_service_names(tmp_path, inv, ["RMS_cam2", ""]) == \
            ["rms-capture", "rms-cam2"]

    def test_no_duplicate_catch_all_when_cam1_exists(self, tmp_path):
        (tmp_path / "RMS_cam1").mkdir()
        inv = {"rms-capture": station_api._UnitInfo(active="active", enabled="enabled")}
        assert station_api._rms_service_names(tmp_path, inv, [""]) == ["rms-cam1"]

    def test_single_instance_station_from_processes_only(self, tmp_path):
        assert station_api._rms_service_names(tmp_path, {}, [""]) == ["rms-capture"]

    def test_nothing_running_and_no_units_reports_nothing(self, tmp_path):
        assert station_api._rms_service_names(tmp_path, {}, []) == []


# ── End to end ────────────────────────────────────────────────────────────


class TestCollectServiceStates:
    def test_per_user_units_no_longer_false_alarm(self, tmp_path):
        """The reported regression: both cameras capturing under per-user
        units, dashboard shows the second one dead."""
        (tmp_path / "RMS_cam2").mkdir()
        with patch.object(station_collect, "_systemctl_lines", _fake_systemctl(
                SYSTEM_UNITS, SYSTEM_UNIT_FILES, USER_UNITS, USER_UNIT_FILES)), \
                patch.object(station_collect, "_capture_process_markers",
                             return_value=["RMS_cam2", ""]), \
                patch.object(station_collect, "_process_running", return_value=True):
            states = station_api._collect_service_states(tmp_path)
        assert states["rms-cam2"] == station_api.SVC_ACTIVE
        assert states["rms-capture"] == station_api.SVC_ACTIVE
        # The colour pipeline lives in the user scope too — it used to be
        # dropped from the report entirely because the system scope said
        # "disabled".
        assert states["color-capture"] == station_api.SVC_ACTIVE

    def test_dead_camera_still_alarms(self, tmp_path):
        """Second camera's unit gone and no capture process for it: inactive,
        even though the first camera is fine."""
        (tmp_path / "RMS_cam2").mkdir()
        with patch.object(station_collect, "_systemctl_lines", _fake_systemctl(
                SYSTEM_UNITS, SYSTEM_UNIT_FILES,
                [u for u in USER_UNITS if "cam2" not in u],
                [f for f in USER_UNIT_FILES if "cam2" not in f])), \
                patch.object(station_collect, "_capture_process_markers", return_value=[""]), \
                patch.object(station_collect, "_process_running", return_value=True):
            states = station_api._collect_service_states(tmp_path)
        assert states["rms-cam2"] == station_api.SVC_INACTIVE
        assert states["rms-capture"] == station_api.SVC_ACTIVE

    def test_unmanaged_capture_reported_as_active_with_a_note(self, tmp_path):
        """Legacy nohup/cron station: no capture unit anywhere, RMS running."""
        (tmp_path / "RMS_cam1").mkdir()
        with patch.object(station_collect, "_systemctl_lines",
                          _fake_systemctl(SYSTEM_UNITS, SYSTEM_UNIT_FILES)), \
                patch.object(station_collect, "_capture_process_markers",
                             return_value=["RMS_cam1"]), \
                patch.object(station_collect, "_process_running", return_value=True):
            states = station_api._collect_service_states(tmp_path)
        assert states["rms-cam1"] == station_api.SVC_UNMANAGED


# ── Watchdog consumption ──────────────────────────────────────────────────


class TestWatchdogServiceStates:
    """The alerting side must read the new state the same way, or the fix only
    silences the UI while Discord keeps paging."""

    def test_up_states(self):
        assert station_watchdog.service_state_is_up("active") is True
        assert station_watchdog.service_state_is_up("active (unmanaged)") is True

    def test_down_states(self):
        assert station_watchdog.service_state_is_up("inactive") is False
        assert station_watchdog.service_state_is_up("failed") is False
        assert station_watchdog.service_state_is_up("") is False

    def test_legacy_boolean_payloads(self):
        assert station_watchdog.service_state_is_up(True) is True
        assert station_watchdog.service_state_is_up(False) is False
        assert station_watchdog.service_state_is_up(None) is False
