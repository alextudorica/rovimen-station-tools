"""A failing boundary stitch must not cost the rest of the night's locks.

`_stitch_clips` shells out to ffmpeg with a timeout, and `subprocess.run`
RAISES `TimeoutExpired` rather than returning a non-zero result. That exception
used to escape `process_night` entirely: dawn logged "EON detection pass
failed" and every detection after the slow one stayed unlocked, so it was never
encoded, never uploaded, rendered as "clip pending" on the dashboard, and was
reaped once the chunk retention window closed.

Observed on gmnro05 (Bistrița, 5 cameras, no working VAAPI) on 2026-09-14 for
night 20260913: all five cameras died at 04:01:58 on a 60 s stream-copy budget,
between them losing 54 of the night's detection clips.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import detection_lock
import flags_manager


STATION = "RO000H"
DATE_STR = "20260322"

# 21:30:59 sits 19 s into the 21:30:40 chunk — inside STITCH_BOUNDARY_SECS of
# its trailing edge, so it takes the stitch path. 21:31:25 sits 5 s into the
# 21:31:20 chunk and takes the plain path; it is the one that used to be lost.
STITCHED_CHUNK = f"{STATION}_{DATE_STR}_213040_color.mkv"
LATER_CHUNK = f"{STATION}_{DATE_STR}_213120_color.mkv"


@pytest.fixture
def night(tmp_path, sample_config):
    cap = Path(sample_config["videocapture_path"])
    chunk_dir = cap / STATION / DATE_STR
    chunk_dir.mkdir(parents=True)
    for t in ("213040", "213100", "213120", "213140"):
        (chunk_dir / f"{STATION}_{DATE_STR}_{t}_color.mkv").write_bytes(b"")

    rms_path = Path(sample_config["stations"][STATION]["rms_data_path"])
    session = rms_path / "ArchivedFiles" / f"{STATION}_{DATE_STR}_210000_000000"
    session.mkdir(parents=True)
    (session / f"FTPdetectinfo_{STATION}_{DATE_STR}_210000_000000.txt").write_text(
        "------------------------------\n"
        "Meteor Count = 2\n"
        "------------------------------\n"
        f"FF_{STATION}_{DATE_STR}_213059_000\n"
        "720  576 25.00  fps\n"
        "   0.00  320.5  240.2  1000  5.5  32.1  123.4  45.6\n"
        "------------------------------\n"
        f"FF_{STATION}_{DATE_STR}_213125_000\n"
        "720  576 25.00  fps\n"
        "   0.00  310.1  230.9  1000  4.2  30.0  120.0  44.0\n"
        "------------------------------\n"
    )
    return {"cfg": sample_config, "chunk_dir": chunk_dir}


def _locks(cfg) -> dict:
    state = flags_manager.load(STATION, DATE_STR, cfg)
    return {
        name: entry.get("lock")
        for name, entry in (state.get("chunks") or {}).items()
    }


def test_ffmpeg_timeout_does_not_abort_the_lock_pass(night):
    """A stitch that times out costs its own stitch, not the whole night."""
    cfg = night["cfg"]

    def _timeout(cmd, *a, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 300))

    with patch.object(detection_lock.subprocess, "run", side_effect=_timeout):
        ran = detection_lock.process_night(STATION, DATE_STR, cfg)

    assert ran is True
    locks = _locks(cfg)
    # The boundary detection falls back to locking its raw chunk...
    assert locks.get(STITCHED_CHUNK) is not None
    # ...and, the point of the fix, the pass reaches the LATER detection.
    assert locks.get(LATER_CHUNK) is not None


def test_ffmpeg_timeout_leaves_no_stitched_file_behind(night):
    cfg = night["cfg"]

    def _timeout(cmd, *a, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 300))

    with patch.object(detection_lock.subprocess, "run", side_effect=_timeout):
        detection_lock.process_night(STATION, DATE_STR, cfg)

    stitched = list(night["chunk_dir"].glob("*_*_*_*_color.mkv"))
    assert stitched == [], f"partial stitch left behind: {stitched}"


def test_run_ffmpeg_returns_none_instead_of_raising():
    def _timeout(cmd, *a, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)

    with patch.object(detection_lock.subprocess, "run", side_effect=_timeout):
        assert detection_lock._run_ffmpeg(["ffmpeg"], 1, STATION, "tail") is None

    with patch.object(detection_lock.subprocess, "run",
                      side_effect=OSError("No such file")):
        assert detection_lock._run_ffmpeg(["ffmpeg"], 1, STATION, "tail") is None


def test_stitch_clips_safe_swallows_unexpected_exceptions(tmp_path):
    a = tmp_path / f"{STATION}_{DATE_STR}_213040_color.mkv"
    b = tmp_path / f"{STATION}_{DATE_STR}_213100_color.mkv"
    with patch.object(detection_lock, "_stitch_clips",
                      side_effect=RuntimeError("disk went away")):
        assert detection_lock._stitch_clips_safe(
            a, b, STATION, DATE_STR, tmp_path, {}
        ) is None


# -- Per-camera-night breaker -------------------------------------------------
#
# On gmnro09 (Sibiciu), whose capture and RMS data share one USB HDD, 19
# extracts of RO0013 ran into the 300 s budget on 2026-10-26: 5700 s of a
# 7522 s EON pass. After STITCH_TIMEOUT_BREAKER timeouts the pass stops
# stitching for that camera-night and locks the raw chunks.

BOUNDARY_STARTS = ("213040", "213100", "213120", "213140")


@pytest.fixture
def boundary_night(tmp_path, sample_config):
    """Four detections, each 19 s into its chunk, so each one stitches."""
    cap = Path(sample_config["videocapture_path"])
    chunk_dir = cap / STATION / DATE_STR
    chunk_dir.mkdir(parents=True)
    for t in BOUNDARY_STARTS + ("213200",):
        (chunk_dir / f"{STATION}_{DATE_STR}_{t}_color.mkv").write_bytes(b"")

    rms_path = Path(sample_config["stations"][STATION]["rms_data_path"])
    session = rms_path / "ArchivedFiles" / f"{STATION}_{DATE_STR}_210000_000000"
    session.mkdir(parents=True)
    blocks = "".join(
        f"FF_{STATION}_{DATE_STR}_{t[:4]}{int(t[4:]) + 19:02d}_000\n"
        "720  576 25.00  fps\n"
        "   0.00  320.5  240.2  1000  5.5  32.1  123.4  45.6\n"
        "------------------------------\n"
        for t in BOUNDARY_STARTS
    )
    (session / f"FTPdetectinfo_{STATION}_{DATE_STR}_210000_000000.txt").write_text(
        "------------------------------\n"
        f"Meteor Count = {len(BOUNDARY_STARTS)}\n"
        "------------------------------\n" + blocks
    )
    return {"cfg": sample_config, "chunk_dir": chunk_dir}


def test_breaker_stops_stitching_after_two_timeouts(boundary_night, caplog):
    cfg = boundary_night["cfg"]
    calls = []

    def _timeout(cmd, *a, **kw):
        calls.append(cmd)
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 60))

    with patch.object(detection_lock.subprocess, "run", side_effect=_timeout), \
         caplog.at_level("INFO", logger="detection_lock"):
        assert detection_lock.process_night(STATION, DATE_STR, cfg) is True

    # Two stitches timed out on their first extract; the other two ran no ffmpeg.
    assert len(calls) == detection_lock.STITCH_TIMEOUT_BREAKER == 2
    locks = _locks(cfg)
    for t in BOUNDARY_STARTS:
        assert locks.get(f"{STATION}_{DATE_STR}_{t}_color.mkv") is not None
    warnings = [r for r in caplog.records
                if r.levelname == "WARNING" and "Stitch breaker" in r.getMessage()]
    assert len(warnings) == 1
    assert "2 ffmpeg timeout(s)" in warnings[0].getMessage()
    assert "2 later boundary detection(s)" in warnings[0].getMessage()


def test_failures_other_than_timeouts_do_not_trip_the_breaker(boundary_night, caplog):
    cfg = boundary_night["cfg"]
    calls = []

    def _fails(cmd, *a, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, b"", b"Invalid data")

    with patch.object(detection_lock.subprocess, "run", side_effect=_fails), \
         caplog.at_level("INFO", logger="detection_lock"):
        detection_lock.process_night(STATION, DATE_STR, cfg)

    assert len(calls) == len(BOUNDARY_STARTS)
    assert "Stitch breaker" not in caplog.text


def test_open_breaker_still_reuses_an_existing_stitch(tmp_path):
    a = tmp_path / f"{STATION}_{DATE_STR}_213040_color.mkv"
    b = tmp_path / f"{STATION}_{DATE_STR}_213100_color.mkv"
    done = tmp_path / f"{STATION}_{DATE_STR}_213050_213110_color.mkv"
    for p in (a, b, done):
        p.write_bytes(b"")
    breaker = detection_lock._StitchBreaker(timeouts=2)
    with patch.object(detection_lock.subprocess, "run") as run:
        assert detection_lock._stitch_clips(
            a, b, STATION, DATE_STR, tmp_path, {}, breaker=breaker) == done
    run.assert_not_called()
    assert breaker.skipped == 0


def test_ffmpeg_never_reads_stdin(boundary_night):
    """ffmpeg reads stdin for interactive keys unless told not to; a dawn
    started from a shell can then stop on SIGTTIN until the timeout."""
    cfg = boundary_night["cfg"]
    seen = []

    def _record(cmd, *a, **kw):
        seen.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    with patch.object(detection_lock.subprocess, "run", side_effect=_record):
        detection_lock.process_night(STATION, DATE_STR, cfg)

    # Tail extract, head extract and concat all ran (the fake writes no file,
    # so each stitch then fails on the move and falls back).
    assert {"concat"} <= {arg for cmd, _ in seen for arg in cmd}
    assert len(seen) == 3 * len(BOUNDARY_STARTS)
    for cmd, kw in seen:
        assert cmd[:2] == ["ffmpeg", "-nostdin"]
        assert kw.get("stdin") is subprocess.DEVNULL


def test_stitch_budgets_are_a_minute():
    assert detection_lock.STITCH_EXTRACT_TIMEOUT_S <= 60
    assert detection_lock.STITCH_CONCAT_TIMEOUT_S <= 60
