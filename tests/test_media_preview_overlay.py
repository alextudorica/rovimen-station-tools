"""The dashboard clip-preview builds its overlay through the SHARED builder.

routes.media._build_crop_cmd must not hand-roll its own logo compositing:
the burned-in clips use overlay.build_branding_overlay (centred watermark +
corner GMN logo, text_scale). If the preview drifts back to the old single
top-left logo layout, the dashboard would show operators an overlay that does
not match what actually ships.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

import overlay as _overlay
import routes.media as media


_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
)


def _system_font() -> str | None:
    return next((p for p in _FONT_CANDIDATES if Path(p).exists()), None)


@pytest.fixture
def real_overlay_mod(monkeypatch):
    """Point routes.media at the real overlay module (bundle path is absent in CI)."""
    monkeypatch.setattr(media, "_overlay_mod", _overlay)
    return _overlay


@pytest.fixture
def overlay_cfg(tmp_path):
    font = tmp_path / "font.ttf"
    font.write_bytes(b"")  # existence check only; PIL falls back to estimate
    wm = tmp_path / "astromania.png"
    gmn = tmp_path / "gmn.png"
    wm.write_bytes(b"\x89PNG\r\n\x1a\n")
    gmn.write_bytes(b"\x89PNG\r\n\x1a\n")
    return {
        "enabled": True,
        "font": str(font),
        "font_size": 19,
        "network": "ROVIMEN",
        "style": "cinema",
        "logo": str(wm),
        "gmn_logo": str(gmn),
        "show_logo": True,
    }


def _call(overlay_cfg, out_path="out.mp4", **kw):
    return media._build_crop_cmd(
        in_path="in.mkv", out_path=out_path,
        x=0.1, y=0.1, w=0.5, h=0.5,
        station_code="RO000H", filename="RO000H_20260101_210000_color.mkv",
        ss=1.0, t=3.0,
        overlay_cfg=overlay_cfg, station_cfg={"az": 180.0, "alt": 45.0},
        **kw,
    )


class TestPreviewUsesSharedBrandingBuilder:
    def test_centred_watermark_not_old_top_left_logo(self, real_overlay_mod, overlay_cfg):
        cmd = _call(overlay_cfg)
        assert "-filter_complex" in cmd
        fc = cmd[cmd.index("-filter_complex") + 1]
        # Bottom-bar branding from build_branding_overlay (centred horizontally,
        # bottom-anchored), never a watermark over the video centre, and not the
        # old single logo pinned next to a top-left wordmark.
        assert "overlay=x=(W-w)/2:y=H-h-4" in fc
        assert "(H-h)/2" not in fc
        # GMN corner (bottom-right default) is present.
        assert "W-w-14" in fc
        assert fc.rstrip().endswith("[out]")

    def test_both_logo_inputs_appended(self, real_overlay_mod, overlay_cfg):
        cmd = _call(overlay_cfg)
        # Three -i inputs beyond the source: ROVIMEN logo, watermark, GMN.
        assert cmd.count("-i") == 4  # source + 3 logos
        assert str(real_overlay_mod.NETWORK_MARK_PATH) in cmd
        assert overlay_cfg["logo"] in cmd
        assert overlay_cfg["gmn_logo"] in cmd

    def test_no_logos_falls_back_to_vf(self, real_overlay_mod, overlay_cfg):
        overlay_cfg = {**overlay_cfg, "show_logo": False, "gmn_logo": None, "show_network": False}
        cmd = _call(overlay_cfg)
        # No branding inputs -> simple -vf crop/scale+drawtext, no filter_complex.
        assert "-filter_complex" not in cmd
        assert "-vf" in cmd

    def test_overlay_module_missing_bare_crop(self, monkeypatch, overlay_cfg):
        monkeypatch.setattr(media, "_overlay_mod", None)
        cmd = _call(overlay_cfg)
        assert "-filter_complex" not in cmd
        vf = cmd[cmd.index("-vf") + 1]
        assert vf.startswith("crop=")

    def test_no_stale_logo_opacity_hand_rolled_scale(self, real_overlay_mod, overlay_cfg):
        # The old code hand-rolled a top-left logo sized to text height and keyed
        # its alpha off `logo_opacity`. The shared builder instead composites the
        # astromania logo bottom-anchored in the bar and the GMN logo in a corner.
        cmd = _call(overlay_cfg)
        fc = cmd[cmd.index("-filter_complex") + 1]
        assert "overlay=x=(W-w)/2:y=H-h-4" in fc  # bar logo, bottom-anchored
        assert "W-w-14" in fc                       # GMN corner, not a top-left logo
        assert "(H-h)/2" not in fc                  # nothing over the video centre

    @pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
    def test_ffmpeg_accepts_the_preview_filtergraph(self, real_overlay_mod, tmp_path):
        """The shared-builder filtergraph must parse: render one frame."""
        font = _system_font()
        if font is None:
            pytest.skip("no usable system font")
        # Real PNG logos so ffmpeg can decode them.
        wm = tmp_path / "wm.png"
        gmn = tmp_path / "gmn.png"
        for p in (wm, gmn):
            subprocess.run(
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                 "-f", "lavfi", "-i", "color=white:s=64x24:d=1",
                 "-frames:v", "1", str(p)],
                check=True,
            )
        src = tmp_path / "RO000H_20260101_210000_color.mkv"
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "color=0x111111:s=1920x1080:r=5:d=1",
             "-c:v", "libx264", "-t", "1", str(src)],
            check=True,
        )
        out = tmp_path / "preview.mp4"
        cfg = {
            "enabled": True, "font": font, "font_size": 19,
            "network": "ROVIMEN", "style": "cinema",
            "logo": str(wm), "gmn_logo": str(gmn), "show_logo": True,
            "text_opacity": 1.0,
        }
        cmd = media._build_crop_cmd(
            in_path=str(src), out_path=str(out),
            x=0.0, y=0.0, w=1.0, h=1.0,
            station_code="RO000H",
            filename="RO000H_20260101_210000_color.mkv",
            ss=0.0, t=0.5,
            overlay_cfg=cfg, station_cfg={"az": 180.0, "alt": 45.0},
        )
        proc = subprocess.run(cmd, capture_output=True, text=True)
        assert proc.returncode == 0, f"ffmpeg rejected preview graph: {proc.stderr[-500:]}"
        assert out.exists() and out.stat().st_size > 0


def test_crop_keeps_the_selection_aspect():
    """A non-16:9 selection is letterboxed into 1280x720, never stretched."""
    import routes.media as media
    cmd = media._build_crop_cmd(
        "in.mkv", "out.mp4", 0.1, 0.1, 0.3, 0.8, "RO0017",
        "RO0017_20260709_214233_color.mkv", 0.0, 5.0, {}, {},
    )
    vf = cmd[cmd.index("-vf") + 1] if "-vf" in cmd else cmd[cmd.index("-filter_complex") + 1]
    assert "force_original_aspect_ratio=decrease" in vf
    assert "pad=1280:720" in vf
