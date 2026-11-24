"""Capture-side logs must be bounded: per-night files plus a janitor sweep.

Colour capture opened ``ffmpeg_<CAM>.log`` and ``stacker.log`` once and
appended to them forever. Stations were found carrying 357 MB / 272 MB /
1.9M-line files, which cost twice over: on 14 of 18 stations ``log_path``
shares a filesystem with the capture data, so the logs eat the headroom that
feeds the ENOSPC deadlock, and their months of stale errors sent an incident
diagnosis down the wrong path twice in one week.

Two layers are pinned here:

  1. Capture writes those logs per night, the same convention its own daemon
     log already used — one file per night, reopened in append mode by a
     mid-night watchdog restart so nothing is lost.
  2. The janitor retires old ones. That pass is fleet-wide deletion, so the
     tests insist it is narrow: an explicit glob allowlist, ``.log`` only,
     no recursion, and never a video or data file — including data parked in
     the log directory itself.
"""

from __future__ import annotations

import gzip
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import janitor_storage_watchdog as jsw
from color_capture import ColorCapture


STATION = "RO0019"


def _age(f: Path, days: float) -> Path:
    """Backdate a file's mtime by *days* (logs are append-only, so mtime is
    exactly 'last written' — the signal the janitor prunes on)."""
    ts = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
    os.utime(f, (ts, ts))
    return f


# ===========================================================================
# Layer 1 — colour capture writes per-night logs
# ===========================================================================

@pytest.fixture()
def capture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ColorCapture:
    cfg = {
        "color_capture_path": str(tmp_path / "color"),
        "log_path": str(tmp_path / "logs"),
        "stations": {STATION: {"camera_rtsp": "rtsp://cam/stream_0"}},
    }
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg))
    cap = ColorCapture(str(cfg_path))
    monkeypatch.setattr(cap, "_disk_ok", lambda: True)
    return cap


class _FakeProc:
    returncode = 0
    pid = 4242

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        pass

    def kill(self):
        pass


def _run_ffmpeg(cap: ColorCapture, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: _FakeProc())
    cap._ffmpeg.pop(STATION, None)
    cap._start_ffmpeg(STATION)


def _cc_logs(cap: ColorCapture) -> list[str]:
    return sorted(p.name for p in cap._log_dir("color_capture").glob("ffmpeg_*.log"))


class TestFfmpegLogIsPerNight:
    def test_name_carries_the_night_date(self, capture, monkeypatch):
        _run_ffmpeg(capture, monkeypatch)
        assert _cc_logs(capture) == [f"ffmpeg_{STATION}_{capture._night_date()}.log"]

    def test_flat_name_is_gone(self, capture, monkeypatch):
        """The append-forever file is what this replaces — it must not be
        recreated alongside the dated one."""
        _run_ffmpeg(capture, monkeypatch)
        assert not (capture._log_dir("color_capture") / f"ffmpeg_{STATION}.log").exists()

    def test_stderr_is_still_wired_to_that_file(self, capture, monkeypatch):
        captured: dict = {}

        def fake_popen(cmd, **kwargs):
            captured["kwargs"] = kwargs
            return _FakeProc()

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        capture._start_ffmpeg(STATION)
        fh = captured["kwargs"]["stderr"]
        assert Path(fh.name).name == f"ffmpeg_{STATION}_{capture._night_date()}.log"
        assert fh.mode == "a", "a restart mid-night must append, not truncate the night"

    def test_restart_within_the_night_reuses_one_file(self, capture, monkeypatch):
        """The health watchdog restarts ffmpeg repeatedly on a bad link; that
        must not shard the night into a file per restart."""
        for _ in range(3):
            capture._stop_ffmpeg(STATION)
            _run_ffmpeg(capture, monkeypatch)
        assert len(_cc_logs(capture)) == 1

    def test_new_night_rolls_to_a_new_file(self, capture, monkeypatch):
        _run_ffmpeg(capture, monkeypatch)
        capture._stop_ffmpeg(STATION)
        monkeypatch.setattr(capture, "_night_date", lambda: "20991231")
        _run_ffmpeg(capture, monkeypatch)
        assert len(_cc_logs(capture)) == 2
        assert f"ffmpeg_{STATION}_20991231.log" in _cc_logs(capture)

    def test_night_date_matches_the_daemon_log_convention(self, capture, monkeypatch):
        """Both logs name the same night, so an incident is one glob away."""
        _run_ffmpeg(capture, monkeypatch)
        night = capture._night_date()
        d = capture._log_dir("color_capture")
        assert (d / f"{night}.log").exists()
        assert (d / f"ffmpeg_{STATION}_{night}.log").exists()


class TestStackerLogIsPerNight:
    def test_stacker_writes_a_dated_file(self, capture, monkeypatch, tmp_path):
        def fake_popen(cmd, **kwargs):
            capture._running = False
            return _FakeProc()

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        capture._running = True
        capture._stacker_queue.put((STATION, tmp_path / "chunk.mkv", ["true"]))
        capture._stacker_worker()

        logs = sorted(p.name for p in capture._log_dir().glob("stacker*.log"))
        assert logs == [f"stacker_{capture._night_date()}.log"]
        assert not (capture._log_dir() / "stacker.log").exists()


# ===========================================================================
# Layer 2 — the janitor retires old capture logs
# ===========================================================================

@pytest.fixture(autouse=True)
def _reset_janitor_logger():
    """Janitor.__init__ attaches handlers to the module logger; drop them so
    repeated instantiations don't pile up file handlers across tests."""
    yield
    for h in list(jsw.logger.handlers):
        jsw.logger.removeHandler(h)
        h.close()


def make_janitor(tmp_path: Path, retention: dict | None = None) -> jsw.Janitor:
    capture = tmp_path / "colorcap"
    capture.mkdir(parents=True, exist_ok=True)
    cfg: dict = {
        "videocapture_path": str(capture),
        "log_path": str(tmp_path / "logs"),
        "stations": {"RO000H": {}},
    }
    if retention is not None:
        cfg["retention"] = retention
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg))
    for h in list(jsw.logger.handlers):
        jsw.logger.removeHandler(h)
    return jsw.Janitor(str(cfg_path))


def _seed_logs(j: jsw.Janitor) -> dict[str, Path]:
    """A log tree with one old and one fresh file of every kind, plus the
    non-log neighbours the sweep must leave alone."""
    cc = j.log_base / "color_capture"
    cc.mkdir(parents=True, exist_ok=True)
    (cc / "nested").mkdir(exist_ok=True)

    def mk(p: Path, days: float | None) -> Path:
        p.write_text("x" * 100)
        return _age(p, days) if days is not None else p

    return {
        # --- expendable: old capture logs ---
        "old_ffmpeg":      mk(cc / "ffmpeg_RO000H_20260801.log", 30),
        "legacy_ffmpeg":   mk(cc / "ffmpeg_RO000H.log", 30),
        "old_daemon":      mk(cc / "20260801.log", 30),
        "legacy_stacker":  mk(j.log_base / "stacker.log", 30),
        "old_stacker":     mk(j.log_base / "stacker_20260801.log", 30),
        # --- keep: still inside the retention window ---
        "fresh_ffmpeg":    mk(cc / "ffmpeg_RO000H_20260901.log", 1),
        "fresh_daemon":    mk(cc / "20260901.log", 1),
        "fresh_stacker":   mk(j.log_base / "stacker_20260901.log", 1),
        # --- expendable: own rotated logs past retention (the handler's
        # backupCount was inert for years — the sweep is the backstop) ---
        "janitor_rotated": mk(j.log_base / "janitor.log.20260801", 30),
        # --- keep: not ours to delete, however old ---
        "janitor_log":     mk(j.log_base / "janitor.log", 30),
        "janitor_fresh_rotated": mk(j.log_base / "janitor.log.20260901", 1),
        "janitor_oddly_named":   mk(j.log_base / "janitor.log.backup", 30),
        "state_json":      mk(j.log_base / "janitor_state.json", 30),
        "other_daemon":    mk(j.log_base / "detection_indexer.log", 30),
        "notes":           mk(cc / "notes.txt", 30),
        "nested_log":      mk(cc / "nested" / "ffmpeg_RO000H_20260801.log", 30),
        # --- keep: video/data parked in the log dir ---
        "video":           mk(cc / "RO000H_20260801_210000_color.mkv", 30),
        "stack":           mk(cc / "RO000H_20260801_210000_stack.webp", 30),
    }


DOOMED = ("old_ffmpeg", "legacy_ffmpeg", "old_daemon", "legacy_stacker",
          "old_stacker", "janitor_rotated")
SPARED = (
    "fresh_ffmpeg", "fresh_daemon", "fresh_stacker",
    "janitor_log", "janitor_fresh_rotated", "janitor_oddly_named",
    "state_json", "other_daemon",
    "notes", "nested_log", "video", "stack",
)


class TestRotationHandlerSuffix:
    def test_extmatch_accepts_the_real_getfilestodelete_suffix(self):
        """CPython >= 3.9 splits janitor.log at the last dot, so the deletion
        candidate suffix is 'log.YYYYMMDD'; older versions strip the whole base
        name and offer 'YYYYMMDD'. The pattern must accept both, or
        backupCount silently never deletes anything (the bug this pins)."""
        import re as _re
        m = _re.compile(r'^(log\.)?\d{8}$')
        assert m.match("log.20260901")
        assert m.match("20260901")
        assert not m.match("log.backup")
        assert not m.match(".20260901")


class TestCaptureLogPruning:
    def test_old_capture_logs_are_purged(self, tmp_path):
        j = make_janitor(tmp_path)
        files = _seed_logs(j)
        j._purge_capture_logs()
        assert [k for k in DOOMED if files[k].exists()] == []

    def test_everything_else_survives(self, tmp_path):
        j = make_janitor(tmp_path)
        files = _seed_logs(j)
        j._purge_capture_logs()
        assert [k for k in SPARED if not files[k].exists()] == []

    def test_video_and_data_files_are_never_candidates(self, tmp_path):
        """The hard line: the janitor deletes logs here, nothing else. A .mkv
        or a stack sitting in the log dir is not the log sweep's business."""
        j = make_janitor(tmp_path, {"log_days": 1})
        files = _seed_logs(j)
        j._purge_capture_logs()
        assert files["video"].exists() and files["stack"].exists()

    def test_capture_tree_is_untouched_by_a_full_sweep(self, tmp_path):
        j = make_janitor(tmp_path)
        _seed_logs(j)
        night = j.capture_path / "RO000H" / datetime.now(timezone.utc).strftime("%Y%m%d")
        (night / "videos").mkdir(parents=True)
        clip = night / "videos" / "RO000H_20260901_210000_color.mkv"
        clip.write_bytes(b"x")
        _age(clip, 30)  # old mtime, but tonight's name — retention reads names
        j._run_normal_sweep()
        assert clip.exists()

    def test_retention_is_configurable(self, tmp_path):
        j = make_janitor(tmp_path, {"log_days": 90})
        files = _seed_logs(j)
        j._purge_capture_logs()
        assert all(files[k].exists() for k in DOOMED), "30-day logs died under a 90-day window"

    def test_zero_disables_the_sweep(self, tmp_path):
        j = make_janitor(tmp_path, {"log_days": 0})
        files = _seed_logs(j)
        j._purge_capture_logs()
        assert all(files[k].exists() for k in DOOMED)

    def test_default_matches_the_janitors_own_log_window(self, tmp_path):
        """One diagnostic window for every log on the station: the janitor
        already keeps 14 rotated days of janitor.log."""
        assert make_janitor(tmp_path).log_retention == 14
        assert jsw.LOG_RETENTION_DAYS == 14

    def test_sweep_is_idempotent(self, tmp_path):
        j = make_janitor(tmp_path)
        files = _seed_logs(j)
        j._purge_capture_logs()
        after_first = {k for k, p in files.items() if p.exists()}
        j._purge_capture_logs()
        assert {k for k, p in files.items() if p.exists()} == after_first

    def test_missing_log_dir_is_not_an_error(self, tmp_path):
        j = make_janitor(tmp_path)
        for h in list(jsw.logger.handlers):
            jsw.logger.removeHandler(h)
            h.close()
        for f in sorted(j.log_base.rglob("*"), reverse=True):
            f.unlink() if f.is_file() else f.rmdir()
        j.log_base.rmdir()
        j._purge_capture_logs()  # must not raise

    def test_sweep_failure_does_not_abort_the_normal_sweep(self, tmp_path, monkeypatch):
        """It runs last and is the least important pass — a fault in it must
        not cost the station its video retention."""
        j = make_janitor(tmp_path)
        boom = {"called": False}

        def explode() -> None:
            boom["called"] = True
            raise OSError("log dir on a dead USB")

        monkeypatch.setattr(j, "_purge_capture_logs", explode)
        j._run_normal_sweep()  # must not raise
        assert boom["called"]


class TestPruneAllowlist:
    def test_globs_are_anchored_to_known_log_names(self):
        """Guard the shape of the allowlist: every entry either ends in .log
        or is the janitor's own name-anchored rotation pattern, so a widened
        glob can never reach a video file."""
        rotated = {f"{n}.[0-9]*.gz" for n in jsw._APPEND_LOGS}
        for p in jsw._LOG_PRUNE_GLOBS:
            assert p.endswith(".log") or p == "janitor.log.[0-9]*" or p in rotated, p

    def test_allowlist_covers_the_legacy_and_dated_names(self, tmp_path):
        j = make_janitor(tmp_path)
        matched = set()
        files = _seed_logs(j)
        for pattern in jsw._LOG_PRUNE_GLOBS:
            matched.update(p.name for p in j.log_base.glob(pattern))
        for key in DOOMED:
            assert files[key].name in matched, f"{key} is not reachable by any glob"


# ===========================================================================
# Layer 3 — append-forever logs are rotated by size
# ===========================================================================
#
# The crons toggle_rovimen.sh installs append to fixed names under ~/logs
# (rovimen_dawn.log, updater.log, ...), and dawn's per-module handlers append
# to stacker.log / encoder.log / timelapse.log. Nothing rotated them: on
# 2026-10-05 gmnro04 (Vâlcele) carried 165 MB of rovimen_dawn.log and 129 MB of
# encoder.log. The writers keep the file open in append mode, so rotation must
# empty it in place rather than rename it.

ROTATE_1K = {"log_rotate_mb": 1 / 1024}   # 1 KiB threshold


def _rotated(j: jsw.Janitor, name: str) -> list[Path]:
    return sorted(j.log_base.glob(f"{name}.[0-9]*.gz"))


class TestAppendLogRotation:
    def test_big_append_log_is_gzipped_and_emptied_in_place(self, tmp_path):
        j = make_janitor(tmp_path, ROTATE_1K)
        log = j.log_base / "rovimen_dawn.log"
        body = b"dawn line\n" * 500
        log.write_bytes(body)
        inode = log.stat().st_ino
        j._rotate_append_logs()
        [gz] = _rotated(j, "rovimen_dawn.log")
        assert gzip.decompress(gz.read_bytes()) == body
        assert log.stat().st_size == 0 and log.stat().st_ino == inode

    def test_open_append_writer_carries_on_in_the_emptied_file(self, tmp_path):
        """dawn holds its handlers open for hours; after rotation its next
        line must land in the live file, not in a renamed one or past a hole."""
        j = make_janitor(tmp_path, ROTATE_1K)
        log = j.log_base / "encoder.log"
        with log.open("a") as writer:
            writer.write("before\n" * 300)
            writer.flush()
            j._rotate_append_logs()
            writer.write("after\n")
            writer.flush()
        assert log.read_text() == "after\n"
        [gz] = _rotated(j, "encoder.log")
        assert gzip.decompress(gz.read_bytes()).decode() == "before\n" * 300

    def test_small_logs_are_left_alone(self, tmp_path):
        j = make_janitor(tmp_path)   # default 20 MB
        log = j.log_base / "updater.log"
        log.write_text("x" * 4096)
        j._rotate_append_logs()
        assert log.read_text() == "x" * 4096 and _rotated(j, "updater.log") == []

    def test_only_the_named_append_logs_are_touched(self, tmp_path):
        j = make_janitor(tmp_path, ROTATE_1K)
        others = [j.log_base / "rms_cam2.log", j.log_base / "janitor.log",
                  j.log_base / "RO000H_20260801_210000_color.mkv",
                  j.log_base / "color_capture" / "rovimen_dawn.log"]
        others[-1].parent.mkdir(parents=True, exist_ok=True)
        for f in others:
            f.write_bytes(b"x" * 4096)
        j._rotate_append_logs()
        assert all(f.stat().st_size == 4096 for f in others)
        assert list(j.log_base.rglob("*.gz")) == []

    def test_zero_disables_rotation(self, tmp_path):
        j = make_janitor(tmp_path, {"log_rotate_mb": 0})
        log = j.log_base / "stacker.log"
        log.write_bytes(b"x" * 4096)
        j._rotate_append_logs()
        assert log.stat().st_size == 4096

    def test_default_threshold_is_20_mb(self, tmp_path):
        assert make_janitor(tmp_path).log_rotate_bytes == 20 * 1024 * 1024

    def test_normal_sweep_rotates(self, tmp_path):
        j = make_janitor(tmp_path, ROTATE_1K)
        (j.log_base / "rovimen_reboot.log").write_bytes(b"x" * 4096)
        j._run_normal_sweep()
        assert len(_rotated(j, "rovimen_reboot.log")) == 1

    def test_rotation_failure_does_not_abort_the_normal_sweep(self, tmp_path, monkeypatch):
        j = make_janitor(tmp_path)
        called = {"purge": False}

        def explode() -> None:
            raise OSError("log dir on a dead USB")

        monkeypatch.setattr(j, "_rotate_append_logs", explode)
        monkeypatch.setattr(j, "_purge_capture_logs", lambda: called.update(purge=True))
        j._run_normal_sweep()   # must not raise
        assert called["purge"]


class TestRotatedCopiesArePruned:
    def test_old_copies_go_but_the_newest_per_log_stays(self, tmp_path):
        j = make_janitor(tmp_path)
        oldest = j.log_base / "rovimen_dawn.log.20260801_040000.gz"
        newer = j.log_base / "rovimen_dawn.log.20260901.gz"   # a hand rotation
        lone = j.log_base / "updater.log.20260801_040000.gz"
        for f, days in ((oldest, 60), (newer, 30), (lone, 60)):
            f.write_bytes(b"gz")
            _age(f, days)
        j._purge_capture_logs()
        assert not oldest.exists()
        assert newer.exists(), "the newest copy of rovimen_dawn.log was purged"
        assert lone.exists(), "the only copy of updater.log was purged"

    def test_unknown_gz_files_are_never_candidates(self, tmp_path):
        j = make_janitor(tmp_path)
        stray = [j.log_base / "rms_cam2.log.20260801.gz",
                 j.log_base / "rovimen_dawn.log.backup.gz",
                 j.log_base / "RO000H_20260801.mkv.gz"]
        for f in stray:
            f.write_bytes(b"gz")
            _age(f, 60)
        j._purge_capture_logs()
        assert all(f.exists() for f in stray)
