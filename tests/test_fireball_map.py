"""Ground-track map of the fireball alert: zoom choice, drawing without network, alert glue."""
from __future__ import annotations

import io
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "dashboard"))

import alert_channels as ac  # noqa: E402
import fireball_alert as fa  # noqa: E402
import fireball_map as fm  # noqa: E402

T = 1_792_613_794.0  # 2026-10-21 20:16:34 UTC
INFO = {
    "RO0003": fa.CamInfo("gmnro02", "Bistrița", 47.13, 24.50),
    "RO0007": fa.CamInfo("gmnro02", "Bistrița", 47.13, 24.50),
    "RO000P": fa.CamInfo("gmnro07", "Cluj", 46.47, 23.19),
}


def _blank_tile(z: int, x: int, y: int) -> bytes | None:
    from PIL import Image
    if (x + y) % 5 == 0:
        return None          # a missing tile must not break the mosaic
    buf = io.BytesIO()
    Image.new("RGB", (256, 256), (240, 236, 228)).save(buf, format="PNG")
    return buf.getvalue()


def _pair(a: str, b: str, ll_a, ll_b, h_a, h_b) -> dict:
    return {"t": T, "a": {"cam": a, "id": a + "_1", "n250_sum": 9000}, "b": {"cam": b, "id": b + "_1", "n250_sum": 7000},
            "geo": {"ok": True, "ll_a": ll_a, "ll_b": ll_b, "h_a": h_a, "h_b": h_b, "v": [17.0, 16.0]}}


def test_choose_zoom_fits_limits() -> None:
    z, (x0, y0, x1, y1) = fm.choose_zoom([46.65, 46.82, 47.13], [22.82, 23.12, 24.50])
    assert fm.ZOOM_MIN <= z <= fm.ZOOM_MAX
    assert (x1 - x0) * fm.TILE <= fm.MAX_W and (y1 - y0) * fm.TILE <= fm.MAX_H
    # a single point still gets a sensible box
    z1, _ = fm.choose_zoom([46.7], [23.0])
    assert z1 <= fm.ZOOM_MAX


def test_render_is_jpeg_without_network() -> None:
    tracks = [{"label": "RO0007 (Bistrița)", "begin": (46.65, 22.82), "end": (46.82, 23.12), "h_begin": 116.0, "h_end": 29.0},
              {"label": "RO000P (Cluj)", "begin": (46.67, 22.90), "end": (46.80, 23.07), "h_begin": 102.0, "h_end": 29.0}]
    cams = [("RO0007 (Bistrița)", 47.13, 24.50), ("RO000P (Cluj)", 46.47, 23.19)]
    jpg = fm.render_track_map(tracks, cams, "Fireball test", fetch_tile=_blank_tile)
    assert jpg[:2] == b"\xff\xd8" and len(jpg) > 10_000
    from PIL import Image
    w, h = Image.open(io.BytesIO(jpg)).size
    assert w <= fm.MAX_W + 2 and h <= fm.MAX_H + 2


def test_overview_frames_the_country_and_widens_for_germany() -> None:
    tracks = [{"label": "RO0007 (Bistrița)", "begin": (46.65, 22.82), "end": (46.82, 23.12), "h_begin": 116.0, "h_end": 29.0}]
    cams = [("RO0007 (Bistrița)", 47.13, 24.50)]
    jpg = fm.render_track_map(tracks, cams, "Fireball test", fetch_tile=_blank_tile, overview=True)
    assert jpg[:2] == b"\xff\xd8"
    z, _ = fm.choose_zoom([46.7], [23.0], fm.overview_box([46.7], [23.0]))
    assert z <= 7
    assert fm.overview_box([46.7], [23.0]) == fm.ROMANIA_BOX
    lat0, lat1, lon0, lon1 = fm.overview_box([46.7, 52.1], [23.0, 13.4])
    assert lat1 >= 52.3 and lon0 <= 13.2       # Johannismühle pulls the frame out to Berlin


def test_tracks_from_alert_one_per_camera_longest() -> None:
    al = fa.Alert("20261021_201634", T, T + 10)
    al.pairs = [_pair("RO0007", "RO000P", [[46.65, 22.82], [46.82, 23.12]], [[46.67, 22.90], [46.80, 23.07]],
                      [116.0, 29.0], [55.0, 29.0]),
                _pair("RO0003", "RO000P", [[46.67, 22.90], [46.80, 23.07]], [[46.60, 22.80], [46.85, 23.20]],
                      [102.0, 53.0], [60.0, 29.0])]
    tracks = {t["cam"]: t for t in fa.tracks_from_alert(al, INFO)}
    assert set(tracks) == {"RO0007", "RO000P", "RO0003"}
    assert tracks["RO000P"]["h_begin"] == 60.0       # the longer of its two tracks
    assert tracks["RO0007"]["label"] == "RO0007 (Bistrița)"


def test_send_track_map_dry_run_and_no_pairs(monkeypatch) -> None:
    monkeypatch.setattr(fm, "_fetch_osm", _blank_tile)
    eng = fa.Engine(INFO)
    tg = ac.Broadcaster([])                                            # no channel: a dry run
    al = fa.Alert("20261021_201634", T, T + 10)
    assert fa.send_track_map(tg, al, eng, "preliminary") is False      # no pairs: nothing to draw
    al.pairs = [_pair("RO0007", "RO000P", [[46.65, 22.82], [46.82, 23.12]], [[46.67, 22.90], [46.80, 23.07]],
                      [116.0, 29.0], [55.0, 29.0])]
    maps = fa.render_alert_maps(al, eng, "preliminary")
    assert len(maps) == 2 and all(m[:2] == b"\xff\xd8" for m in maps)
    assert fa.send_track_map(tg, al, eng, "confirmed") is False        # dry run sends nothing, raises nothing
