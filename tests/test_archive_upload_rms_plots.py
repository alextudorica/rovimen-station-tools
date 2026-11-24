"""The station uploader must mirror every RMS check plot the dashboard labels.

Until 2026-09 ``_find_rms_files`` pushed only the two calibration PNGs, so the
day the plot surfaces went archive-first every other check plot -- radiants,
stacks, field sums, photometry, observing periods -- disappeared from the
dashboard. These tests pin the uploader's glob list to the dashboard catalogue
and prove a full RMS session dir comes back whole.
"""
from __future__ import annotations

import fnmatch
from pathlib import Path

import archive_upload
from archive_upload import _RMS_PLOT_GLOBS, _Uploader
from routes.archive import RMS_PLOT_LABELS

SESSION = "RO000A_20260905_171639_633140"


def _matches(name: str) -> bool:
    return any(fnmatch.fnmatch(name, g) for g in _RMS_PLOT_GLOBS)


def test_every_dashboard_plot_kind_is_uploaded():
    missing = [s for s in RMS_PLOT_LABELS if not _matches(f"{SESSION}{s}")]
    assert missing == [], f"uploader never sends: {missing}"


def test_meteor_stack_special_case_is_uploaded():
    assert _matches(f"{SESSION}_stack_62_meteors.jpg")


def test_find_rms_files_returns_the_whole_session(tmp_path: Path):
    rms = tmp_path / "RMS_data" / "cam1"
    session = rms / "ArchivedFiles" / SESSION
    session.mkdir(parents=True)
    plots = [f"{SESSION}{s}" for s in RMS_PLOT_LABELS] + [f"{SESSION}_stack_62_meteors.jpg"]
    texts = [f"FTPdetectinfo_{SESSION}.txt", f"{SESSION}_radiants.txt"]
    noise = [f"FTPdetectinfo_{SESSION}_unfiltered.txt", f"{SESSION}.config", "platepar_cmn2010.cal"]
    for name in plots + texts + noise:
        (session / name).write_bytes(b"x")

    up = _Uploader.__new__(_Uploader)
    up.stations = {"RO000A": {"rms_data_path": str(rms)}}
    got = sorted(p.name for p in up._find_rms_files("RO000A", "20260905"))

    assert got == sorted(plots + texts)
