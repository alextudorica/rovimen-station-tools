"""Stitched-clip stacks must carry exactly one data bar.

A clip that spans a chunk boundary gets a stitched clip plus a stitched stack,
built by ``detection_lock._stitch_stacks`` from the two neighbouring per-chunk
stacks. Both sources already carry the burned-in overlay bar, and the old
implementation did two things wrong:

  1. it max-combined the WHOLE frame, overprinting the two bars on each other, so
     the strip showed two timestamps and two logos blended into mush;
  2. it saved with PIL, which writes no EXIF, dropping the overlaid-still marker.

Consequence of (2): the dashboard's stack gate read the merge as un-branded and
burned a SECOND bar underneath, producing the 1280x976 double-strip images that
shipped for every stitched stack -- and because the fleet was still on the old
oversized logo, the extra strip is where the giant wordmark showed up.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import detection_lock
import overlay
import stack_overlay


REPO_ROOT = Path(__file__).resolve().parent.parent
FONT = REPO_ROOT / "rovimen-scripts" / "fonts" / "VCR_OSD_MONO_1.001.ttf"
LOGO = REPO_ROOT / "rovimen-scripts" / "assets" / "astromania_text.png"

CAM = "RO000Q"
SKY_H = 720


def _cfg() -> dict:
    return {
        "overlay": {
            "enabled": True,
            "font": str(FONT),
            "logo": str(LOGO),
            "font_size": 19,
            "network": "ROVIMEN",
            "style": "cinema",
            "show_logo": True,
        }
    }


def _barred_stack(path: Path, sky_value: int, bar_value: int, bar_h: int) -> Path:
    """A stack shaped like a station-burned one: sky on top, data bar below."""
    arr = np.zeros((SKY_H + bar_h, 1280, 3), dtype=np.uint8)
    arr[:SKY_H] = sky_value
    arr[SKY_H:] = bar_value
    Image.fromarray(arr).save(path, "WEBP", quality=95)
    return path


# ── bar geometry is one number, shared ───────────────────────────────────────


def test_bar_height_matches_the_deployed_128px_bar():
    assert overlay.bar_height({"style": "cinema", "font_size": 19}) == 128


def test_bar_height_zero_for_barless_style():
    assert overlay.bar_height({"style": "standard", "font_size": 19}) == 0


def test_build_drawtext_annotations_agrees_with_bar_height():
    cfg = {
        "enabled": True, "font": str(FONT), "logo": str(LOGO), "font_size": 19,
        "network": "ROVIMEN", "style": "cinema",
    }
    _filters, bar_h = overlay.build_drawtext_annotations(cfg, CAM, {}, 0)
    assert bar_h == overlay.bar_height(cfg)


# ── the merge itself ─────────────────────────────────────────────────────────


@pytest.fixture
def stitch_dir(tmp_path):
    d = tmp_path / "20260819"
    (d / "stacks").mkdir(parents=True)
    return d


def _run_stitch(stitch_dir: Path, bar_h: int) -> Path:
    stacks = stitch_dir / "stacks"
    clip_a = stitch_dir / f"{CAM}_20260819_010607_color.mkv"
    clip_b = stitch_dir / f"{CAM}_20260819_010627_color.mkv"
    stitched = stitch_dir / f"{CAM}_20260819_010607_010627_color.mkv"
    # Distinct sky values so we can prove the max-combine happened, distinct bar
    # values so we can prove the bar was copied and not blended.
    _barred_stack(stacks / f"{CAM}_20260819_010607_stack.webp", 40, 10, bar_h)
    _barred_stack(stacks / f"{CAM}_20260819_010627_stack.webp", 90, 200, bar_h)
    detection_lock._stitch_stacks(clip_a, clip_b, stitched, CAM, "20260819", _cfg())
    return stacks / f"{CAM}_20260819_010607_010627_stack.webp"


def test_stitched_stack_keeps_a_single_bar_from_the_earlier_chunk(stitch_dir):
    bar_h = overlay.bar_height(_cfg()["overlay"])
    out = _run_stitch(stitch_dir, bar_h)
    assert out.exists()

    arr = np.array(Image.open(out).convert("RGB"))
    assert arr.shape[0] == SKY_H + bar_h, "merge must not change the frame height"
    # Sky: max-combined, so the brighter of the two sources wins.
    assert arr[:SKY_H].max() >= 88
    # Bar: taken verbatim from clip_a, NOT max-blended with clip_b's brighter bar.
    assert arr[SKY_H:].max() < 100, "bar was blended instead of copied"


def test_stitched_stack_is_marked_as_already_overlaid(stitch_dir):
    out = _run_stitch(stitch_dir, overlay.bar_height(_cfg()["overlay"]))
    assert overlay.still_is_overlaid(out), "missing marker re-brands into a second bar"


def test_stitched_stack_passes_the_dashboard_gate_untouched(stitch_dir, monkeypatch):
    """The gate must return the bytes unchanged -- no second bar."""
    out = _run_stitch(stitch_dir, overlay.bar_height(_cfg()["overlay"]))
    original = out.read_bytes()

    monkeypatch.setattr(stack_overlay, "_overlay_mod", overlay)
    served = stack_overlay.ensure_overlaid_stack_bytes(
        out, out.name, _cfg()["overlay"], CAM, {},
    )
    assert served == original


def test_overlay_disabled_falls_back_to_whole_frame_merge(stitch_dir):
    """With no bar there is nothing to protect, so the old behaviour is correct."""
    stacks = stitch_dir / "stacks"
    clip_a = stitch_dir / f"{CAM}_20260819_010607_color.mkv"
    clip_b = stitch_dir / f"{CAM}_20260819_010627_color.mkv"
    stitched = stitch_dir / f"{CAM}_20260819_010607_010627_color.mkv"
    _barred_stack(stacks / f"{CAM}_20260819_010607_stack.webp", 40, 10, 0)
    _barred_stack(stacks / f"{CAM}_20260819_010627_stack.webp", 90, 200, 0)
    cfg = {"overlay": {"enabled": False}}

    detection_lock._stitch_stacks(clip_a, clip_b, stitched, CAM, "20260819", cfg)

    out = stacks / f"{CAM}_20260819_010607_010627_stack.webp"
    arr = np.array(Image.open(out).convert("RGB"))
    assert arr.shape[0] == SKY_H
    assert arr.max() >= 88


def test_an_empty_stitched_stack_is_rebuilt(stitch_dir):
    """A merge killed mid-save left 0 bytes, which used to count as done."""
    out = stitch_dir / "stacks" / f"{CAM}_20260819_010607_010627_stack.webp"
    out.write_bytes(b"")
    assert _run_stitch(stitch_dir, overlay.bar_height(_cfg()["overlay"])) == out
    assert out.stat().st_size > 0
    assert overlay.still_is_overlaid(out)


def test_a_merge_killed_mid_save_leaves_no_empty_stack(stitch_dir, monkeypatch):
    """The merge is written to a scratch file and renamed into place, so an
    interrupted save (color_capture SIGTERMs a late dawn run) leaves nothing
    that looks like a finished stack."""
    stacks = stitch_dir / "stacks"
    _barred_stack(stacks / f"{CAM}_20260819_010607_stack.webp", 40, 10, 128)
    _barred_stack(stacks / f"{CAM}_20260819_010627_stack.webp", 90, 200, 128)

    def _killed(self, fp, *args, **kwargs):
        Path(fp).write_bytes(b"")   # the file exists, empty, as when PIL is killed
        raise RuntimeError("SIGTERM")

    monkeypatch.setattr(Image.Image, "save", _killed)
    detection_lock._stitch_stacks(
        stitch_dir / f"{CAM}_20260819_010607_color.mkv",
        stitch_dir / f"{CAM}_20260819_010627_color.mkv",
        stitch_dir / f"{CAM}_20260819_010607_010627_color.mkv", CAM, "20260819", _cfg())
    assert sorted(p.name for p in stacks.iterdir()) == [
        f"{CAM}_20260819_010607_stack.webp", f"{CAM}_20260819_010627_stack.webp"]


def test_mismatched_source_shapes_are_skipped(stitch_dir):
    stacks = stitch_dir / "stacks"
    clip_a = stitch_dir / f"{CAM}_20260819_010607_color.mkv"
    clip_b = stitch_dir / f"{CAM}_20260819_010627_color.mkv"
    stitched = stitch_dir / f"{CAM}_20260819_010607_010627_color.mkv"
    _barred_stack(stacks / f"{CAM}_20260819_010607_stack.webp", 40, 10, 128)
    Image.new("RGB", (640, 480), (9, 9, 9)).save(
        stacks / f"{CAM}_20260819_010627_stack.webp", "WEBP")

    detection_lock._stitch_stacks(clip_a, clip_b, stitched, CAM, "20260819", _cfg())

    assert not (stacks / f"{CAM}_20260819_010607_010627_stack.webp").exists()


# ── the gate's name-based fallback for stacks already on the storage box ──────


@pytest.mark.parametrize("name,expected", [
    ("RO000Q_20260820_010607_010627_stack.webp", True),
    ("RO000Q_20260819_174756_stack.webp", False),
    ("RO000Q_20260819_174756_010627_stack.png", True),
    ("RO000Q_20260819_night_stack.webp", False),
    ("", False),
])
def test_stitched_names_are_recognised_as_pre_barred(name, expected):
    assert stack_overlay.stack_name_implies_overlaid(name) is expected


def test_gate_passes_through_unmarked_stitched_stack(tmp_path, monkeypatch):
    """Backfill-free fix for the ~30 stitched stacks per camera-night already
    on the box: they predate the marker but are barred by construction."""
    p = tmp_path / f"{CAM}_20260819_010607_010627_stack.webp"
    _barred_stack(p, 40, 10, 128)          # barred pixels, no EXIF marker
    assert not overlay.still_is_overlaid(p)

    monkeypatch.setattr(stack_overlay, "_overlay_mod", overlay)
    served = stack_overlay.ensure_overlaid_stack_bytes(
        p, p.name, _cfg()["overlay"], CAM, {},
    )
    assert served == p.read_bytes()
