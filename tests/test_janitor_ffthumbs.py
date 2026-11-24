"""Age-based retention for the detection indexer's local ffthumbs/ dirs.

The indexer renders BW FF thumbnails into <capture>/<cam>/<date>/ffthumbs/
and pushes them to the VPS; the archive copy is the canonical one, so the
local renders age out with the same cutoff as stack thumbs. Only the normal
sweep is in scope — the disk-pressure escalation paths never touch ffthumbs.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import janitor_storage_watchdog as jw


CAM = "RO000H"


def _date(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y%m%d")


@pytest.fixture
def janitor(tmp_path):
    capture = tmp_path / "color_capture"
    capture.mkdir()
    cfg = {
        "videocapture_path": str(capture),
        "log_path": str(tmp_path / "logs"),
        "retention": {
            "color_days": 2,
            "locked_days": 7,
            "stacks_days": 3,
            "timelapse_days": 3,
        },
        "stations": {CAM: {}},
    }
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg))
    j = jw.Janitor(str(cfg_path))
    return j, capture


def _mk_ffthumb(capture: Path, date: str, name: str) -> Path:
    p = capture / CAM / date / "ffthumbs" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"RIFF....WEBPx")
    return p


def test_old_ffthumbs_purged_and_dir_removed(janitor):
    j, capture = janitor
    old = _date(10)
    thumb = _mk_ffthumb(capture, old, f"{CAM}_{old}_214512.webp")

    j._purge_station(CAM)

    assert not thumb.exists()
    assert not thumb.parent.exists()


def test_recent_ffthumbs_kept(janitor):
    j, capture = janitor
    recent = _date(1)  # inside the 3-day stacks retention
    thumb = _mk_ffthumb(capture, recent, f"{CAM}_{recent}_214512.webp")

    j._purge_station(CAM)

    assert thumb.exists()


def test_cutoff_boundary_matches_stack_thumbs(janitor):
    # Dates strictly before the cutoff are purged; the cutoff day itself
    # is kept — same semantics as the stacks/thumbs sweep.
    j, capture = janitor
    boundary = _date(3)
    thumb = _mk_ffthumb(capture, boundary, f"{CAM}_{boundary}_214512.webp")

    j._purge_station(CAM)

    assert thumb.exists()


def test_thumbnail_root_ages_out_without_a_capture_dir(janitor):
    """The thumbnails now live outside the capture root (rovimen_config.
    ffthumb_root), so they are swept even when the colour disk is gone."""
    import rovimen_config
    j, _capture = janitor
    root = rovimen_config.ffthumb_root(j.cfg)
    old, new = _date(10), _date(1)
    for d in (old, new):
        (root / CAM / d).mkdir(parents=True)
        (root / CAM / d / f"{CAM}_{d}_214512.webp").write_bytes(b"x")

    j._purge_ffthumbs()

    assert not (root / CAM / old).exists()
    assert (root / CAM / new / f"{CAM}_{new}_214512.webp").exists()
