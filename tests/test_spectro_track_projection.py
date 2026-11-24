"""Where the reducer looks for the meteor in the GH662S frame.

Until 2026-09-25 ``azalt_to_px`` added the image-up term to the row number,
flipping every projected track about the centre row, and the reducer scored a
one-pixel line there. The existing tests used a horizontal track ON the
centre row, which the flip leaves in place, so nothing caught it; the
RO000A 2026-09-24 20:20:33 meteor sat in the frame at ~8 ADU and scored 0.14.
These tests pin the projection to its inverse and the search to a thin ridge
near (not on) the prediction.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "rovimen-scripts"
sys.path.insert(0, str(SCRIPTS))

import spectro_pipeline as sp  # noqa: E402

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

# Diagonal, well off the centre row: the flip moves it by hundreds of px.
TRACK = [[300.0, 600.0], [700.0, 150.0]]


def _model() -> sp.SpectroModel:
    return sp.SpectroModel.from_config(SPECTRO_CFG)


class TestProjection:
    # 5 px, not 0.5: the solved R and U are 0.006 off orthogonal, which costs
    # up to 3.4 px at the corners. The flip this guards against costs 2*|py-359|.
    @pytest.mark.parametrize("px,py", [(100, 80), (1200, 650), (300, 600), (900, 120)])
    def test_projection_inverts_the_pixel_to_sky_mapping(self, px, py):
        m = _model()
        az, alt = sp._px_to_azalt(m, px, py)
        back = m.azalt_to_px(az, alt)
        assert back is not None
        assert back[0] == pytest.approx(px, abs=5)
        assert back[1] == pytest.approx(py, abs=5)

    def test_image_up_lands_above_the_centre_row(self):
        m = _model()
        c, _, up = m.basis
        v = c + 0.1 * up
        v /= np.linalg.norm(v)
        az = np.degrees(np.arctan2(v[0], v[1])) % 360
        alt = np.degrees(np.arcsin(v[2]))
        _, py = m.azalt_to_px(az, alt)
        assert py < sp.FRAME_H / 2 - 1 - 50


def _frames(n: int = 8, level: float = 60.0, seed: int = 3) -> list[np.ndarray]:
    rng = np.random.default_rng(seed)
    base = np.full((sp.FRAME_H, sp.FRAME_W), level)
    return [np.clip(base + rng.normal(0, 2.0, base.shape), 0, 255) for _ in range(n)]


def _draw_line(frame: np.ndarray, track: list, shift: float, width: int, add: float) -> None:
    pts, normal = sp._track_samples(track, n=2000)
    for w in range(-(width // 2), width // 2 + 1):
        q = pts + normal * (shift + w)
        ok = ((q[:, 0] >= 0) & (q[:, 0] < sp.FRAME_W)
              & (q[:, 1] >= 0) & (q[:, 1] < sp.FRAME_H))
        frame[q[ok, 1].astype(int), q[ok, 0].astype(int)] += add


def _save(tmp_path: Path, frames: list[np.ndarray]) -> list[Path]:
    from PIL import Image

    out = []
    for i, f in enumerate(frames, start=1):
        p = tmp_path / ("chunk_color_%03d.png" % i)
        Image.fromarray(np.clip(f, 0, 255).astype(np.uint8)).save(p)
        out.append(p)
    return out


class TestTransientSearch:
    def test_a_faint_streak_off_the_prediction_is_found(self, tmp_path):
        frames = _frames()
        _draw_line(frames[5], TRACK, shift=24, width=5, add=8)   # ~ the 09-24 meteor
        k, _, score, shift = sp.find_transient_frame(_save(tmp_path, frames), TRACK)
        assert k == 5
        assert score >= sp.TRANSIENT_MIN_SCORE
        assert shift == pytest.approx(24, abs=sp.TRACK_SEARCH_STEP_PX)

    def test_a_broad_cloud_over_the_track_is_not_a_transient(self, tmp_path):
        frames = _frames()
        _draw_line(frames[4], TRACK, shift=0, width=161, add=40)
        _, _, score, _ = sp.find_transient_frame(_save(tmp_path, frames), TRACK)
        assert score < sp.TRANSIENT_MIN_SCORE

    def test_a_cloud_edge_along_the_track_is_not_a_transient(self, tmp_path):
        frames = _frames()
        for s in range(0, 200, 2):   # bright on one side of the line only
            _draw_line(frames[4], TRACK, shift=s, width=1, add=40)
        _, _, score, _ = sp.find_transient_frame(_save(tmp_path, frames), TRACK)
        assert score < sp.TRANSIENT_MIN_SCORE

    def test_a_track_running_off_the_frame_is_not_diluted(self, tmp_path):
        track = [[1000.0, 500.0], [1600.0, 100.0]]   # right half is off-sensor
        frames = _frames()
        _draw_line(frames[2], track, shift=0, width=5, add=8)
        _, _, score, _ = sp.find_transient_frame(_save(tmp_path, frames), track)
        assert score >= sp.TRANSIENT_MIN_SCORE


@pytest.fixture
def event(tmp_path, monkeypatch):
    monkeypatch.setattr(sp, "extract_frames",
                        lambda mkv, out_dir: sorted(Path(out_dir).glob("*.png")))

    def make(track, frames, stale=False):
        from PIL import Image

        ev = tmp_path / "RO000A_20260924_202033"
        (ev / "frames").mkdir(parents=True)
        for i, g in enumerate(frames, start=1):
            rgb = np.repeat(np.clip(g, 0, 255)[..., None], 3, axis=2).astype(np.uint8)
            Image.fromarray(rgb).save(ev / "frames" / ("chunk_color_%03d.png" % i))
        (ev / "chunk_color.mkv").write_bytes(b"")
        (ev / "event.json").write_text(json.dumps({
            "event_id": ev.name, "night": "20260924",
            "track_px": track, "chunks": ["chunk_color.mkv"]}))
        if stale:
            (ev / "spectrum.png").write_bytes(b"old")
            (ev / "spectrum.csv").write_text("old")
        return ev
    return make


class TestReduceEvent:
    def test_a_zero_order_at_the_right_edge_is_labelled_off_sensor(self, event):
        track = [[1300.0, 200.0], [1050.0, 500.0]]
        frames = _frames()
        _draw_line(frames[4], track, shift=16, width=5, add=10)
        res = sp.reduce_event(event(track, frames), _model())
        assert res["detected"] is True
        assert res["spectrum"] is False
        assert "outside the frame" in res["note"]

    def test_a_rereduction_drops_the_previous_spectrum(self, event):
        ev = event(TRACK, _frames(), stale=True)
        res = sp.reduce_event(ev, _model())
        assert res["detected"] is False
        assert not (ev / "spectrum.png").exists()
        assert not (ev / "spectrum.csv").exists()


class TestNightRun:
    def _run(self, tmp_path, monkeypatch, command, **spectro):
        cfg = {"stations": {"GH662S": {"spectro": dict(SPECTRO_CFG, **spectro)},
                            "GH662W": {"spectro": SPECTRO_CFG}}}
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps(cfg))
        calls = []
        monkeypatch.setattr(sp, "run_cut", lambda *a: calls.append("cut") or [])
        monkeypatch.setattr(sp, "run_wide_trigger", lambda *a: calls.append("wide") or [])
        monkeypatch.setattr(sp, "push_events", lambda *a: 0)
        monkeypatch.setattr(sp.Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(sys, "argv", ["spectro_pipeline.py", command,
                                          "--night", "20260924", "--config", str(cfg_path)])
        assert sp.main() == 0
        return calls

    def test_the_night_run_skips_the_widefield_trigger_by_default(self, tmp_path, monkeypatch):
        assert self._run(tmp_path, monkeypatch, "night") == ["cut"]

    def test_the_widefield_trigger_is_opt_in(self, tmp_path, monkeypatch):
        calls = self._run(tmp_path, monkeypatch, "night", widefield_trigger=True)
        assert calls == ["cut", "wide"]

    def test_the_wide_command_still_runs_it_on_demand(self, tmp_path, monkeypatch):
        assert self._run(tmp_path, monkeypatch, "wide") == ["wide"]
