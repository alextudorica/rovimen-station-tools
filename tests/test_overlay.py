"""Tests for overlay.py -- OSD annotation utilities."""

from __future__ import annotations

import hashlib
import inspect
import math
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import overlay
from overlay import (
    BAR_LOGO_HEIGHT_FRAC,
    BAR_LOGO_MARGIN,
    BAR_LOGO_MAX_HEIGHT_FRAC,
    BAR_LOGO_MAX_WIDTH_FRAC,
    BAR_LOGO_ASSET_SCALE,
    BAR_GAP,
    BAR_MARGIN,
    BAR_LOGO_OPACITY,
    BAR_LOGO_REF_ASPECT,
    CODE_OWNED_OVERLAY_KEYS,
    INERT_OVERLAY_KEYS,
    GMN_LOGO_CORNER,
    NETWORK_TEXT_SCALE,
    STANDARD_LOGO_WIDTH_FRAC,
    TEXT_SCALE,
    _standard_logo_scale_expr,
    bar_logo_aspect,
    bar_logo_headroom,
    bar_logo_opacity,
    bar_logo_scale_expr,
    build_branding_overlay,
    build_drawtext_annotations,
    effective_font_size,
    gmn_logo_spec,
    measure_text_height,
    measure_text_width,
    network_font_size,
    relabel_fragment_output,
)

_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
)


def _system_font() -> str | None:
    """First real TTF on this host, or None. The fake font fixture renders nothing."""
    return next((p for p in _FONT_CANDIDATES if Path(p).exists()), None)


@pytest.fixture
def make_overlay_cfg(tmp_path):
    """Return a factory that produces a valid overlay config dict.

    Creates a temporary empty file to satisfy the font-path existence check.
    PIL font loading will fail gracefully, triggering the monospace fallback.
    """
    font_file = tmp_path / "fake_font.ttf"
    font_file.write_bytes(b"")

    def _make(**overrides) -> dict:
        base = {
            "font": str(font_file),
            "font_size": 20,
            "text_opacity": 0.4,
            "coords": "45.0N 25.0E",
            "network": "ROVIMEN",
            "style": "standard",
            "show_logo": False,
            "show_station": True,
            "show_coords": True,
            "show_pointing": True,
            "show_timestamp": True,
            "show_network": True,
            "az": 180.0,
            "alt": 45.0,
        }
        base.update(overrides)
        return base

    return _make


# ---- measure_text_width ----


class TestMeasureTextWidth:
    def test_fallback_with_nonexistent_font(self):
        """Non-existent font triggers the monospace estimate."""
        text = "Hello"
        result = measure_text_width("/no/such/font.ttf", 20, text)
        assert result == int(20 * 0.65 * len(text))

    def test_monospace_estimate_math(self):
        """Verify the exact monospace formula: font_size * 0.65 * len(text)."""
        assert measure_text_width("/no/such/font.ttf", 30, "ABC") == int(30 * 0.65 * 3)
        assert measure_text_width("/no/such/font.ttf", 10, "X") == int(10 * 0.65 * 1)
        assert measure_text_width("/no/such/font.ttf", 40, "ABCDEFGHIJ") == int(40 * 0.65 * 10)

    def test_longer_text_wider(self):
        """Longer text must produce a wider measurement."""
        short = measure_text_width("/no/such/font.ttf", 20, "Hi")
        long_ = measure_text_width("/no/such/font.ttf", 20, "Hello World")
        assert long_ > short

    def test_empty_text_returns_zero(self):
        """Empty string should measure as 0 width."""
        assert measure_text_width("/no/such/font.ttf", 20, "") == 0


# ---- measure_text_height ----


class TestMeasureTextHeight:
    def test_fallback_with_nonexistent_font(self):
        """Non-existent font falls back to font_size."""
        assert measure_text_height("/no/such/font.ttf", 24) == 24

    def test_returns_font_size_as_fallback(self):
        """Multiple font sizes all fall back correctly."""
        for size in (10, 19, 32, 64):
            assert measure_text_height("/no/such/font.ttf", size) == size


# ---- build_drawtext_annotations ----


class TestBuildDrawtextAnnotations:
    STATION_ID = "RO000H"
    CHUNK_EPOCH = 1717200000

    def test_missing_font_returns_empty(self):
        """When the font file does not exist, return ([], 0)."""
        cfg = {"font": "/no/such/font.ttf", "font_size": 20, "style": "standard"}
        filters, bar_h = build_drawtext_annotations(cfg, self.STATION_ID, {}, self.CHUNK_EPOCH)
        assert filters == []
        assert bar_h == 0

    def test_null_font_returns_empty(self):
        """config_defaults.json ships "font": null — an explicit None must
        degrade to no-annotation, not crash Path(None). This broke every
        encode on stations that received the overlay block via the updater
        config migration (gmnro16, 2026-08-01)."""
        cfg = {"font": None, "logo": None, "font_size": 20, "style": "cinema"}
        filters, bar_h = build_drawtext_annotations(cfg, self.STATION_ID, {}, self.CHUNK_EPOCH)
        assert filters == []
        assert bar_h == 0

    def test_standard_style_returns_filters_bar_zero(self, make_overlay_cfg):
        """Standard style produces drawtext filters with bar_h == 0."""
        cfg = make_overlay_cfg(style="standard")
        filters, bar_h = build_drawtext_annotations(cfg, self.STATION_ID, {}, self.CHUNK_EPOCH)
        assert len(filters) > 0
        assert bar_h == 0

    def test_cinema_style_returns_filters_bar_positive(self, make_overlay_cfg):
        """Cinema style produces drawtext filters with bar_h > 0."""
        cfg = make_overlay_cfg(style="cinema")
        filters, bar_h = build_drawtext_annotations(cfg, self.STATION_ID, {}, self.CHUNK_EPOCH)
        assert len(filters) > 0
        assert bar_h > 0

    def test_cinema_bar_h_aligned_to_16px(self, make_overlay_cfg):
        """Cinema bar height is always aligned to 16px boundary."""
        for font_size in (10, 19, 24, 32, 48):
            cfg = make_overlay_cfg(style="cinema", font_size=font_size)
            _, bar_h = build_drawtext_annotations(cfg, self.STATION_ID, {}, self.CHUNK_EPOCH)
            assert bar_h % 16 == 0, f"bar_h={bar_h} not aligned to 16px for font_size={font_size}"

    def test_show_network_false_excludes_network(self, make_overlay_cfg):
        """Disabling show_network removes network text from filters."""
        cfg = make_overlay_cfg(show_network=False)
        filters, _ = build_drawtext_annotations(cfg, self.STATION_ID, {}, self.CHUNK_EPOCH)
        combined = " ".join(filters)
        assert "ROVIMEN" not in combined

    def test_show_timestamp_false_excludes_timestamp(self, make_overlay_cfg):
        """Disabling show_timestamp removes the clock filter."""
        cfg = make_overlay_cfg(show_timestamp=False)
        filters, _ = build_drawtext_annotations(cfg, self.STATION_ID, {}, self.CHUNK_EPOCH)
        combined = " ".join(filters)
        assert r"pts\:gmtime\:" not in combined
        assert "UTC" not in combined

    def test_show_station_false_excludes_station_id(self, make_overlay_cfg):
        """Disabling show_station removes the station ID from filters."""
        cfg = make_overlay_cfg(show_station=False, show_coords=False)
        filters, _ = build_drawtext_annotations(cfg, self.STATION_ID, {}, self.CHUNK_EPOCH)
        combined = " ".join(filters)
        assert self.STATION_ID not in combined

    def test_show_coords_false_excludes_coordinates(self, make_overlay_cfg):
        """Disabling show_coords removes coordinate text from filters."""
        cfg = make_overlay_cfg(show_coords=False, show_station=False)
        filters, _ = build_drawtext_annotations(cfg, self.STATION_ID, {}, self.CHUNK_EPOCH)
        combined = " ".join(filters)
        assert "45.0N 25.0E" not in combined

    def test_show_pointing_false_excludes_az_alt(self, make_overlay_cfg):
        """Disabling show_pointing removes az/alt text from filters."""
        cfg = make_overlay_cfg(show_pointing=False)
        filters, _ = build_drawtext_annotations(cfg, self.STATION_ID, {}, self.CHUNK_EPOCH)
        combined = " ".join(filters)
        assert "ALT" not in combined
        assert "AZ" not in combined

    def test_all_show_flags_false_returns_empty(self, make_overlay_cfg):
        """With all show flags disabled, no filters are generated."""
        cfg = make_overlay_cfg(
            show_network=False,
            show_station=False,
            show_coords=False,
            show_pointing=False,
            show_timestamp=False,
        )
        filters, bar_h = build_drawtext_annotations(cfg, self.STATION_ID, {}, self.CHUNK_EPOCH)
        assert filters == []

    def test_filters_contain_font_path_and_scaled_size(self, make_overlay_cfg):
        """Data lines use the text_scale size; the wordmark uses its own larger one."""
        cfg = make_overlay_cfg(font_size=24, text_scale=1.5)
        filters, _ = build_drawtext_annotations(cfg, self.STATION_ID, {}, self.CHUNK_EPOCH)
        font_path = cfg["font"]
        net_size = network_font_size(cfg)  # round(36 * 5/3)
        assert net_size > 36, "the wordmark must outrank the data columns"
        for f in filters:
            assert f"fontfile={font_path}" in f
            expected = net_size if ":text='ROVIMEN':" in f else 36
            assert f"fontsize={expected}" in f

    def test_stale_text_scale_in_config_is_ignored(self, make_overlay_cfg):
        """text_scale is code-owned: a config copy must not change the drawn size.

        Every station carried text_scale=1.5 merged in from config_defaults.json.
        It happened to equal TEXT_SCALE, so it hid the same shadowing bug that
        bar_logo_height_frac made visible — the next edit to TEXT_SCALE would
        have gone inert fleet-wide.
        """
        cfg = make_overlay_cfg(font_size=24, text_scale=1.0)
        filters, _ = build_drawtext_annotations(cfg, self.STATION_ID, {}, self.CHUNK_EPOCH)
        net_size = network_font_size(cfg)
        drawn = round(24 * TEXT_SCALE)
        for f in filters:
            expected = net_size if ":text='ROVIMEN':" in f else drawn
            assert f"fontsize={expected}" in f

    def test_stale_network_text_scale_in_config_is_ignored(self, make_overlay_cfg):
        """The wordmark scale is code-owned too."""
        cfg = make_overlay_cfg(font_size=24, text_scale=1.0, network_text_scale=2.0)
        filters, _ = build_drawtext_annotations(cfg, self.STATION_ID, {}, self.CHUNK_EPOCH)
        wordmark = [f for f in filters if ":text='ROVIMEN':" in f]
        expected = round(round(24 * TEXT_SCALE) * NETWORK_TEXT_SCALE)
        assert wordmark and f"fontsize={expected}" in wordmark[0]

    def test_station_cfg_az_alt_overrides_overlay(self, make_overlay_cfg):
        """station_cfg az/alt take precedence over overlay_cfg values."""
        cfg = make_overlay_cfg(az=100.0, alt=30.0)
        station_cfg = {"az": 270.5, "alt": 55.3}
        filters, _ = build_drawtext_annotations(cfg, self.STATION_ID, station_cfg, self.CHUNK_EPOCH)
        combined = " ".join(filters)
        assert "ALT 55.3" in combined
        assert "AZ 270.5" in combined
        # Verify the overlay_cfg values are NOT present
        assert "ALT 30.0" not in combined
        assert "AZ 100.0" not in combined

    def test_chunk_epoch_in_timestamp_filter(self, make_overlay_cfg):
        """The chunk_epoch appears as the gmtime offset in the timestamp filter."""
        epoch = 1717200000
        cfg = make_overlay_cfg()
        filters, _ = build_drawtext_annotations(cfg, self.STATION_ID, {}, epoch)
        combined = " ".join(filters)
        assert rf"gmtime\:{epoch}\:" in combined

    def test_text_opacity_in_fontcolor(self, make_overlay_cfg):
        """The text_opacity value appears as alpha in fontcolor."""
        cfg = make_overlay_cfg(text_opacity=0.7)
        filters, _ = build_drawtext_annotations(cfg, self.STATION_ID, {}, self.CHUNK_EPOCH)
        for f in filters:
            assert "fontcolor=white@0.70" in f

    # -- Cinema-specific tests --

    def test_cinema_show_network_false(self, make_overlay_cfg):
        """Cinema style with show_network=False excludes network text."""
        cfg = make_overlay_cfg(style="cinema", show_network=False)
        filters, bar_h = build_drawtext_annotations(cfg, self.STATION_ID, {}, self.CHUNK_EPOCH)
        combined = " ".join(filters)
        assert "ROVIMEN" not in combined
        assert bar_h > 0

    def test_cinema_all_flags_false_returns_empty(self, make_overlay_cfg):
        """Cinema style with all show flags disabled returns empty filters."""
        cfg = make_overlay_cfg(
            style="cinema",
            show_network=False,
            show_station=False,
            show_coords=False,
            show_pointing=False,
            show_timestamp=False,
        )
        filters, bar_h = build_drawtext_annotations(cfg, self.STATION_ID, {}, self.CHUNK_EPOCH)
        assert filters == []

    def test_standard_network_centred_at_bottom(self, make_overlay_cfg):
        """In standard style, the wordmark is horizontally centred at the BOTTOM
        (never mid-frame over the meteor)."""
        cfg = make_overlay_cfg(style="standard")
        filters, _ = build_drawtext_annotations(cfg, self.STATION_ID, {}, self.CHUNK_EPOCH)
        network_filters = [f for f in filters if "ROVIMEN" in f]
        assert len(network_filters) == 1
        assert ":x=(w-tw)/2:" in network_filters[0]
        # Anchored to the bottom edge, not y=MARGIN at the top.
        assert ":y=h-14-2*th-10" in network_filters[0]
        assert ":y=14" not in network_filters[0]

    def test_cinema_network_centred_in_bar(self, make_overlay_cfg, monkeypatch):
        """Without the logo asset, the cinema text wordmark is centred in the bar."""
        import overlay
        monkeypatch.setattr(overlay, "NETWORK_MARK_PATH", Path("/nonexistent/rovimen_mark.png"))
        cfg = make_overlay_cfg(style="cinema")
        filters, _ = build_drawtext_annotations(cfg, self.STATION_ID, {}, self.CHUNK_EPOCH)
        network_filters = [f for f in filters if "ROVIMEN" in f]
        assert len(network_filters) == 1
        assert ":x=(w-tw)/2:" in network_filters[0]

    def test_cinema_network_logo_replaces_text(self, make_overlay_cfg):
        """With the shipped logo, row 0 carries the image, centred, row-tall; no text."""
        import overlay
        assert overlay.NETWORK_MARK_PATH.is_file(), "rovimen_mark.png must ship in assets/"
        cfg = make_overlay_cfg(style="cinema")
        filters, bar_h = build_drawtext_annotations(cfg, self.STATION_ID, {}, self.CHUNK_EPOCH)
        assert not [f for f in filters if "ROVIMEN" in f]
        inputs, frag = overlay.build_branding_overlay(cfg, "vmain", "out", 1, bar_h=bar_h)
        assert inputs[:2] == ["-i", str(overlay.NETWORK_MARK_PATH)]
        row0_h = max(overlay.effective_font_size(cfg), overlay.network_font_size(cfg))
        mark_h = int(round(row0_h * overlay.NETWORK_MARK_ROW_SCALE)) // 2 * 2
        cut = overlay.network_mark_crop(mark_h)
        assert f"[1:v]scale=-2:{mark_h},crop=iw:ih-{cut}:0:{cut},format=rgba" in frag
        assert f"overlay=x=(W-w)/2:y=H-{bar_h}+{overlay.NETWORK_MARK_TOP_INSET}" in frag
        assert frag.endswith("[out]")

    def test_network_logo_clears_the_operator_logo(self):
        """The ROVIMEN logo leads the bar but never touches the operator logo under it."""
        import overlay
        root = Path(__file__).resolve().parent.parent / "rovimen-scripts"
        cfg = {"font": str(root / "fonts" / "VCR_OSD_MONO_1.001.ttf"), "font_size": 19,
               "style": "cinema", "logo": str(root / "assets" / "astromania_text.png")}
        bar_h = overlay.bar_height(cfg)
        row0_h = max(overlay.effective_font_size(cfg), overlay.network_font_size(cfg))
        mark_h = int(round(row0_h * overlay.NETWORK_MARK_ROW_SCALE)) // 2 * 2
        logo_h = int(overlay.bar_logo_scale_expr(cfg, bar_h).split(":")[1])
        assert mark_h > 2 * logo_h, "the ROVIMEN logo must be the most prominent mark"
        mark_bottom = overlay.NETWORK_MARK_TOP_INSET + mark_h - overlay.network_mark_crop(mark_h)
        assert mark_bottom + overlay.BAR_GAP <= bar_h - overlay.BAR_LOGO_MARGIN - logo_h

    def test_network_mark_ink_top_matches_the_asset(self):
        """The crop constant must cut only transparent rows, and all of them."""
        import numpy as np
        from PIL import Image
        import overlay
        alpha = np.asarray(Image.open(overlay.NETWORK_MARK_PATH).convert("RGBA"))[..., 3]
        first_ink_row = int(np.nonzero((alpha > 0).any(axis=1))[0][0])
        assert first_ink_row == round(overlay.NETWORK_MARK_INK_TOP * alpha.shape[0])

    def test_other_network_name_keeps_text(self, make_overlay_cfg):
        """A station naming another network gets its own text, not the ROVIMEN logo."""
        import overlay
        cfg = make_overlay_cfg(style="cinema", network="OTHERNET")
        filters, bar_h = build_drawtext_annotations(cfg, self.STATION_ID, {}, self.CHUNK_EPOCH)
        assert [f for f in filters if ":text='OTHERNET':" in f]
        inputs, _ = overlay.build_branding_overlay(cfg, "vmain", "out", 1, bar_h=bar_h)
        assert str(overlay.NETWORK_MARK_PATH) not in inputs

    def test_standard_timestamp_at_bottom_right(self, make_overlay_cfg):
        """In standard style, timestamp is positioned at bottom-right."""
        cfg = make_overlay_cfg(style="standard")
        filters, _ = build_drawtext_annotations(cfg, self.STATION_ID, {}, self.CHUNK_EPOCH)
        ts_filters = [f for f in filters if r"pts\:gmtime\:" in f]
        assert len(ts_filters) == 1
        # Bottom-right: x uses w-MARGIN-tw, y uses h-MARGIN-th
        assert "w-14-tw" in ts_filters[0]
        assert "h-14-th" in ts_filters[0]

    def test_cinema_station_is_left_column(self, make_overlay_cfg):
        """In cinema style, the station code sits in the LEFT column of the bar."""
        cfg = make_overlay_cfg(style="cinema")
        filters, bar_h = build_drawtext_annotations(cfg, self.STATION_ID, {}, self.CHUNK_EPOCH)
        station_filters = [f for f in filters if f":text='{self.STATION_ID}'" in f]
        assert len(station_filters) == 1
        # Left-anchored at x=MARGIN, on the top bar row. That row is as tall as
        # the wordmark, so the data text is centred against it rather than sitting
        # flush at the margin.
        assert ":x=14:" in station_filters[0]
        cfg_font = effective_font_size(cfg)
        row0_h = max(cfg_font, network_font_size(cfg))
        assert f":y=h-{bar_h}+{14 + (row0_h - cfg_font) // 2}" in station_filters[0]


# ---- clock timezone ----


class TestBarHeightGrowsWithMeteorRows:
    """bar_height gained an n_meteors argument for the details pass.

    The base (n=0) height must equal what the fixed two-row bar always produced,
    since detection_lock._stitch_stacks and the live encoder both slice sky from
    bar on that value. Each meteor row then grows the bar downward.
    """

    CFG = {"style": "cinema", "font_size": 20}

    def test_default_arg_is_the_base_bar(self):
        assert overlay.bar_height(self.CFG) == overlay.bar_height(self.CFG, 0)

    def test_base_matches_the_historical_formula(self):
        """n=0 reproduces 2*MARGIN + row0_h + font_size + GAP, 16-aligned."""
        fs = effective_font_size(self.CFG)
        row0 = max(fs, network_font_size(self.CFG))
        raw = 2 * BAR_MARGIN + row0 + fs + BAR_GAP
        assert overlay.bar_height(self.CFG, 0) == (raw + 15) // 16 * 16

    def test_grows_monotonically_and_stays_16_aligned(self):
        prev = overlay.bar_height(self.CFG, 0)
        for n in (1, 2, 3, 5):
            h = overlay.bar_height(self.CFG, n)
            assert h >= prev
            assert h % 16 == 0
            prev = h

    def test_extra_height_matches_the_bar_difference(self):
        for n in (1, 2, 3):
            assert overlay.meteor_rows_extra_height(self.CFG, n) == (
                overlay.bar_height(self.CFG, n) - overlay.bar_height(self.CFG, 0)
            )

    def test_non_cinema_and_zero_have_no_bar(self):
        assert overlay.bar_height({"style": "standard"}, 3) == 0
        assert overlay.meteor_rows_extra_height({"style": "standard"}, 3) == 0
        assert overlay.meteor_rows_extra_height(self.CFG, 0) == 0

    def test_meteor_row_height_is_a_data_line_plus_gap(self):
        assert overlay.meteor_row_height(self.CFG) == (
            effective_font_size(self.CFG) + BAR_GAP
        )


class TestClockIsUtcLocked:
    """The OSD clock must render UTC on a host whose timezone is not UTC.

    ffmpeg's ``expansion=strftime`` formats ``basetime`` through ``localtime()``,
    so on the Europe/Bucharest stations every burnt-in clock read UTC+3 under a
    "UTC" label (docs/known_failure_modes.md entry 23).
    """

    STATION_ID = "RO0019"
    CHUNK_EPOCH = 1785103297  # 2026-07-26 22:01:37 UTC, the detection this fix was found on

    def _clock(self, cfg) -> str:
        filters, _ = build_drawtext_annotations(cfg, self.STATION_ID, {}, self.CHUNK_EPOCH)
        clocks = [f for f in filters if r"pts\:gmtime\:" in f]
        assert len(clocks) == 1, f"expected exactly one clock filter, got {len(clocks)}"
        return clocks[0]

    def test_no_localtime_strftime_expansion(self, make_overlay_cfg):
        """The clock must not use the localtime-based strftime expansion."""
        clock = self._clock(make_overlay_cfg())
        assert "expansion=strftime" not in clock
        assert "basetime=" not in clock

    def test_epoch_passed_to_gmtime(self, make_overlay_cfg):
        """The chunk epoch is formatted through gmtime, in seconds."""
        clock = self._clock(make_overlay_cfg())
        assert rf"gmtime\:{self.CHUNK_EPOCH}\:" in clock
        # Not the microsecond basetime the old form used.
        assert str(self.CHUNK_EPOCH * 1000000) not in clock

    def test_label_still_says_utc(self, make_overlay_cfg):
        """The rendered label stays UTC, now truthfully."""
        assert "UTC" in self._clock(make_overlay_cfg())

    def test_colons_escaped_for_filtergraph(self, make_overlay_cfg):
        """Unescaped colons inside %{...} would split the drawtext options."""
        clock = self._clock(make_overlay_cfg())
        expr = clock.split(":text=", 1)[1].split(":fontsize", 1)[0]
        assert expr.startswith("'") and expr.endswith("'"), expr
        assert ":" not in expr.replace(r"\:", ""), expr

    @pytest.mark.parametrize("style", ["standard", "cinema"])
    def test_both_styles_use_gmtime(self, make_overlay_cfg, style):
        """Cinema and standard layouts build the clock from the same helper."""
        clock = self._clock(make_overlay_cfg(style=style))
        assert r"pts\:gmtime\:" in clock

    @pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
    def test_render_identical_across_host_timezones(self, make_overlay_cfg, tmp_path):
        """The rendered pixels must not depend on the host timezone.

        String assertions above can be satisfied by a filter ffmpeg silently
        mis-parses, so render the real filter under two zones and diff the
        output. A three-hour shift changes the drawn digits and the checksum.
        """
        font = _system_font()
        if font is None:
            pytest.skip("no usable system font")
        clock = self._clock(make_overlay_cfg(font=font, font_size=24, text_opacity=1.0))

        digests = []
        for tz in ("UTC", "Europe/Bucharest"):
            out = tmp_path / f"clock_{tz.replace('/', '_')}.png"
            proc = subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=black:s=680x60:d=1",
                    "-vf", clock, "-frames:v", "1", str(out),
                ],
                env={**os.environ, "TZ": tz},
                capture_output=True,
                text=True,
            )
            assert proc.returncode == 0, f"ffmpeg rejected the filter: {proc.stderr}"
            assert out.exists()
            digests.append(hashlib.md5(out.read_bytes()).hexdigest())

        assert digests[0] == digests[1], "OSD clock shifts with the host timezone"


# ---- branding layout: text scale, centred watermark, corner GMN logo ----


class TestEffectiveFontSize:
    def test_default_scale_is_50pct_larger(self):
        """font_size is per-station; the scale is code-owned at +50%."""
        assert TEXT_SCALE == 1.5
        assert effective_font_size({"font_size": 20}) == 30
        assert effective_font_size({"font_size": 19}) == round(19 * 1.5)

    def test_font_size_still_comes_from_config(self):
        """The per-station half of the calculation must keep working."""
        assert effective_font_size({"font_size": 30}) == round(30 * TEXT_SCALE)
        assert effective_font_size({"font_size": 12}) == round(12 * TEXT_SCALE)

    def test_config_text_scale_cannot_change_the_result(self):
        for stale in (0.5, 1.0, 2.0):
            assert effective_font_size({"font_size": 20, "text_scale": stale}) == \
                effective_font_size({"font_size": 20})

    def test_missing_font_size_uses_default(self):
        assert effective_font_size({}) == round(19 * TEXT_SCALE)

    def test_never_below_one(self):
        assert effective_font_size({"font_size": 0}) == 1


class TestBrandingBalance:
    """The operator credit must stay smaller than the ROVIMEN wordmark.

    The bar slot carries whichever operator mark a station is configured with
    (Astromania, SARM, Astroclubul Bucuresti), so these fractions govern all of
    them. The mark sits beside the wordmark as a credit, not as the headline;
    pin the intent so a future tweak is deliberate.
    """

    def test_operator_logo_reads_as_a_credit(self):
        assert BAR_LOGO_HEIGHT_FRAC == pytest.approx(0.22)
        assert STANDARD_LOGO_WIDTH_FRAC == pytest.approx(0.105)

    def test_operator_logo_stays_narrower_than_a_third_of_the_frame(self):
        """A ~9.8:1 wordmark turns bar height into frame width very fast.

        The shipped mark rendered 764 px wide (60% of a 1280 px frame) and
        collided with the UTC clock. Guard the width the fraction implies.
        """
        bar_h, frame_w, aspect = 128, 1280, 2056 / 210
        logo_w = round(bar_h * BAR_LOGO_HEIGHT_FRAC) * aspect
        assert logo_w / frame_w < 0.33

    def test_wordmark_outranks_the_operator_credit(self):
        assert NETWORK_TEXT_SCALE == pytest.approx(5 / 3)
        assert NETWORK_TEXT_SCALE > 1.0

    def test_wordmark_scales_above_the_data_columns(self):
        cfg = {"font_size": 24, "text_scale": 1.5}
        assert network_font_size(cfg) > effective_font_size(cfg)


class TestGeometryIsCodeOwned:
    """No overlay geometry may be read back out of a station config.

    An ``overlay_cfg.get('<something>_frac', CONSTANT)`` read is invisible in
    review and permanently voids the constant beside it: config_defaults.json
    shipped ``bar_logo_height_frac`` = 0.62 into all 18 station configs on
    2026-08-05, and from then on #971, #975 and #981 each shrank the constant,
    passed CI, deployed, and changed nothing. These scan the module's own source,
    so the next such read fails here instead of shipping inert for two weeks.
    """

    SOURCE = inspect.getsource(overlay)

    def _config_reads(self) -> set[str]:
        return set(re.findall(
            r"overlay_cfg\.get\(\s*['\"]([a-z0-9_]+)['\"]", self.SOURCE
        ))

    def test_registered_keys_are_never_read_from_config(self):
        offenders = sorted(self._config_reads() & set(overlay.CODE_OWNED_OVERLAY_KEYS))
        assert not offenders, f"code-owned keys read from config: {offenders}"

    def test_no_scale_or_fraction_key_is_read_from_config(self):
        """Catches a NEW geometry key nobody thought to register."""
        offenders = sorted(
            k for k in self._config_reads() if k.endswith(("_frac", "_scale"))
        )
        assert not offenders, (
            f"geometry read from config: {offenders} — put the value in a module "
            "constant and add the key to CODE_OWNED_OVERLAY_KEYS instead"
        )

    def test_inert_keys_are_never_read_from_config(self):
        """A key we purge as dead must not quietly regain a reader.

        ``logo_size`` and ``logo_opacity`` are removed from the shipped defaults,
        from every station config and from the dashboard's overlay editor on the
        grounds that nothing consumes them. A new read here would make the purge
        a behaviour change instead of a cleanup.
        """
        offenders = sorted(self._config_reads() & set(overlay.INERT_OVERLAY_KEYS))
        assert not offenders, f"inert keys read from config: {offenders}"

    def test_inert_and_code_owned_do_not_overlap(self):
        """The two registries answer different questions; keep them disjoint."""
        assert not (set(overlay.INERT_OVERLAY_KEYS) & set(CODE_OWNED_OVERLAY_KEYS))

    def test_every_registered_key_has_a_constant_behind_it(self):
        """A registry entry with no constant would purge config for nothing."""
        constants = {
            n for n in dir(overlay)
            if n.isupper() and n.endswith(("_FRAC", "_SCALE"))
        }
        assert len(constants) >= len(overlay.CODE_OWNED_OVERLAY_KEYS)
        for key in overlay.CODE_OWNED_OVERLAY_KEYS:
            assert key.islower() and not key.startswith("_")


class TestLogoSizeHasOneSourceOfTruth:
    """No config key may shadow the fleet logo size.

    ``config_defaults.json`` shipped ``bar_logo_height_frac`` = 0.62 from
    2026-08-05 and config_migrate merged it into every station's config.json, so
    the 0.42 -> 0.315 -> 0.104 -> 0.052 walk of ``BAR_LOGO_HEIGHT_FRAC`` across
    four PRs changed nothing on any station: a real MD0001 clip from 2026-08-17
    still measured 764x78 px, exactly 0.62 of its 128 px bar. Pin the precedence.
    """

    def test_bar_logo_height_follows_the_constant(self):
        bar_h = 128
        expected = int(round(bar_h * BAR_LOGO_HEIGHT_FRAC)) // 2 * 2
        assert bar_logo_scale_expr({}, bar_h) == f"-2:{expected}"

    def test_stale_station_override_cannot_enlarge_the_logo(self):
        bar_h = 128
        assert bar_logo_scale_expr({"bar_logo_height_frac": 0.62}, bar_h) == \
            bar_logo_scale_expr({}, bar_h)

    def test_stale_station_override_cannot_resize_the_barless_logo(self):
        assert _standard_logo_scale_expr({"bar_logo_width_frac": 0.28}) == \
            _standard_logo_scale_expr({})

    def test_bar_logo_stays_inside_the_bar(self):
        """Height x aspect must not exceed the bar, or it clips the video above."""
        bar_h = 128
        target_h = int(bar_logo_scale_expr({}, bar_h).split(":")[1])
        assert 0 < target_h <= bar_h - 2 * BAR_LOGO_MARGIN


class TestBarLogoEqualInk:
    """Every operator's mark must read, whatever shape the artwork is.

    Sizing by bar height alone gave each mark the same HEIGHT, so its WIDTH --
    what a viewer actually reads -- came out proportional to its aspect ratio. An
    audit of the rendered bar across all 21 stations on 2026-08-25 found the fleet
    split by asset shape and nothing else: the 9.79:1 astromania wordmark
    rendered 22x215 px and read fine, while the three stations carrying a
    near-square badge (sternfreunde_berlin, ab1,
    gmn_sphere) rendered 22x22 px specks and looked un-branded with
    ``show_logo: true``. Equal-INK scaling (sqrt of the aspect ratio) fixes the
    badges without touching the wordmark, so these pin both halves.
    """

    BAR_H = 128
    _ROOT = Path(__file__).resolve().parent.parent / "rovimen-scripts"
    ASSETS = _ROOT / "assets"
    FONT = _ROOT / "fonts" / "VCR_OSD_MONO_1.001.ttf"

    def _cfg(self, logo: Path | str) -> dict:
        """A station's real overlay config: shipped font, shipped font size."""
        return {"logo": str(logo), "style": "cinema", "font": str(self.FONT),
                "font_size": 19}

    def _height(self, logo: Path | str) -> int:
        return int(bar_logo_scale_expr(self._cfg(logo), self.BAR_H).split(":")[1])

    def _rendered(self, logo: Path) -> tuple[int, int]:
        """(width, height) in px as ffmpeg would scale it."""
        h = self._height(logo)
        return round(h * bar_logo_aspect(self._cfg(logo))), h

    def _shipped(self) -> list[Path]:
        return sorted(self.ASSETS.glob("*.png"))

    def test_shipped_assets_exist(self):
        """A guard on the guard: these tests are vacuous without the artwork."""
        assert len(self._shipped()) >= 4

    def test_wordmark_height_is_unchanged(self):
        """The astromania wordmark renders at the bar slot's reference size (28x274 since 2026-10-29)."""
        wordmark = self.ASSETS / "astromania_text.png"
        legacy = int(round(self.BAR_H * BAR_LOGO_HEIGHT_FRAC)) // 2 * 2
        assert self._height(wordmark) == legacy
        assert self._rendered(wordmark) == (274, 28)

    def test_square_badge_is_no_longer_a_speck(self):
        """gmn_sphere went out 22x22 px; the wordmark beside it was 22x215."""
        badge = self.ASSETS / "gmn_sphere.png"
        wordmark_h = self._height(self.ASSETS / "astromania_text.png")
        w, h = self._rendered(badge)
        assert w >= 35, f"badge still narrow at {w} px (was 22)"
        assert h >= wordmark_h + 15

    def test_every_shipped_asset_carries_comparable_ink(self):
        """Area within a factor of 2 of the wordmark, or clamped.

        Equal ink is the target; the headroom clamp then trims the badges to
        ~65% of the wordmark's area so they clear the ROVIMEN row. Both are far
        from the 10% they used to get.
        """
        ref_w, ref_h = self._rendered(self.ASSETS / "astromania_text.png")
        ref_area = ref_w * ref_h
        for asset in self._shipped():
            w, h = self._rendered(asset)
            if asset.name in BAR_LOGO_ASSET_SCALE:
                continue  # deliberately larger, pinned in its own test
            clamped = (
                w >= self.BAR_H * BAR_LOGO_MAX_WIDTH_FRAC - 2
                or h >= bar_logo_headroom(self._cfg(asset), self.BAR_H) - 2
            )
            assert clamped or 0.5 <= (w * h) / ref_area <= 2.0, \
                f"{asset.name} renders {w}x{h}, wordmark {ref_w}x{ref_h}"

    def test_no_shipped_asset_escapes_the_bar(self):
        for asset in self._shipped():
            w, h = self._rendered(asset)
            assert 0 < h <= self.BAR_H - 2 * BAR_LOGO_MARGIN
            assert h <= self.BAR_H * BAR_LOGO_MAX_HEIGHT_FRAC
            scale = BAR_LOGO_ASSET_SCALE.get(asset.name, 1.0)
            assert w <= self.BAR_H * BAR_LOGO_MAX_WIDTH_FRAC * scale + 2

    def test_astroclub_wordmark_is_enlarged(self):
        """abtext.png read small at 22x158 px; BAR_LOGO_ASSET_SCALE grows it ~30%."""
        w, h = self._rendered(self.ASSETS / "abtext.png")
        assert (w, h) == (302, 42)

    def test_wide_banner_is_width_capped_not_stretched(self):
        """sqrt cuts both ways: a 19:1 banner must not creep across the bar."""
        aspect = 19.35  # ab_text_lung.png, 987x51, gmnro07's spare wordmark
        height = self.BAR_H * BAR_LOGO_HEIGHT_FRAC * math.sqrt(
            BAR_LOGO_REF_ASPECT / aspect
        )
        assert height * aspect > self.BAR_H * BAR_LOGO_MAX_WIDTH_FRAC, \
            "test asset no longer exercises the width cap"

    def test_unreadable_asset_renders_as_the_wordmark_does(self, tmp_path):
        """A truncated or exotic file must not land on an accidental size."""
        stub = tmp_path / "logo.png"
        stub.write_bytes(b"\x89PNG")
        assert bar_logo_scale_expr({"logo": str(stub)}, self.BAR_H) == \
            bar_logo_scale_expr({}, self.BAR_H)
        assert bar_logo_aspect({"logo": str(stub)}) == BAR_LOGO_REF_ASPECT

    def test_missing_asset_renders_as_the_wordmark_does(self):
        assert bar_logo_scale_expr({"logo": "/nope/gone.png"}, self.BAR_H) == \
            bar_logo_scale_expr({}, self.BAR_H)

    def test_zero_bar_height_never_yields_a_zero_scale(self):
        """The bar-less style passes bar_h=0 through the same helper."""
        badge = self.ASSETS / "gmn_sphere.png"
        assert int(bar_logo_scale_expr({"logo": str(badge)}, 0).split(":")[1]) >= 2

    def test_badge_stays_out_of_the_row_reserved_for_the_wordmark(self):
        """Row 0 owns its band whether or not the glyphs fill it.

        The logo is inset BAR_LOGO_MARGIN from the bar bottom, so its top sits at
        ``bar_h - MARGIN - h``. That must land below row 0, which bar_height()
        reserves as ``BAR_MARGIN + row0_h`` -- the NOMINAL height, not the glyph
        ink, which measures 13 px shorter and would invite a badge inside the
        band.
        """
        for name in ("gmn_sphere.png", "astroian_observatory.png", "sternfreunde_berlin.png",
                     "povesti_cu_stele_observatory.png", "zeta_aquarii_observatory.png"):
            cfg = self._cfg(self.ASSETS / name)
            h = int(bar_logo_scale_expr(cfg, self.BAR_H).split(":")[1])
            logo_top = self.BAR_H - BAR_LOGO_MARGIN - h
            row0_end = BAR_MARGIN + max(
                effective_font_size(cfg), network_font_size(cfg)
            )
            assert logo_top >= row0_end + BAR_GAP - 1, \
                f"{name} rides into the ROVIMEN row"

    def test_geometry_does_not_depend_on_the_environment(self):
        """Same code, same pixels -- with or without a measurable font.

        measure_text_height() needs PIL, which is an optional import in this
        module. Sizing the logo off a measured glyph height would make a station
        with PIL burn a 56 px badge while the dashboard rendered the same
        station's stack at 42 px: same overlay.py, different pixels, and no way
        to tell from either artifact which path produced it.
        """
        for asset in self._shipped():
            with_font = bar_logo_scale_expr(self._cfg(asset), self.BAR_H)
            no_font = bar_logo_scale_expr(
                {"logo": str(asset), "style": "cinema", "font": "/nonexistent.ttf",
                 "font_size": 19},
                self.BAR_H,
            )
            assert with_font == no_font, f"{asset.name}: {with_font} vs {no_font}"

    def test_headroom_tracks_the_bar_layout(self):
        """A bigger network wordmark must shrink the space left for the logo."""
        small = {"font_size": 12, "font": str(self.FONT)}
        large = {"font_size": 30, "font": str(self.FONT)}
        assert bar_logo_headroom(large, self.BAR_H) < bar_logo_headroom(small, self.BAR_H)

    def test_clamps_never_render_smaller_than_the_height_only_rule(self):
        """No asset, bar or font may make this a regression.

        A short bar or an unmeasurable font makes the headroom pessimistic --
        negative, even. Every clamp is floored at the baseline so the worst case
        is exactly the size the height-only rule shipped.
        """
        baseline_of = lambda bar: max(  # noqa: E731
            2, int(round(bar * BAR_LOGO_HEIGHT_FRAC)) // 2 * 2
        )
        for bar in (16, 32, 64, 96, 128, 160):
            for asset in self._shipped():
                for cfg in (self._cfg(asset), {"logo": str(asset)}):
                    h = int(bar_logo_scale_expr(cfg, bar).split(":")[1])
                    assert h >= baseline_of(bar), \
                        f"{asset.name} at bar={bar} shrank to {h}"

    def test_missing_font_falls_back_to_the_baseline_not_smaller(self):
        badge = self.ASSETS / "gmn_sphere.png"
        h = int(bar_logo_scale_expr(
            {"logo": str(badge), "font": "/nope.ttf", "font_size": 19}, 64
        ).split(":")[1])
        assert h >= max(2, int(round(64 * BAR_LOGO_HEIGHT_FRAC)) // 2 * 2)

    def test_headroom_never_returns_a_useless_height(self):
        """A bar too short for any logo must still yield a scalable size."""
        assert bar_logo_headroom({}, 0) >= 2
        assert bar_logo_headroom({}, 16) >= 2

    def test_size_still_comes_only_from_the_artwork(self):
        """No config key may reintroduce a per-station size lever."""
        badge = self.ASSETS / "gmn_sphere.png"
        plain = bar_logo_scale_expr({"logo": str(badge)}, self.BAR_H)
        for shadow in ("logo_size", "bar_logo_height_frac", "bar_logo_max_height_frac",
                       "bar_logo_max_width_frac"):
            assert bar_logo_scale_expr({"logo": str(badge), shadow: 200}, self.BAR_H) \
                == plain, f"{shadow} changed the rendered size"


class TestBarLogoOpacity:
    def test_default(self):
        assert bar_logo_opacity({}) == BAR_LOGO_OPACITY

    def test_override(self):
        assert bar_logo_opacity({"bar_logo_opacity": 0.5}) == 0.5


class TestGmnLogoSpec:
    def test_none_when_unset(self):
        assert gmn_logo_spec({}) is None

    def test_none_when_missing_file(self):
        assert gmn_logo_spec({"gmn_logo": "/no/such/logo.png"}) is None

    def test_default_corner_is_bottom_right(self, tmp_path):
        logo = tmp_path / "gmn.png"
        logo.write_bytes(b"\x89PNG")
        spec = gmn_logo_spec({"gmn_logo": str(logo)})
        assert spec is not None
        path, opacity, x, y = spec
        assert path == str(logo)
        assert GMN_LOGO_CORNER == "br"
        assert x == "W-w-14"
        assert y == "H-h-14"

    @pytest.mark.parametrize(
        "corner,expected_x,expected_y",
        [
            ("tl", "14", "14"),
            ("tr", "W-w-14", "14"),
            ("bl", "14", "H-h-14"),
            ("br", "W-w-14", "H-h-14"),
        ],
    )
    def test_each_corner(self, tmp_path, corner, expected_x, expected_y):
        logo = tmp_path / "gmn.png"
        logo.write_bytes(b"\x89PNG")
        _, _, x, y = gmn_logo_spec({"gmn_logo": str(logo), "gmn_corner": corner})
        assert (x, y) == (expected_x, expected_y)

    def test_custom_margin(self, tmp_path):
        logo = tmp_path / "gmn.png"
        logo.write_bytes(b"\x89PNG")
        _, _, x, y = gmn_logo_spec(
            {"gmn_logo": str(logo), "gmn_corner": "br", "gmn_logo_margin": 30}
        )
        assert x == "W-w-30"
        assert y == "H-h-30"


class TestBuildBrandingOverlay:
    @pytest.fixture(autouse=True)
    def _no_network_mark(self, monkeypatch):
        """These pin the operator and GMN logo stages; the ROVIMEN logo has its own tests."""
        import overlay
        monkeypatch.setattr(overlay, "NETWORK_MARK_PATH", Path("/nonexistent/rovimen_mark.png"))

    def test_no_logos_is_passthrough(self):
        inputs, fragment = build_branding_overlay(
            {}, in_label="vmain", out_label="out", input_index=1
        )
        assert inputs == []
        assert fragment == "[vmain]null[out]"

    def test_bar_logo_only(self, tmp_path):
        wm = tmp_path / "astromania.png"
        wm.write_bytes(b"\x89PNG")
        inputs, fragment = build_branding_overlay(
            {"logo": str(wm)}, in_label="vmain", out_label="out", input_index=1,
            bar_h=64,
        )
        assert inputs == ["-i", str(wm)]
        # Centred horizontally, BOTTOM-anchored inside the bar — never the
        # frame centre (no (H-h)/2 over the video).
        assert f"overlay=x=(W-w)/2:y=H-h-{BAR_LOGO_MARGIN}[out]" in fragment
        assert "(H-h)/2" not in fragment
        assert fragment.endswith("[out]")
        # Bar-legible opacity applied.
        assert f"colorchannelmixer=aa={BAR_LOGO_OPACITY}" in fragment
        # Sized by the bar height: 22% of a 64px bar = 14.08 -> 14 (even).
        expected_h = max(2, int(round(64 * BAR_LOGO_HEIGHT_FRAC)) // 2 * 2)
        assert f"scale=-2:{expected_h}" in fragment
        assert expected_h == 14

    def test_bar_logo_never_over_video_centre_standard(self, tmp_path):
        """With no bar (bar_h=0, standard style), the logo is still bottom-anchored,
        sized from the artwork's own width, and never centred over the video."""
        wm = tmp_path / "astromania.png"
        wm.write_bytes(b"\x89PNG")
        _, fragment = build_branding_overlay(
            {"logo": str(wm)}, in_label="vmain", out_label="out", input_index=1,
            bar_h=0,
        )
        assert "overlay=x=(W-w)/2:y=H-h-14[out]" in fragment
        assert "(H-h)/2" not in fragment
        assert "trunc(iw*" in fragment  # asset-width scaling in the bar-less style

    def test_show_logo_false_drops_bar_logo(self, tmp_path):
        wm = tmp_path / "astromania.png"
        wm.write_bytes(b"\x89PNG")
        inputs, fragment = build_branding_overlay(
            {"logo": str(wm), "show_logo": False},
            in_label="vmain", out_label="out", input_index=1, bar_h=64,
        )
        assert inputs == []
        assert fragment == "[vmain]null[out]"

    def test_gmn_only_goes_to_corner(self, tmp_path):
        gmn = tmp_path / "gmn.png"
        gmn.write_bytes(b"\x89PNG")
        inputs, fragment = build_branding_overlay(
            {"gmn_logo": str(gmn)}, in_label="vmain", out_label="out", input_index=1,
            bar_h=64,
        )
        assert inputs == ["-i", str(gmn)]
        assert "overlay=x=W-w-14:y=H-h-14[out]" in fragment

    def test_both_logos_chained_and_indexed(self, tmp_path):
        wm = tmp_path / "astromania.png"
        gmn = tmp_path / "gmn.png"
        wm.write_bytes(b"\x89PNG")
        gmn.write_bytes(b"\x89PNG")
        inputs, fragment = build_branding_overlay(
            {"logo": str(wm), "gmn_logo": str(gmn)},
            in_label="vmain", out_label="out", input_index=1, bar_h=64,
        )
        # Bar logo is input 1, GMN is input 2, both mapped by index.
        assert inputs == ["-i", str(wm), "-i", str(gmn)]
        assert "[1:v]scale=" in fragment
        assert "[2:v]scale=" in fragment
        # Bottom-bar logo composited first, then GMN corner, ending at [out].
        assert fragment.index("(W-w)/2") < fragment.index("W-w-14")
        assert "(H-h)/2" not in fragment  # nothing over the video centre
        assert fragment.endswith("[out]")
        # No dangling references to the intermediate stage label outside its use.
        assert fragment.count("[out]") == 1


# ---- relabel_fragment_output: robust final-stage relabel ----


class TestRelabelFragmentOutput:
    def test_swaps_trailing_label(self):
        frag = "[0][logo1]overlay=x=(W-w)/2:y=(H-h)/2[vbrand]"
        out = relabel_fragment_output(frag, "vbrand", "out")
        assert out == "[0][logo1]overlay=x=(W-w)/2:y=(H-h)/2[out]"

    def test_only_final_label_renamed_not_input_pads(self):
        # A watermark+GMN fragment references [logo1]/[wm1] mid-graph; only the
        # terminal [gmn2] pad may be renamed, never an interior reference.
        frag = "[1:v]scale=x[logo1];[vmain][logo1]overlay[wm1];[wm1][logo2]overlay[gmn2]"
        out = relabel_fragment_output(frag, "gmn2", "out")
        assert out.endswith("[out]")
        assert "[gmn2]" not in out
        assert out.count("[logo1]") == 2  # interior refs untouched

    def test_bracket_in_expression_does_not_confuse_relabel(self):
        # The old rsplit('[',1)[0] approach would slice at the last '[' — a
        # bracket inside an overlay expression would corrupt the graph. Naming
        # the pad explicitly is immune to that.
        frag = "[vmain][logo1]overlay=x=if(gt(a[,b],0),1,2)[vbrand]"
        out = relabel_fragment_output(frag, "vbrand", "out")
        assert out == "[vmain][logo1]overlay=x=if(gt(a[,b],0),1,2)[out]"

    def test_raises_when_label_absent(self):
        with pytest.raises(ValueError):
            relabel_fragment_output("[a]null[b]", "vbrand", "out")

    def test_passthrough_fragment_relabels(self):
        # build_branding_overlay's no-logo passthrough ends at the out_label
        # already, but the helper must still handle renaming it.
        frag = "[vmain]null[vbrand]"
        assert relabel_fragment_output(frag, "vbrand", "out") == "[vmain]null[out]"


# ---- real ffmpeg render: nothing drawn over the video area ----

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ASTROMANIA_LOGO = _REPO_ROOT / "rovimen-scripts" / "assets" / "astromania_text.png"
_GMN_LOGO = _REPO_ROOT / "rovimen-scripts" / "assets" / "gmn_sphere.png"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
class TestRenderKeepsVideoAreaClean:
    """Render a real frame through the SAME graph the encoder builds and prove
    the sky/video area is untouched — no branding splashed over the meteor.

    This is the regression that PR #951 shipped: a large semi-transparent
    astromania watermark composited at the FRAME CENTRE, over the video. The
    string assertions above catch the layout expressions, but only a rendered
    frame proves the pixels above the bar equal the clean source.
    """

    STATION_ID = "RO001E"
    CHUNK_EPOCH = 1785103297

    def _cfg(self, style: str) -> dict:
        font = _system_font()
        if font is None:
            pytest.skip("no usable system font")
        if not _ASTROMANIA_LOGO.exists():
            pytest.skip("astromania logo asset missing")
        return {
            "font": font,
            "font_size": 20,
            "text_opacity": 1.0,
            "coords": "45.0N 25.0E",
            "network": "ROVIMEN",
            "style": style,
            "show_logo": True,
            "show_network": True,
            "show_station": True,
            "show_coords": True,
            "show_pointing": True,
            "show_timestamp": True,
            "logo": str(_ASTROMANIA_LOGO),
            "gmn_logo": str(_GMN_LOGO) if _GMN_LOGO.exists() else None,
            "az": 255.2,
            "alt": 40.7,
        }

    def _render(self, tmp_path: Path, cfg: dict, w: int = 640, h: int = 360):
        """Render a solid-colour frame through drawtext + branding; return
        (out_path, bar_h, w, h). The base is a flat mid-grey so any pixel the
        branding touches over the video changes colour."""
        filters, bar_h = build_drawtext_annotations(
            cfg, self.STATION_ID, {"az": cfg["az"], "alt": cfg["alt"]}, self.CHUNK_EPOCH
        )
        logo_inputs, brand_fragment = build_branding_overlay(
            cfg, in_label="vmain", out_label="out", input_index=1, bar_h=bar_h
        )
        vf_parts: list[str] = []
        if bar_h > 0:
            vf_parts.append(f"pad=iw:ih+{bar_h}:0:0:black")
        vf_parts.extend(filters)
        out = tmp_path / f"render_{cfg['style']}.png"
        base = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"color=c=0x808080:s={w}x{h}:d=1",
        ]
        if logo_inputs:
            cpu_chain = ",".join(vf_parts) if vf_parts else "null"
            fc = f"[0:v]{cpu_chain}[vmain];{brand_fragment}"
            cmd = [*base, *logo_inputs, "-filter_complex", fc,
                   "-map", "[out]", "-frames:v", "1", str(out)]
        else:
            cmd = [*base, "-vf", ",".join(vf_parts), "-frames:v", "1", str(out)]
        proc = subprocess.run(
            cmd, env={**os.environ, "TZ": "UTC"}, capture_output=True, text=True
        )
        assert proc.returncode == 0, f"ffmpeg rejected the filter: {proc.stderr}"
        assert out.exists()
        return out, bar_h, w, h

    def test_cinema_video_area_is_untouched(self, tmp_path):
        """The whole video region above the bar stays flat mid-grey: no branding
        or text drawn over the sky."""
        from PIL import Image

        cfg = self._cfg("cinema")
        out, bar_h, w, h = self._render(tmp_path, cfg)
        assert bar_h > 0
        with Image.open(out) as im:
            im = im.convert("RGB")
            assert im.height == h + bar_h  # bar padded below the video
            # Every pixel of the video region (rows 0..h) is the untouched base.
            video = im.crop((0, 0, w, h))
            colours = video.getcolors(maxcolors=1 << 24)
            assert colours == [(w * h, (128, 128, 128))], (
                "branding or text bled over the video area: "
                f"{sorted(colours, key=lambda c: -c[0])[:5]}"
            )
            # The bar region is NOT flat grey — the logo/text landed there.
            bar = im.crop((0, h, w, h + bar_h))
            assert bar.getcolors(maxcolors=1 << 24) != [(w * bar_h, (0, 0, 0))]

    def test_standard_top_of_frame_is_untouched(self, tmp_path):
        """Standard style has no bar; the branding is bottom-anchored, so the top
        two-thirds of the frame (where the meteor lives) stays clean."""
        from PIL import Image

        cfg = self._cfg("standard")
        out, bar_h, w, h = self._render(tmp_path, cfg)
        assert bar_h == 0
        with Image.open(out) as im:
            im = im.convert("RGB")
            assert im.size == (w, h)
            # Top 60% of the frame must be the untouched base colour.
            top = im.crop((0, 0, w, int(h * 0.6)))
            colours = top.getcolors(maxcolors=1 << 24)
            assert colours == [(top.width * top.height, (128, 128, 128))], (
                "branding bled into the upper frame: "
                f"{sorted(colours, key=lambda c: -c[0])[:5]}"
            )
