"""Per-night spectro index + event previews (station side).

The dashboard used to learn what each event was by opening its event.json and
reduction.json: 76 storage-box reads to render a 38-event night. The station
now writes one index.json per night from files it already has open, and a
preview image for every event — including the ones where nothing was found,
which are the majority and used to have no picture at all.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "rovimen-scripts"))

from spectro_pipeline import (  # noqa: E402
    PREVIEW_MAX_W,
    build_night_index,
    write_preview,
)


def _event(night_dir: Path, ev_id: str, event: dict, reduction: dict | None,
           artifacts: tuple[str, ...] = ()) -> Path:
    d = night_dir / ev_id
    d.mkdir(parents=True)
    (d / "event.json").write_text(json.dumps(event))
    if reduction is not None:
        (d / "reduction.json").write_text(json.dumps(reduction))
    for name in artifacts:
        (d / name).write_bytes(b"x")
    return d


@pytest.fixture
def night(tmp_path: Path) -> Path:
    n = tmp_path / "20260918"
    n.mkdir()
    _event(n, "RO000A_20260918_212418",
           {"event_id": "RO000A_20260918_212418", "ff": "FF_x.bin"},
           {"detected": False, "score": 1.1,
            "note": "no on-track transient above threshold"},
           ("spectro.jpg",))
    _event(n, "RO000B_20260918_174310",
           {"event_id": "RO000B_20260918_174310", "ff": "FF_y.bin"},
           {"detected": True, "spectrum": False, "score": 4.2},
           ("spectro.jpg",))
    _event(n, "WIDE_20260918_235813",
           {"event_id": "WIDE_20260918_235813", "trigger": "widefield_streak"},
           {"detected": True, "spectrum": True, "score": 9.9},
           ("wide.jpg", "spectro.jpg", "spectrum.png", "spectrum.csv"))
    return n


def test_index_counts_events_by_trigger_and_outcome(night):
    idx = build_night_index(night, "GH662S", "20260918")

    assert idx["counts"] == {"events": 3, "detected": 2, "spectra": 1,
                             "rms": 2, "widefield": 1}
    assert idx["night"] == "20260918"
    assert idx["spectro_code"] == "GH662S"


def test_index_classifies_each_event_for_the_tab(night):
    events = {e["id"]: e for e in build_night_index(night, "GH662S", "20260918")["events"]}

    assert events["RO000A_20260918_212418"]["state"] == "none"
    assert events["RO000B_20260918_174310"]["state"] == "transient"
    assert events["WIDE_20260918_235813"]["state"] == "spectrum"
    # The trigger decides which camera's frame the tab labels as the trigger
    # frame, so it has to survive into the index.
    assert events["WIDE_20260918_235813"]["trigger"] == "widefield"
    assert events["WIDE_20260918_235813"]["source"] == "GH662W"
    assert events["RO000A_20260918_212418"]["trigger"] == "rms"
    assert events["RO000A_20260918_212418"]["source"] == "RO000A"


def test_index_carries_the_time_and_only_uploadable_files(night):
    events = {e["id"]: e for e in build_night_index(night, "GH662S", "20260918")["events"]}

    assert events["RO000A_20260918_212418"]["time"] == "21:24:18"
    assert events["WIDE_20260918_235813"]["files"] == [
        "event.json", "reduction.json", "spectro.jpg", "spectrum.csv",
        "spectrum.png", "wide.jpg",
    ]


def test_index_survives_an_event_with_no_reduction_yet(night):
    _event(night, "RO000A_20260919_010711",
           {"event_id": "RO000A_20260919_010711"}, None)

    idx = build_night_index(night, "GH662S", "20260918")

    assert idx["counts"]["events"] == 4
    entry = next(e for e in idx["events"] if e["id"] == "RO000A_20260919_010711")
    assert entry["state"] == "none"


def test_index_skips_a_directory_whose_event_json_is_unreadable(night):
    broken = night / "RO000A_20260919_999999"
    broken.mkdir()
    (broken / "event.json").write_text("{not json")

    idx = build_night_index(night, "GH662S", "20260918")

    assert idx["counts"]["events"] == 3
    assert all(e["id"] != "RO000A_20260919_999999" for e in idx["events"])


def test_index_is_written_into_the_night_dir(night):
    build_night_index(night, "GH662S", "20260918")

    on_disk = json.loads((night / "index.json").read_text())
    assert on_disk["counts"]["events"] == 3


# ── previews ────────────────────────────────────────────────────────────────

def test_preview_stretches_a_faint_frame_instead_of_rendering_black(tmp_path):
    """A median-subtracted difference frame spans a handful of ADU. A fixed
    gain renders it black, which is what an operator would be asked to
    review."""
    pytest.importorskip("PIL")
    from PIL import Image

    img = np.random.default_rng(0).normal(0.0, 0.6, (720, 1280))
    img[300:305, 200:900] += 4.0          # the trail
    out = tmp_path / "spectro.jpg"

    write_preview(img, out)

    a = np.asarray(Image.open(out).convert("L"), dtype=float)
    assert a.max() > 200, "the trail must survive the stretch"
    assert a.mean() < 160, "and the sky must not be blown out"


def test_preview_is_downscaled_and_small_enough_to_ship_per_event(tmp_path):
    pytest.importorskip("PIL")
    from PIL import Image

    out = tmp_path / "wide.jpg"
    write_preview(np.random.default_rng(1).normal(40, 8, (720, 1280)), out)

    im = Image.open(out)
    assert im.width == PREVIEW_MAX_W
    assert im.height == round(720 * PREVIEW_MAX_W / 1280)
    # 38 events a night, three images each, on a metered storage box.
    assert out.stat().st_size < 400_000


def test_preview_draws_the_track_it_was_given(tmp_path):
    pytest.importorskip("PIL")
    from PIL import Image

    flat = np.full((720, 1280), 30.0)
    plain, marked = tmp_path / "a.jpg", tmp_path / "b.jpg"

    write_preview(flat, plain)
    write_preview(flat, marked, lines=[[(100, 100), (900, 400)]])

    a = np.asarray(Image.open(plain).convert("RGB"), dtype=int)
    b = np.asarray(Image.open(marked).convert("RGB"), dtype=int)
    assert np.abs(a - b).sum() > 0, "the track overlay must be visible"
    # Drawn in red so it reads against a grayscale frame.
    red_excess = (b[..., 0].astype(int) - b[..., 2].astype(int)).max()
    assert red_excess > 60
