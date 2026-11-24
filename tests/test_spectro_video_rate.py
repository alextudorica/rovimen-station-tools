"""The GH662S grating camera at 25 fps, with RMS running on it.

At video rate each frame holds the meteor as a moving point, so the reducer
follows the zero order frame by frame (from RMS's own centroids, or traced
along a witness camera's projected track) and co-adds a point-source spectrum
per frame. These tests stand in for ffmpeg: frames are numpy arrays, and the
cut stage's window copy is stubbed.
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

# Real ffmpeg encodes/decodes: 9-17 s each locally, 4x that on a slow CI runner,
# so the suite-wide 60 s timeout (pyproject) would flake on them.
pytestmark = pytest.mark.timeout(300)

CFG = {
    "basis": [[0.186803, 0.29696, 0.93644],
              [0.437833, 0.828149, -0.349959],
              [0.882123, -0.470254, -0.026843]],
    "scale_x": 25.26, "scale_y": 24.36,
    "dispersion_px_per_nm": 0.72,
    "dispersion_axis": [1.0, 0.0],
    "zero_order_to_400nm_px": 288,
    "trigger_stations": ["RO000A", "GH662S"],
}
NIGHT = "20260926"


def _model() -> sp.SpectroModel:
    return sp.SpectroModel.from_config(CFG)


def _dx(nm: float) -> int:
    return int(288 + (nm - 400.0) * 0.72)


# ── FTPdetectinfo ──────────────────────────────────────────────────────────

def _block(ff: str, rows: list, cam: str = "GH662S") -> list[str]:
    out = ["-------------------------------------------------------", ff, "Uncalibrated",
           f"{cam} 0001 {len(rows):04d} 0025.00 000.0 000.0  00.0   9.0 0100.0 0050.0"]
    for n, x, y in rows:
        out.append(f"{n:09.4f} {x:07.2f} {y:07.2f} 000.000000 +00.000000 "
                   f"037.000000 +041.000000 000001000 +01.00 000080 10.00 000000")
    return out


def _ftp(path: Path, blocks: list[list[str]]) -> Path:
    head = ["Meteor Count = %06d" % len(blocks), "-----", "Processed with RMS", "",
            "FF  folder = x", "CAL folder = x", "",
            "Cam# Meteor# #Segments fps hnr mle bin Pix/fm Rho Phi",
            "Per segment:  Frame# Col Row RA Dec Azim Elev Inten Mag"]
    path.write_text("\n".join(head + [ln for b in blocks for ln in b]) + "\n")
    return path


METEOR = [(100 + i, 250.0 + 6 * i, 200.0 + 9 * i) for i in range(20)]
FF = f"FF_GH662S_{NIGHT}_010021_500_0000000.fits"


class TestFtpdetect:
    def test_frames_and_fps_are_kept(self, tmp_path):
        det, = sp.parse_ftpdetect(_ftp(tmp_path / "f.txt", [_block(FF, METEOR)]))
        assert det["fps"] == 25.0
        assert det["frames"][0][:3] == [100.0, 250.0, 200.0]
        assert len(det["frames"]) == 20

    def test_detection_time_counts_frames_from_the_ff_start(self, tmp_path):
        det, = sp.parse_ftpdetect(_ftp(tmp_path / "f.txt", [_block(FF, METEOR)]))
        t0, t1 = sp.detection_utc(det)
        from datetime import datetime, timezone
        ff0 = datetime(2026, 9, 26, 1, 0, 21, 500000, tzinfo=timezone.utc).timestamp()
        assert t0 == pytest.approx(ff0 + 100 / 25)
        assert t1 == pytest.approx(ff0 + 119 / 25)

    def test_the_dispersed_copy_of_a_meteor_is_not_a_second_event(self, tmp_path):
        copy = [(n, x + _dx(589.3), y + 1.0) for n, x, y in METEOR]
        other = [(10 + i, 900.0 - 5 * i, 600.0 - 4 * i) for i in range(12)]
        dets = sp.parse_ftpdetect(_ftp(tmp_path / "f.txt", [
            _block(FF, METEOR), _block(FF, copy), _block(FF, other)]))
        kept = sp.drop_dispersed_copies(dets, _model())
        assert [d["frames"][0][1] for d in kept] == [250.0, 900.0]


# ── CUT ────────────────────────────────────────────────────────────────────

@pytest.fixture
def home(tmp_path, monkeypatch):
    cap = tmp_path / "color_capture" / "GH662S" / NIGHT
    cap.mkdir(parents=True)
    for hms in ("010000", "010020", "010040"):
        (cap / f"GH662S_{NIGHT}_{hms}_color.mkv").write_bytes(b"x")

    def fake_cut(chunks, t0, t1, out):
        out.write_bytes(b"window")
        return {"file": out.name, "t0_nominal_utc": t0 - 3, "duration_s": 7.0,
                "chunks": [c.name for c in chunks]}
    monkeypatch.setattr(sp, "cut_window", fake_cut)
    monkeypatch.setattr(sp, "chunk_fps", lambda mkv: 25.0)
    return tmp_path


def _archive(home: Path, cam: str, camdir: str, blocks: list) -> None:
    d = home / "RMS_data" / camdir / "ArchivedFiles" / f"{cam}_{NIGHT}_005900_123456"
    d.mkdir(parents=True)
    _ftp(d / f"FTPdetectinfo_{cam}_{NIGHT}_005900_123456.txt", blocks)


def _config(home: Path) -> dict:
    return {"videocapture_path": str(home / "color_capture"),
            "stations": {"GH662S": {"spectro": CFG}}}


class TestCut:
    def test_an_own_detection_becomes_a_self_event_with_its_window(self, home):
        _archive(home, "GH662S", "cam5", [_block(FF, METEOR)])
        ev_dir, = sp.run_cut(_config(home), "GH662S", NIGHT, home)
        ev = json.loads((ev_dir / "event.json").read_text())
        assert ev_dir.name == f"GH662S_{NIGHT}_010025"      # meteor start, not FF start
        assert ev["trigger"] == "self"
        assert ev["fps"] == 25.0 and ev["window"]["file"] == "window.mkv"
        assert ev["chunks"] == [] and len(ev["frames"]) == 20

    def test_a_witness_of_the_same_meteor_is_dropped(self, home):
        _archive(home, "GH662S", "cam5", [_block(FF, METEOR)])
        wff = f"FF_RO000A_{NIGHT}_010022_000_0000000.fits"
        _archive(home, "RO000A", "cam1", [_block(wff, [(80 + i, 600.0, 300.0) for i in range(5)],
                                                 cam="RO000A")])
        evs = sp.run_cut(_config(home), "GH662S", NIGHT, home)
        assert [e.name for e in evs] == [f"GH662S_{NIGHT}_010025"]

    def test_slow_chunks_keep_the_legacy_whole_chunk_rescue(self, home, monkeypatch):
        monkeypatch.setattr(sp, "chunk_fps", lambda mkv: 0.5)
        _archive(home, "GH662S", "cam5", [_block(FF, METEOR)])
        ev_dir, = sp.run_cut(_config(home), "GH662S", NIGHT, home)
        ev = json.loads((ev_dir / "event.json").read_text())
        assert "window" not in ev and ev["chunks"]


# ── REDUCE ─────────────────────────────────────────────────────────────────

N_FRAMES = 30
LAG = 5    # recording frame of RMS frame 100


def _frames(meteor=True, x0=250.0, y0=200.0, lines=True, cloud=False, seed=4):
    rng = np.random.default_rng(seed)
    base = np.full((sp.FRAME_H, sp.FRAME_W, 3), 28.0, dtype=np.float32)
    out = np.empty((N_FRAMES, sp.FRAME_H, sp.FRAME_W, 3), dtype=np.uint8)
    for k in range(N_FRAMES):
        f = base + rng.normal(0, 2.0, base.shape).astype(np.float32)
        i = k - LAG
        if meteor and 0 <= i < 20:
            x, y = int(x0 + 6 * i), int(y0 + 9 * i)
            f[y - 2:y + 3, x - 2:x + 3] += 120
            if lines:
                for nm, a in ((589.3, 1.0), (517.3, 0.6)):
                    xd = x + _dx(nm)
                    if xd + 3 < sp.FRAME_W:
                        f[y - 2:y + 3, xd - 2:xd + 3] += 70 * a
        if cloud:
            cx = 200 + 10 * k
            f[150:500, cx:cx + 300] += 40
        out[k] = np.clip(f, 0, 255).astype(np.uint8)
    return out


def _event(tmp_path: Path, trigger="self", x0=250.0, y0=200.0, track=None) -> Path:
    ev = tmp_path / "EV"
    ev.mkdir()
    (ev / "window.mkv").write_bytes(b"w")
    rows = [[100.0 + i, x0 + 6 * i, y0 + 9 * i, 1000.0] for i in range(20)]
    body = {"event_id": "GH662S_20260926_010025", "night": NIGHT, "trigger": trigger,
            "fps": 25.0, "window": {"file": "window.mkv"}, "chunks": [],
            "track_px": track or [[rows[0][1], rows[0][2]], [rows[-1][1], rows[-1][2]]]}
    if trigger == "self":
        body["frames"] = rows
    (ev / "event.json").write_text(json.dumps(body))
    return ev


def _peaks(ev: Path, n=2) -> list[float]:
    a = np.loadtxt(ev / "spectrum.csv", delimiter=",", skiprows=1)
    out = []
    for lam in a[np.argsort(a[:, 1])[::-1], 0]:
        if all(abs(lam - p) > 15 for p in out):
            out.append(lam)
        if len(out) == n:
            break
    return out


class TestReduceVideo:
    def test_own_detection_gives_the_emission_lines(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sp, "decode_window", lambda mkv: _frames())
        ev = _event(tmp_path)
        res = sp.reduce_event(ev, _model())
        assert res["detected"] and res["spectrum"]
        assert res["frame_offset"] == LAG and res["n_frames"] == 20
        p1, p2 = _peaks(ev)
        assert p1 == pytest.approx(589.3, abs=4) and p2 == pytest.approx(517.3, abs=4)

    def test_a_meteor_near_the_right_edge_is_labelled_off_sensor(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sp, "decode_window",
                            lambda mkv: _frames(x0=1060.0, y0=150.0, lines=False))
        res = sp.reduce_event(_event(tmp_path, x0=1060.0, y0=150.0), _model())
        assert res["detected"] and not res["spectrum"]
        assert "outside the frame" in res["note"]

    def test_a_moving_cloud_without_a_meteor_is_not_detected(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sp, "decode_window",
                            lambda mkv: _frames(meteor=False, cloud=True))
        res = sp.reduce_event(_event(tmp_path), _model())
        assert res["detected"] is False

    def test_a_witness_trigger_is_traced_near_its_projected_track(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sp, "decode_window", lambda mkv: _frames())
        off_track = [[250.0 + 20, 200.0 - 13], [250.0 + 6 * 19 + 20, 200.0 + 9 * 19 - 13]]
        res = sp.reduce_event(_event(tmp_path, trigger="rms", track=off_track), _model())
        assert res["detected"] and res["spectrum"]
        assert res["n_frames"] >= 15

    def test_a_flash_in_place_on_the_track_is_not_a_meteor(self, tmp_path, monkeypatch):
        frames = _frames(meteor=False)
        frames[10:13, 300:303, 250:600] = 200          # a line lighting up, not moving
        monkeypatch.setattr(sp, "decode_window", lambda mkv: frames)
        track = [[250.0, 301.0], [600.0, 301.0]]
        res = sp.reduce_event(_event(tmp_path, trigger="rms", track=track), _model())
        assert res["detected"] is False

    def test_legacy_events_still_take_the_long_exposure_path(self, tmp_path, monkeypatch):
        called = []
        monkeypatch.setattr(sp, "reduce_video_event", lambda *a: called.append(1) or {})
        monkeypatch.setattr(sp, "extract_frames", lambda mkv, out: [])
        ev = tmp_path / "OLD"
        ev.mkdir()
        (ev / "event.json").write_text(json.dumps({
            "event_id": "RO000A_20260924_202033", "track_px": [[1, 1], [2, 2]],
            "chunks": []}))
        sp.reduce_event(ev, _model())
        assert called == []
