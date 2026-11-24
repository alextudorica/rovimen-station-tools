"""The station /api/overlay_preview renders through the SHARED overlay builder.

The admin "overlay preview" must match the overlay actually burned into clips.
That means it goes through overlay.build_drawtext_annotations (centred wordmark,
text_scale) + overlay.build_branding_overlay (centred watermark, corner GMN),
never a hand-rolled top-left single-logo layout. These tests intercept the
ffmpeg call and assert the composed command, then (when ffmpeg is present)
render one real frame to prove the graph parses.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import station_api


_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
)


def _system_font() -> str | None:
    return next((p for p in _FONT_CANDIDATES if Path(p).exists()), None)


@pytest.fixture()
def client():
    return station_api.app.test_client()


@pytest.fixture()
def cfg(tmp_path):
    font = tmp_path / "font.ttf"
    font.write_bytes(b"")  # existence check only
    wm = tmp_path / "astromania.png"
    gmn = tmp_path / "gmn.png"
    wm.write_bytes(b"\x89PNG\r\n\x1a\n")
    gmn.write_bytes(b"\x89PNG\r\n\x1a\n")
    return {
        "stations": {"RO000H": {}},
        "overlay": {
            "enabled": True,
            "font": str(font),
            "font_size": 19,
            "network": "ROVIMEN",
            "style": "cinema",
            "logo": str(wm),
            "gmn_logo": str(gmn),
            "show_logo": True,
        },
    }


class _FakeRun:
    """Capture the ffmpeg argv and pretend the render succeeded."""

    def __init__(self):
        self.cmd = None

    def __call__(self, cmd, *a, **kw):
        self.cmd = cmd
        # Write a byte to the output path (last arg) so the route can read it.
        Path(cmd[-1]).write_bytes(b"\x89PNG")

        class _R:
            returncode = 0
            stderr = b""
        return _R()


class TestOverlayPreviewUsesSharedBuilder:
    def test_missing_font_returns_400(self, client, cfg):
        cfg["overlay"]["font"] = "/no/such/font.ttf"
        with patch.object(station_api, "load_config", return_value=cfg):
            r = client.post("/api/overlay_preview", json={})
        assert r.status_code == 400

    def test_centred_watermark_and_corner_gmn_in_cmd(self, client, cfg):
        fake = _FakeRun()
        with patch.object(station_api, "load_config", return_value=cfg), \
             patch.object(station_api.subprocess, "run", fake):
            r = client.post("/api/overlay_preview", json={})
        assert r.status_code == 200
        assert "-filter_complex" in fake.cmd
        fc = fake.cmd[fake.cmd.index("-filter_complex") + 1]
        # Bottom-bar branding from the shared builder (centred, bottom-anchored),
        # not a top-left logo and not a watermark over the video centre.
        assert "overlay=x=(W-w)/2:y=H-h-4" in fc
        assert "(H-h)/2" not in fc
        # GMN corner (bottom-right default, margin 14).
        assert "W-w-14" in fc
        assert fc.rstrip().endswith("[out]")

    def test_text_scale_applied_via_shared_builder(self, client, cfg):
        # effective_font_size scales by text_scale=1.5, so 19px -> round(28.5)=28
        # (banker's rounding). The point is the drawn size is NOT the raw 19.
        fake = _FakeRun()
        with patch.object(station_api, "load_config", return_value=cfg), \
             patch.object(station_api.subprocess, "run", fake):
            client.post("/api/overlay_preview", json={})
        fc = fake.cmd[fake.cmd.index("-filter_complex") + 1]
        assert "fontsize=28" in fc   # 19 scaled by text_scale, not the raw 19
        assert "fontsize=19" not in fc

    def test_logos_appended_as_inputs(self, client, cfg):
        fake = _FakeRun()
        with patch.object(station_api, "load_config", return_value=cfg), \
             patch.object(station_api.subprocess, "run", fake):
            client.post("/api/overlay_preview", json={})
        # source (lavfi) + ROVIMEN logo + watermark + gmn = four -i
        assert fake.cmd.count("-i") == 4
        assert cfg["overlay"]["logo"] in fake.cmd
        assert cfg["overlay"]["gmn_logo"] in fake.cmd

    def test_no_logos_falls_back_to_vf(self, client, cfg):
        cfg["overlay"]["show_logo"] = False
        cfg["overlay"]["gmn_logo"] = None
        cfg["overlay"]["show_network"] = False
        fake = _FakeRun()
        with patch.object(station_api, "load_config", return_value=cfg), \
             patch.object(station_api.subprocess, "run", fake):
            client.post("/api/overlay_preview", json={})
        assert "-filter_complex" not in fake.cmd
        assert "-vf" in fake.cmd

    def test_no_hand_rolled_logo_opacity_scale(self, client, cfg):
        fake = _FakeRun()
        with patch.object(station_api, "load_config", return_value=cfg), \
             patch.object(station_api.subprocess, "run", fake):
            client.post("/api/overlay_preview", json={})
        fc = fake.cmd[fake.cmd.index("-filter_complex") + 1]
        # The shared builder composites the astromania logo bottom-anchored in
        # the bar and the GMN logo in a corner, replacing the old hand-rolled
        # top-left logo whose alpha came from `logo_opacity`.
        assert "overlay=x=(W-w)/2:y=H-h-4" in fc  # bar logo, bottom-anchored
        assert "W-w-14" in fc                       # GMN corner
        assert "(H-h)/2" not in fc                  # nothing over the video centre

    @pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
    def test_ffmpeg_accepts_the_preview_graph(self, client, tmp_path):
        font = _system_font()
        if font is None:
            pytest.skip("no usable system font")
        wm = tmp_path / "wm.png"
        gmn = tmp_path / "gmn.png"
        for p in (wm, gmn):
            subprocess.run(
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                 "-f", "lavfi", "-i", "color=white:s=64x24:d=1",
                 "-frames:v", "1", str(p)],
                check=True,
            )
        cfg = {
            "stations": {"RO000H": {}},
            "overlay": {
                "enabled": True, "font": font, "font_size": 19,
                "network": "ROVIMEN", "style": "cinema",
                "logo": str(wm), "gmn_logo": str(gmn), "show_logo": True,
                "text_opacity": 1.0,
            },
        }
        with patch.object(station_api, "load_config", return_value=cfg):
            r = client.post("/api/overlay_preview", json={"az": 180.0, "alt": 45.0})
        assert r.status_code == 200, r.get_data(as_text=True)[:400]
        assert r.mimetype == "image/png"
        assert len(r.get_data()) > 0


class TestPreviewBodyCannotReadFiles:
    """The request body changes how the preview looks, never what it reads.

    The body used to be merged over the whole overlay config, and ``coords``
    reached the drawtext filter raw, so a network caller could append
    ``drawtext=textfile=<any file>`` and get the file back in the PNG; ``font``,
    ``logo`` and ``gmn_logo`` named files ffmpeg would open."""

    def _fc(self, client, cfg, body):
        fake = _FakeRun()
        with patch.object(station_api, "load_config", return_value=cfg), \
             patch.object(station_api.subprocess, "run", fake):
            r = client.post("/api/overlay_preview", json=body)
        assert r.status_code == 200, r.get_data(as_text=True)
        flag = "-filter_complex" if "-filter_complex" in fake.cmd else "-vf"
        return fake.cmd, fake.cmd[fake.cmd.index(flag) + 1]

    def test_text_and_paths_come_from_the_config(self, client, cfg, tmp_path):
        cfg["overlay"]["coords"] = "45.12N  25.46E"
        evil = tmp_path / "secret"
        evil.write_text("ingest-key")
        cmd, fc = self._fc(client, cfg, {
            "coords": f"x,drawtext=textfile={evil}",
            "network": f"y,drawtext=textfile={evil}",
            "font": str(evil),
            "logo": str(evil),
            "gmn_logo": str(evil),
        })
        assert str(evil) not in " ".join(cmd)
        assert "textfile" not in fc
        assert "text='45.12N  25.46E'" in fc
        assert f"fontfile={cfg['overlay']['font']}" in fc
        assert cfg["overlay"]["logo"] in cmd and cfg["overlay"]["gmn_logo"] in cmd

    def test_display_keys_still_apply(self, client, cfg):
        _cmd, fc = self._fc(client, cfg, {
            "style": "standard", "font_size": 30, "show_coords": False,
            "show_station": True, "text_opacity": 0.5,
        })
        assert "pad=" not in fc                      # standard: no bar
        assert "fontsize=45" in fc                   # 30 x text_scale 1.5
        assert "RO000H" in fc
        assert "fontcolor=white@0.50" in fc

    def test_a_logo_from_the_assets_dir_previews_before_it_is_saved(
            self, client, cfg, tmp_path, monkeypatch):
        assets = tmp_path / "assets"
        assets.mkdir()
        picked = assets / "sternfreunde_berlin.png"
        picked.write_bytes(b"\x89PNG\r\n\x1a\n")
        monkeypatch.setattr(station_api, "_ASSETS_DIR", assets)
        cmd, _fc = self._fc(client, cfg, {"logo": str(picked)})
        assert str(picked) in cmd
        assert cfg["overlay"]["logo"] not in cmd

    @pytest.mark.parametrize("key", ["font_size", "text_opacity", "bar_logo_opacity"])
    def test_a_non_numeric_display_value_is_a_400(self, client, cfg, key):
        with patch.object(station_api, "load_config", return_value=cfg):
            r = client.post("/api/overlay_preview", json={key: "big"})
        assert r.status_code == 400
        assert key in r.get_json()["error"]
