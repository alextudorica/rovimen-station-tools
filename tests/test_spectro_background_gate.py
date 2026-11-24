"""A bright meteor does not prove its spectrum was recorded.

`reduce_event` gates on the zero-order transient score, which answers "was
something there" -- and then wrote `spectrum: true` for whatever the walk along
the dispersion axis happened to integrate, without ever asking whether that was
meteor light. On a cloudy night the walk crosses moonlit cloud and returns a
smooth blue-to-red continuum that passes for a spectrum.

That is not hypothetical. On the 2026-09-21 21:12:36 GH662S event the reducer
reported `spectrum: true` with a zero-order score of 122.89 against a threshold
of 3.0, and the extracted profile had no emission lines at all: Mg +0.5 sigma,
Na +0.9 sigma, O +1.5 sigma. Re-running the identical integration on tracks
displaced into blank sky returned between 0.53x and 1.91x the meteor track's
own flux with correlated profile shape -- the "spectrum" was the sky. Every one
of the seven spectra produced on 2026-09-17 was likewise a cloud edge.

So the reducer now measures the same walk off-target and requires the real
track to stand out from it. These tests pin that gate: a genuine dispersed
feature survives, a uniform or cloud-dominated field does not, and `detected`
stays true either way because the transient itself is real and the operator
still needs its preview.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "rovimen-scripts"
sys.path.insert(0, str(SCRIPTS))

import spectro_pipeline as sp  # noqa: E402

# Real ffmpeg encodes/decodes: 9-17 s each locally, 4x that on a slow CI runner,
# so the suite-wide 60 s timeout (pyproject) would flake on them.
pytestmark = pytest.mark.timeout(300)


SPECTRO_CFG = {
    "basis": [[0.186803, 0.29696, 0.93644],
              [0.437833, 0.828149, -0.349959],
              [0.882123, -0.470254, -0.026843]],
    "scale_x": 25.26, "scale_y": 24.36,
    "dispersion_px_per_nm": 0.72,
    "dispersion_axis": [1.0, 0.0],
    "zero_order_to_400nm_px": 288,
    "trigger_stations": ["RO000A"],
}

TRACK = [[600.0, 360.0], [500.0, 360.0]]


def _model():
    return sp.SpectroModel.from_config(SPECTRO_CFG)


def _write_event(tmp_path: Path, frames: list[np.ndarray]) -> Path:
    """An event directory with pre-extracted frames, so the reducer never
    shells out to ffmpeg."""
    from PIL import Image

    ev = tmp_path / "EV_20260921_211236"
    (ev / "frames").mkdir(parents=True)
    for i, arr in enumerate(frames, start=1):
        Image.fromarray(arr.astype(np.uint8)).save(
            ev / "frames" / ("chunk_color_%03d.png" % i))
    (ev / "chunk_color.mkv").write_bytes(b"")
    (ev / "event.json").write_text(json.dumps({
        "event_id": ev.name, "night": "20260921",
        "track_px": TRACK, "chunks": ["chunk_color.mkv"],
    }))
    return ev


@pytest.fixture(autouse=True)
def _no_ffmpeg(monkeypatch):
    """Serve the frames the test laid out instead of decoding a chunk.

    reduce_event builds its frame list only from what it extracts, so the
    tests stand in for ffmpeg rather than shipping real video.
    """
    monkeypatch.setattr(
        sp, "extract_frames",
        lambda mkv, out_dir: sorted(Path(out_dir).glob("*.png")))


def _blank(n=6, level=20):
    """A flat sky with a little noise, identical frame to frame."""
    rng = np.random.default_rng(7)
    base = np.full((sp.FRAME_H, sp.FRAME_W, 3), level, dtype=float)
    return [np.clip(base + rng.normal(0, 1.5, base.shape), 0, 255) for _ in range(n)]


def _add_zero_order(frame):
    """A bright streak along the track: this is what the score measures."""
    for x in range(int(TRACK[1][0]), int(TRACK[0][0]) + 1):
        frame[int(TRACK[0][1]) - 1:int(TRACK[0][1]) + 2, x, :] = 250
    return frame


def _add_first_order(frame):
    """Dispersed light where the model says it must be: +288..+562 px in x."""
    for x0 in (int(TRACK[1][0]), int(TRACK[0][0])):
        for d in range(288, 562):
            x = x0 + d
            if x < sp.FRAME_W:
                frame[int(TRACK[0][1]) - 2:int(TRACK[0][1]) + 3, x, :] += 90
    return frame


class TestBackgroundGate:
    def test_a_real_dispersed_feature_is_kept(self, tmp_path):
        frames = _blank()
        frames[-1] = _add_first_order(_add_zero_order(frames[-1].copy()))
        ev = _write_event(tmp_path, frames)
        res = sp.reduce_event(ev, _model())
        assert res["detected"] is True
        assert res["spectrum"] is True, res.get("note")
        assert res["contrast"] >= sp.SPECTRUM_MIN_CONTRAST
        assert (ev / "spectrum.csv").exists()

    def test_a_bright_transient_with_no_dispersed_light_is_rejected(self, tmp_path):
        """The exact 2026-09-21 shape: strong zero order, nothing dispersed."""
        frames = _blank()
        frames[-1] = _add_zero_order(frames[-1].copy())
        ev = _write_event(tmp_path, frames)
        res = sp.reduce_event(ev, _model())
        assert res["detected"] is True, "the transient is real; only the spectrum is not"
        assert res["spectrum"] is False
        assert res["contrast"] < sp.SPECTRUM_MIN_CONTRAST
        assert "background" in res["note"]

    def test_no_spectrum_files_are_written_when_rejected(self, tmp_path):
        """A rejected event must not leave a CSV that reads as a measurement."""
        frames = _blank()
        frames[-1] = _add_zero_order(frames[-1].copy())
        ev = _write_event(tmp_path, frames)
        sp.reduce_event(ev, _model())
        assert not (ev / "spectrum.csv").exists()
        assert not (ev / "spectrum.png").exists()

    def test_the_preview_is_still_written_when_rejected(self, tmp_path):
        """An event with no picture is indistinguishable on the dashboard from
        one that was never processed -- that contract predates this gate."""
        frames = _blank()
        frames[-1] = _add_zero_order(frames[-1].copy())
        ev = _write_event(tmp_path, frames)
        sp.reduce_event(ev, _model())
        assert (ev / "spectro.jpg").exists()

    def test_contrast_is_recorded_on_both_paths(self, tmp_path):
        """Every verdict carries its number, so past events stay auditable."""
        frames = _blank()
        frames[-1] = _add_zero_order(frames[-1].copy())
        ev = _write_event(tmp_path, frames)
        sp.reduce_event(ev, _model())
        assert "contrast" in json.loads((ev / "reduction.json").read_text())


class TestControlGeometry:
    def test_controls_are_perpendicular_to_the_dispersion_axis(self):
        """Displacing ALONG the dispersion axis would just resample the same
        spectrum at a wavelength offset and veto every real detection."""
        assert all(s != 0 for s in sp.SPECTRUM_CONTROL_SHIFTS_PX)
        assert len(sp.SPECTRUM_CONTROL_SHIFTS_PX) >= 4
        assert min(sp.SPECTRUM_CONTROL_SHIFTS_PX) < 0 < max(sp.SPECTRUM_CONTROL_SHIFTS_PX), (
            "controls must sample both sides, or a one-sided cloud gradient "
            "biases the reference"
        )

    def test_threshold_is_above_unity(self):
        """At 1.0 the gate would pass anything that merely matches the sky."""
        assert sp.SPECTRUM_MIN_CONTRAST > 1.0
