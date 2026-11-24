"""Tests for overlay_details.py -- growing the bottom bar with per-meteor rows.

Covers the pure field-formatting and row-assembly logic (which fields render, in
what order, and how a meteor becomes a ``(left, right)`` bar row), the
detection<->clip time matching (now returning EVERY meteor in a clip, ordered),
and the meteor-row drawtext placement (owned by overlay.py, exercised through the
same call the render uses). Rendering (ffmpeg) is not exercised here; formatting
and placement are what the burnt-in result depends on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import overlay
import overlay_details as od


# ---------------------------------------------------------------------------
# Field formatting
# ---------------------------------------------------------------------------

class TestFieldFormatting:
    def test_mag_sign_and_precision(self):
        assert od._fmt_mag(-2.3) == "mag -2.3"
        assert od._fmt_mag(1.0) == "mag +1.0"
        assert od._fmt_mag(None) is None

    def test_shower_uppercased_bare_code(self):
        assert od._fmt_shower("per") == "PER"
        assert od._fmt_shower("SPO") == "SPO"
        assert od._fmt_shower(None) is None
        assert od._fmt_shower("") is None

    def test_alt_begin_end(self):
        assert od._fmt_alt(65.0, 42.0) == "ALT 65.0°→42.0°"

    def test_alt_single_value(self):
        assert od._fmt_alt(65.0, None) == "ALT 65.0°"
        assert od._fmt_alt(None, 42.0) == "ALT 42.0°"
        assert od._fmt_alt(None, None) is None

    def test_velocity(self):
        assert od._fmt_velocity(12.4) == "12.4°/s"
        assert od._fmt_velocity(None) is None

    def test_tof_positive_only(self):
        assert od._fmt_tof(1.234) == "ToF 1.23s"
        assert od._fmt_tof(0.0) is None
        assert od._fmt_tof(-1.0) is None
        assert od._fmt_tof(None) is None


# ---------------------------------------------------------------------------
# Line assembly (left-hand science fields of one meteor row)
# ---------------------------------------------------------------------------

class TestFormatDetailLines:
    FULL = {
        "shower": "PER",
        "mag_apparent": -1.5,
        "elev_beg": 70.0,
        "elev_end": 55.0,
        "angular_velocity": 14.2,
        "duration_s": 0.8,
    }

    def test_full_detection_all_fields_in_order(self):
        # The row carries shower, magnitude, altitude, velocity and ToF in that
        # fixed order — the five fields the details pass exists to show.
        lines = od.format_detail_lines(self.FULL)
        assert lines == [
            "PER",
            "mag -1.5",
            "ALT 70.0°→55.0°",
            "14.2°/s",
            "ToF 0.80s",
        ]

    def test_altitude_is_present_whenever_rms_measured_it(self):
        # elev_beg/elev_end come from the FTPdetectinfo frame data, so a
        # detection RMS measured always has them — dropping them from the row
        # was the gap this closes.
        lines = od.format_detail_lines({"elev_beg": 12.5, "elev_end": 8.0})
        assert lines == ["ALT 12.5°→8.0°"]

    def test_altitude_omitted_when_unmeasured(self):
        lines = od.format_detail_lines({"shower": "PER", "mag_apparent": 1.0})
        assert not any("ALT" in line for line in lines)

    def test_missing_fields_are_skipped_not_blanked(self):
        det = {"shower": "SPO", "mag_apparent": 2.0}
        lines = od.format_detail_lines(det)
        assert lines == ["SPO", "mag +2.0"]
        assert all(line.strip() for line in lines)

    def test_empty_detection_yields_no_lines(self):
        assert od.format_detail_lines({}) == []

    def test_order_is_stable_regardless_of_dict_order(self):
        det = {
            "duration_s": 0.8,
            "angular_velocity": 14.2,
            "mag_apparent": -1.5,
            "shower": "PER",
            "elev_beg": 70.0,
            "elev_end": 55.0,
        }
        assert od.format_detail_lines(det) == od.format_detail_lines(self.FULL)


# ---------------------------------------------------------------------------
# Meteor row assembly: (left science fields, right UTC time)
# ---------------------------------------------------------------------------

class TestFormatMeteorRow:
    def test_left_joins_fields_right_is_time(self):
        det = {
            "shower": "NIA",
            "mag_apparent": 1.9,
            "angular_velocity": 9.6,
            "duration_s": 0.48,
            # The FF block began 8 s before the meteor: the row shows the meteor.
            "ff_file": "FF_RO001F_20260824_221445_000_0000001.fits",
            "time_utc": "2026-08-24T22:14:53.000",
        }
        left, right = od.format_meteor_row(det)
        assert left == od.FIELD_SEP.join(["NIA", "mag +1.9", "9.6°/s", "ToF 0.48s"])
        assert right == "22:14:53Z"

    def test_row_time_from_time_utc_when_no_ff(self):
        det = {"shower": "PER", "time_utc": "2026-08-24T22:14:57"}
        left, right = od.format_meteor_row(det)
        assert left == "PER"
        assert right == "22:14:57Z"

    def test_time_only_still_yields_a_row(self):
        # A detection with no science fields but a known time still gets a row
        # (its clock), rather than being dropped as blank.
        det = {"ff_file": "FF_RO001F_20260824_221445_000_0000001.fits",
               "time_utc": "2026-08-24T22:14:53.000"}
        row = od.format_meteor_row(det)
        assert row is not None
        left, right = row
        assert left == ""
        assert right == "22:14:53Z"

    def test_fully_empty_detection_yields_no_row(self):
        assert od.format_meteor_row({}) is None

    def test_build_meteor_rows_skips_empties(self):
        dets = [
            {"shower": "PER", "ff_file": "FF_RO001F_20260824_221445_000_0.fits",
             "time_utc": "2026-08-24T22:14:53.000"},
            {},  # dropped
            {"shower": "SPO", "ff_file": "FF_RO001F_20260824_221445_000_0.fits",
             "time_utc": "2026-08-24T22:14:57.000"},
        ]
        rows = od.build_meteor_rows(dets)
        assert len(rows) == 2
        assert rows[0][1] == "22:14:53Z"
        assert rows[1][1] == "22:14:57Z"


# ---------------------------------------------------------------------------
# Meteor-row drawtext placement (owned by overlay.py)
# ---------------------------------------------------------------------------

@pytest.fixture
def overlay_cfg(tmp_path):
    font = tmp_path / "font.ttf"
    font.write_bytes(b"")
    return {
        "font": str(font),
        "font_size": 20,
        "text_opacity": 0.4,
        "style": "cinema",
    }


class TestBuildMeteorRowAnnotations:
    ROWS = [("PER   mag -1.5   14.2°/s   ToF 0.80s", "22:14:53Z"),
            ("SPO   mag +2.0", "22:14:57Z")]
    BASE_H = 800  # 720 sky + a live bar

    def _extra(self, cfg):
        return overlay.meteor_rows_extra_height(cfg, len(self.ROWS))

    def test_no_rows_returns_empty(self, overlay_cfg):
        assert overlay.build_meteor_row_annotations(overlay_cfg, [], self.BASE_H, 100) == []

    def test_zero_extra_height_returns_empty(self, overlay_cfg):
        assert overlay.build_meteor_row_annotations(
            overlay_cfg, self.ROWS, self.BASE_H, 0
        ) == []

    def test_missing_font_returns_empty(self):
        assert overlay.build_meteor_row_annotations(
            {"font": "/no/such.ttf", "font_size": 20}, self.ROWS, self.BASE_H, 100
        ) == []

    def test_null_font_returns_empty(self):
        # config_defaults ships "font": null — must degrade, not crash Path(None).
        assert overlay.build_meteor_row_annotations(
            {"font": None, "font_size": 20}, self.ROWS, self.BASE_H, 100
        ) == []

    def test_one_left_and_one_right_per_row_plus_separator(self, overlay_cfg):
        extra = self._extra(overlay_cfg)
        filters = overlay.build_meteor_row_annotations(
            overlay_cfg, self.ROWS, self.BASE_H, extra
        )
        # One separator rule + a left + a right per row.
        drawboxes = [f for f in filters if f.startswith("drawbox=")]
        drawtexts = [f for f in filters if f.startswith("drawtext=")]
        assert len(drawboxes) == 1
        assert len(drawtexts) == 2 * len(self.ROWS)

    def test_rows_are_below_the_base_frame_never_over_the_sky(self, overlay_cfg):
        """Every meteor row sits at y >= base_frame_h: never over the 720 sky."""
        extra = self._extra(overlay_cfg)
        filters = overlay.build_meteor_row_annotations(
            overlay_cfg, self.ROWS, self.BASE_H, extra
        )
        for f in filters:
            # Both drawtext (:y=) and drawbox (:y=) carry a literal pixel y.
            y = int(f.split(":y=")[1].split(":")[0])
            assert y >= self.BASE_H, f"row drawn into the sky at y={y}"
            # No frame-relative anchoring (that is the live bar's job).
            assert "y=h-" not in f and "y=H-" not in f

    def test_rows_stack_downward_by_one_row_height(self, overlay_cfg):
        extra = self._extra(overlay_cfg)
        filters = overlay.build_meteor_row_annotations(
            overlay_cfg, self.ROWS, self.BASE_H, extra
        )
        lefts = [f for f in filters
                 if f.startswith("drawtext=") and f":x={overlay.BAR_MARGIN}:" in f]
        ys = [int(f.split(":y=")[1].split(":")[0]) for f in lefts]
        assert ys == sorted(ys) and len(set(ys)) == len(ys)
        row_h = overlay.meteor_row_height(overlay_cfg)
        for a, b in zip(ys, ys[1:]):
            assert b - a == row_h

    def test_left_is_margin_anchored_right_is_edge_anchored(self, overlay_cfg):
        extra = self._extra(overlay_cfg)
        filters = overlay.build_meteor_row_annotations(
            overlay_cfg, self.ROWS, self.BASE_H, extra
        )
        drawtexts = [f for f in filters if f.startswith("drawtext=")]
        lefts = [f for f in drawtexts if f":x={overlay.BAR_MARGIN}:" in f]
        rights = [f for f in drawtexts if f"x=w-{overlay.BAR_MARGIN}-tw" in f]
        assert len(lefts) == len(self.ROWS)
        assert len(rights) == len(self.ROWS)

    def test_opacity_and_scaled_font_referenced(self, overlay_cfg):
        extra = self._extra(overlay_cfg)
        filters = [f for f in overlay.build_meteor_row_annotations(
            overlay_cfg, self.ROWS, self.BASE_H, extra
        ) if f.startswith("drawtext=")]
        size = overlay.effective_font_size(overlay_cfg)
        for f in filters:
            assert f"fontfile={overlay_cfg['font']}" in f
            assert f"fontsize={size}" in f
            assert "fontcolor=white@0.40" in f
            assert "shadowcolor=black@" in f

    def test_special_chars_escaped(self, overlay_cfg):
        extra = self._extra(overlay_cfg)
        filters = [f for f in overlay.build_meteor_row_annotations(
            overlay_cfg, [("ALT 70.0°→55.0°", "22:14:53Z")], self.BASE_H, extra
        ) if f.startswith("drawtext=")]
        # The colon in the clock and the arrow in the text stay inside one
        # single-quoted option each.
        for f in filters:
            assert f.count("text='") == 1


class TestEscapeDrawtext:
    def test_colon_escaped(self):
        assert overlay.escape_drawtext("a:b") == r"'a\:b'"

    def test_single_quote_closes_and_reopens_the_quoting(self):
        # Inside single quotes the filtergraph parser takes a backslash
        # literally, so the quote is escaped outside them
        # (tests/test_overlay_text_escaping.py renders it through ffmpeg).
        assert overlay.escape_drawtext("it's") == r"'it\'\''s'"

    def test_backslash_escaped_for_option_and_expansion(self):
        assert overlay.escape_drawtext("a\\b") == r"'a\\\\b'"

    def test_percent_escaped_so_it_does_not_expand(self):
        assert overlay.escape_drawtext("100%") == r"'100\\%'"

    def test_plain_wrapped_in_quotes(self):
        assert overlay.escape_drawtext("SHW PER") == "'SHW PER'"


# ---------------------------------------------------------------------------
# Bar geometry grows with meteor count
# ---------------------------------------------------------------------------

class TestBarGrowsWithMeteorCount:
    CFG = {"style": "cinema", "font_size": 20}

    def test_zero_meteors_matches_the_live_bar(self):
        """The base bar (n=0) is unchanged: the live path and stitch slice rely on it."""
        assert overlay.bar_height(self.CFG) == overlay.bar_height(self.CFG, 0)

    def test_each_meteor_adds_room(self):
        h0 = overlay.bar_height(self.CFG, 0)
        h1 = overlay.bar_height(self.CFG, 1)
        h2 = overlay.bar_height(self.CFG, 2)
        h3 = overlay.bar_height(self.CFG, 3)
        assert h0 < h1 <= h2 <= h3
        # Monotonic, and each is 16px-aligned for the VAAPI encoder.
        for h in (h0, h1, h2, h3):
            assert h % 16 == 0

    def test_extra_height_is_the_difference_from_the_base(self):
        for n in (1, 2, 3):
            extra = overlay.meteor_rows_extra_height(self.CFG, n)
            assert extra == overlay.bar_height(self.CFG, n) - overlay.bar_height(self.CFG, 0)
            assert extra > 0

    def test_extra_height_zero_for_no_meteors_or_non_cinema(self):
        assert overlay.meteor_rows_extra_height(self.CFG, 0) == 0
        assert overlay.meteor_rows_extra_height({"style": "standard"}, 3) == 0


# ---------------------------------------------------------------------------
# Detection <-> clip mapping
# ---------------------------------------------------------------------------

class TestDetectionTime:
    def test_is_the_meteor_time_not_the_ff_block_start(self):
        # The FF name is the start of a 256-frame (10.24 s) block; time_utc is
        # block start + first frame / fps, the moment the meteor appeared.
        det = {"ff_file": "FF_RO000H_20260801_190512_123_0000001.fits",
               "time_utc": "2026-08-01T19:05:20.123"}
        dt = od._detection_time(det)
        assert dt is not None
        assert (dt.hour, dt.minute, dt.second) == (19, 5, 20)
        assert dt.microsecond == 123000

    def test_radiant_only_row(self):
        det = {"ff_file": "RAD_x", "time_utc": "2026-08-01T19:05:25"}
        dt = od._detection_time(det)
        assert dt is not None
        assert dt.second == 25

    def test_none_without_time_utc(self):
        # An FF name alone says when the block began, not when the meteor flew.
        assert od._detection_time(
            {"ff_file": "FF_RO000H_20260801_190512_123_0000001.fits"}) is None
        assert od._detection_time({"ff_file": "junk"}) is None


class TestMatchDetectionsToClip:
    CLIP = "RO000H_20260801_190512_color.mkv"

    def _det(self, secs: int, mag: float | None = None) -> dict:
        # A meteor at 19:05:<secs>, 8 s (frame 200) into an FF block that
        # began at 19:05:<secs - 8>, so the block start and the meteor
        # disagree about which clip the meteor is in.
        ff = f"FF_RO000H_20260801_1905{secs - 8:02d}_000_0000001.fits"
        return {"ff_file": ff, "time_utc": f"2026-08-01T19:05:{secs:02d}.000",
                "mag_apparent": mag}

    def test_all_detections_inside_span_returned_ordered(self):
        # Two meteors in one 20s clip -> two rows, in time order.
        first = self._det(15, mag=1.0)   # 19:05:15
        second = self._det(25, mag=-3.0)  # 19:05:25
        got = od.match_detections_to_clip(self.CLIP, [second, first], 20)
        assert got == [first, second]

    def test_detection_outside_span_excluded(self):
        inside = self._det(20)
        outside = self._det(40)  # its FF block began at 19:05:32, the span's end
        got = od.match_detections_to_clip(self.CLIP, [inside, outside], 20)
        assert got == [inside]

    def test_no_detections_returns_empty(self):
        assert od.match_detections_to_clip(self.CLIP, [], 20) == []

    def test_unparseable_clip_returns_empty(self):
        assert od.match_detections_to_clip("garbage.mkv", [self._det(20)], 20) == []

    def test_stitched_clip_uses_two_timestamp_span(self):
        clip = "RO000H_20260801_190512_190612_color.mkv"
        det = self._det(50)  # 19:05:50, inside the wide stitched span
        assert od.match_detections_to_clip(clip, [det], 20) == [det]


class TestMatchDetectionToClipSingular:
    """The singular helper still returns the brightest for the CLI path."""

    CLIP = "RO000H_20260801_190512_color.mkv"

    def _det(self, secs: int, mag: float | None = None) -> dict:
        # A meteor at 19:05:<secs>, 8 s (frame 200) into an FF block that
        # began at 19:05:<secs - 8>, so the block start and the meteor
        # disagree about which clip the meteor is in.
        ff = f"FF_RO000H_20260801_1905{secs - 8:02d}_000_0000001.fits"
        return {"ff_file": ff, "time_utc": f"2026-08-01T19:05:{secs:02d}.000",
                "mag_apparent": mag}

    def test_brightest_wins(self):
        faint = self._det(15, mag=1.0)
        bright = self._det(25, mag=-3.0)
        assert od.match_detection_to_clip(self.CLIP, [faint, bright], 20) is bright

    def test_none_when_empty(self):
        assert od.match_detection_to_clip(self.CLIP, [], 20) is None


# ---------------------------------------------------------------------------
# Night driver — only-detected-clips policy
# ---------------------------------------------------------------------------

class TestRunNightSkipsUndetected:
    def test_no_locked_clips_returns_zero(self, tmp_path, monkeypatch):
        cfg = {"videocapture_path": str(tmp_path)}
        assert od.run_night("RO000H", "20260801", cfg) == 0

    def test_locked_clip_without_matching_detection_is_untouched(
        self, tmp_path, monkeypatch
    ):
        night = tmp_path / "RO000H" / "20260801"
        night.mkdir(parents=True)
        clip = night / "RO000H_20260801_190512_color.mkv"
        clip.write_bytes(b"")
        (night / (clip.name + ".locked")).write_bytes(b"")
        cfg = {"videocapture_path": str(tmp_path), "segment_duration": 20}

        monkeypatch.setattr(od, "_load_night_detections", lambda *a, **k: [])
        rendered: list[Path] = []
        monkeypatch.setattr(
            od, "render_details_clip",
            lambda c, d, cf, dst=None: rendered.append(c) or c,
        )
        assert od.run_night("RO000H", "20260801", cfg) == 0
        assert rendered == []

    def test_locked_clip_with_matching_detection_is_rendered(
        self, tmp_path, monkeypatch
    ):
        night = tmp_path / "RO000H" / "20260801"
        night.mkdir(parents=True)
        clip = night / "RO000H_20260801_190512_color.mkv"
        clip.write_bytes(b"")
        (night / (clip.name + ".locked")).write_bytes(b"")
        cfg = {"videocapture_path": str(tmp_path), "segment_duration": 20}

        det = {"ff_file": "FF_RO000H_20260801_190520_000_0000001.fits",
               "time_utc": "2026-08-01T19:05:20.000",
               "shower": "PER", "mag_apparent": -1.0}
        monkeypatch.setattr(od, "_load_night_detections", lambda *a, **k: [det])
        rendered: list[tuple[Path, list]] = []
        monkeypatch.setattr(
            od, "render_details_clip",
            lambda c, d, cf, dst=None: rendered.append((c, d)) or c,
        )
        assert od.run_night("RO000H", "20260801", cfg) == 1
        assert len(rendered) == 1
        got_clip, got_dets = rendered[0]
        assert got_clip == clip
        # The night driver passes the LIST of matching meteors, not a single dict.
        assert got_dets == [det]

    def test_multi_meteor_clip_passes_all_rows(self, tmp_path, monkeypatch):
        night = tmp_path / "RO000H" / "20260801"
        night.mkdir(parents=True)
        clip = night / "RO000H_20260801_190512_color.mkv"
        clip.write_bytes(b"")
        (night / (clip.name + ".locked")).write_bytes(b"")
        cfg = {"videocapture_path": str(tmp_path), "segment_duration": 20}

        d1 = {"ff_file": "FF_RO000H_20260801_190515_000_0000001.fits",
              "time_utc": "2026-08-01T19:05:15.000", "shower": "PER"}
        d2 = {"ff_file": "FF_RO000H_20260801_190525_000_0000001.fits",
              "time_utc": "2026-08-01T19:05:25.000", "shower": "SPO"}
        monkeypatch.setattr(od, "_load_night_detections", lambda *a, **k: [d2, d1])
        captured: list[list] = []
        monkeypatch.setattr(
            od, "render_details_clip",
            lambda c, d, cf, dst=None: captured.append(d) or c,
        )
        assert od.run_night("RO000H", "20260801", cfg) == 1
        # Both meteors handed to the render, in time order.
        assert captured == [[d1, d2]]


# ---------------------------------------------------------------------------
# Feature enablement: services.overlay_details.enabled ships true fleet-wide
# ---------------------------------------------------------------------------

class TestOverlayDetailsEnabledByDefault:
    def test_config_defaults_enables_overlay_details(self):
        """dawn_process gates the details pass on services.overlay_details.enabled;
        shipping it true fleet-wide is what runs the feature at dawn on every
        station after the base upload."""
        import json
        defaults = json.loads(
            (Path(__file__).resolve().parents[1]
             / "rovimen-scripts" / "config_defaults.json").read_text()
        )
        assert defaults["services"]["overlay_details"]["enabled"] is True


# ---------------------------------------------------------------------------
# Multi-station rows: what triangulation adds (issue #950)
# ---------------------------------------------------------------------------

_TRAJ = {
    "trajectory_id": "20260801190520_abc12",
    "shower": "CAP",
    "ra_geo_deg": 45.1,
    "dec_geo_deg": 58.0,
    "velocity_geo_km_s": 59.2,
    "altitude_begin_km": 102.4,
    "altitude_end_km": 87.6,
    "station_count": 3,
}

_DET = {
    # Frame 200 of a block that began at 19:05:12: the meteor flew at 19:05:20.
    "ff_file": "FF_RO000H_20260801_190512_000_0000001.fits",
    "time_utc": "2026-08-01T19:05:20.000",
    "shower": "PER",
    "mag_apparent": -1.5,
    "elev_beg": 70.0,
    "elev_end": 55.0,
    "angular_velocity": 14.2,
    "duration_s": 0.8,
}


class TestTrajectoryRow:
    def test_carries_radiant_velocity_and_heights(self):
        left, right = od.format_trajectory_row(_TRAJ)
        assert left == od.FIELD_SEP.join([
            "GMN", "RAD 45.1°,+58.0°", "Vg 59.2 km/s", "H 102→88 km",
        ])
        assert right == "3 cams"

    def test_absent_without_a_trajectory(self):
        assert od.format_trajectory_row(None) is None
        assert od.format_trajectory_row({}) is None

    def test_label_alone_is_not_a_row(self):
        # A trajectory record stripped of every measurement would otherwise
        # render a bar row saying only "GMN".
        assert od.format_trajectory_row({"trajectory_id": "x"}) is None

    def test_partial_solution_renders_what_it_has(self):
        left, _ = od.format_trajectory_row(
            {"velocity_geo_km_s": 41.0, "station_count": 2}
        )
        assert left == od.FIELD_SEP.join(["GMN", "Vg 41.0 km/s"])


class TestMeteorRowsWithTrajectory:
    def test_untriangulated_meteor_still_gets_exactly_one_row(self):
        rows = od.format_meteor_rows(_DET)
        assert len(rows) == 1
        assert "GMN" not in rows[0][0]

    def test_triangulated_meteor_gets_its_own_row_plus_the_solution(self):
        rows = od.format_meteor_rows({**_DET, "trajectory": _TRAJ})
        assert len(rows) == 2
        # Single-station measurements stay on the first row, unreplaced: the
        # apparent angular rate is what the clip shows.
        assert "14.2°/s" in rows[0][0]
        assert "ALT 70.0°→55.0°" in rows[0][0]
        assert rows[0][1] == "19:05:20Z"
        # The solution goes underneath rather than onto the same line, which
        # would run into the clock at the right.
        assert rows[1][0].startswith("GMN")
        assert "Vg 59.2 km/s" in rows[1][0]

    def test_gmn_shower_supersedes_the_single_station_guess(self):
        rows = od.format_meteor_rows({**_DET, "trajectory": _TRAJ})
        assert rows[0][0].startswith("CAP")      # not the single-station PER

    def test_single_station_shower_kept_when_gmn_did_not_identify_one(self):
        traj = {**_TRAJ, "shower": None}
        rows = od.format_meteor_rows({**_DET, "trajectory": traj})
        assert rows[0][0].startswith("PER")

    def test_bar_grows_by_rows_not_by_meteors(self):
        # Two meteors, one triangulated -> three rows, so the bar the render
        # pads for is sized off the row count.
        rows = od.build_meteor_rows([
            _DET,
            {**_DET,
             "ff_file": "FF_RO000H_20260801_190525_000_0000002.fits",
             "time_utc": "2026-08-01T19:05:25.000",
             "trajectory": _TRAJ},
        ])
        assert len(rows) == 3


# ---------------------------------------------------------------------------
# Trajectory input: index, file, fetch gating
# ---------------------------------------------------------------------------

class TestTrajectoryIndexing:
    def test_index_keys_on_second_precision_time(self):
        idx = od.index_trajectories([
            {"time_utc": "2026-08-01T19:05:20.123456", "trajectory": _TRAJ},
        ])
        assert idx == {"2026-08-01T19:05:20": _TRAJ}

    def test_index_skips_malformed_rows(self):
        idx = od.index_trajectories([
            {"time_utc": None, "trajectory": _TRAJ},
            {"time_utc": "2026-08-01T19:05:20", "trajectory": None},
            None,
            {},
        ])
        assert idx == {}

    def test_load_file_accepts_the_endpoint_body_or_a_bare_list(self, tmp_path):
        import json
        row = {"time_utc": "2026-08-01T19:05:20", "trajectory": _TRAJ}
        body = tmp_path / "body.json"
        body.write_text(json.dumps({"camera": "RO000H", "trajectories": [row]}))
        bare = tmp_path / "bare.json"
        bare.write_text(json.dumps([row]))
        assert od.load_trajectory_file(body) == {"2026-08-01T19:05:20": _TRAJ}
        assert od.load_trajectory_file(bare) == {"2026-08-01T19:05:20": _TRAJ}

    def test_load_file_tolerates_a_missing_or_corrupt_file(self, tmp_path):
        assert od.load_trajectory_file(tmp_path / "nope.json") == {}
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        assert od.load_trajectory_file(bad) == {}


class TestAttachTrajectories:
    def test_matches_on_begin_time(self):
        dets = [dict(_DET)]
        assert od.attach_trajectories(
            dets, {"2026-08-01T19:05:20": _TRAJ}
        ) == 1
        assert dets[0]["trajectory"] == _TRAJ

    def test_absorbs_a_one_second_solver_disagreement(self):
        dets = [dict(_DET)]
        assert od.attach_trajectories(dets, {"2026-08-01T19:05:21": _TRAJ}) == 1
        assert dets[0]["trajectory"] == _TRAJ

    def test_leaves_unmatched_detections_untouched(self):
        dets = [dict(_DET)]
        assert od.attach_trajectories(dets, {"2026-08-01T20:00:00": _TRAJ}) == 0
        assert "trajectory" not in dets[0]

    def test_empty_index_is_a_no_op(self):
        dets = [dict(_DET)]
        assert od.attach_trajectories(dets, {}) == 0
        assert "trajectory" not in dets[0]


class TestFetchGating:
    def test_dawn_does_not_ask_about_a_night_gmn_cannot_have(self):
        from datetime import datetime, timezone
        now = datetime(2026, 8, 2, 6, 0, tzinfo=timezone.utc)
        assert od._night_old_enough_for_gmn("20260801", now=now) is False

    def test_a_fortnight_later_it_does(self):
        from datetime import datetime, timezone
        now = datetime(2026, 8, 15, 6, 0, tzinfo=timezone.utc)
        assert od._night_old_enough_for_gmn("20260801", now=now) is True

    def test_fetch_short_circuits_for_a_fresh_night(self, monkeypatch):
        # Nothing should reach the network for last night's pass.
        def _boom(*_a, **_k):
            raise AssertionError("fetch attempted for a fresh night")
        monkeypatch.setattr(od, "_night_old_enough_for_gmn", lambda *a, **k: False)
        import urllib.request
        monkeypatch.setattr(urllib.request, "urlopen", _boom)
        assert od.fetch_night_trajectories("RO000H", "20260801", {}) == {}

    def test_fetch_without_push_credentials_is_silent(self, monkeypatch):
        monkeypatch.setattr(od, "_night_old_enough_for_gmn", lambda *a, **k: True)
        import rovimen_pusher
        monkeypatch.setattr(rovimen_pusher, "resolve_ingest_key", lambda *a, **k: None)
        assert od.fetch_night_trajectories("RO000H", "20260801", {}) == {}

    def test_run_night_uses_a_supplied_index_without_fetching(
        self, tmp_path, monkeypatch
    ):
        night = tmp_path / "RO000H" / "20260801"
        night.mkdir(parents=True)
        clip = night / "RO000H_20260801_190512_color.mkv"
        clip.write_bytes(b"")
        (night / (clip.name + ".locked")).write_bytes(b"")
        cfg = {"videocapture_path": str(tmp_path), "segment_duration": 20}

        monkeypatch.setattr(od, "_load_night_detections", lambda *a, **k: [dict(_DET)])
        monkeypatch.setattr(
            od, "fetch_night_trajectories",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("fetched")),
        )
        seen: list[list[dict]] = []
        monkeypatch.setattr(
            od, "render_details_clip",
            lambda c, d, cf, dst=None: seen.append(d) or c,
        )
        assert od.run_night(
            "RO000H", "20260801", cfg,
            trajectories={"2026-08-01T19:05:20": _TRAJ},
        ) == 1
        assert seen[0][0]["trajectory"] == _TRAJ


def test_a_session_in_both_rms_trees_is_read_once(tmp_path):
    """RMS mirrors a session into ArchivedFiles at the end of the night; the
    loader read it from CapturedFiles too, so every meteor got two rows."""
    ff = "FF_RO000H_20260801_190512_123_0000000.fits"
    ftp = (
        "Meteor Count = 000001\n-----\n"
        f"{ff}\nRO000H 0001 0002 0025.00 0 0 0 0 0 0\n"
        "0000.0000 100.0 100.0 10.0 20.0 130.0 45.0 100 -1.00 0 10 0\n"
        "0025.0000 110.0 110.0 11.0 21.0 131.0 46.0 100 -0.50 0 10 0\n"
    )
    for sub in ("CapturedFiles", "ArchivedFiles"):
        sess = tmp_path / "rms" / sub / "RO000H_20260801_180000_000000"
        sess.mkdir(parents=True)
        (sess / "FTPdetectinfo_RO000H_20260801_180000_000000.txt").write_text(ftp)
    cfg = {"stations": {"RO000H": {"rms_data_path": str(tmp_path / "rms")}}}
    rows = od._load_night_detections("RO000H", "20260801", cfg)
    assert [(r["ff_file"], r["meteor_no"]) for r in rows] == [(ff, 1)]


def _ro001r_night(tmp_path: Path) -> dict:
    """gmnro22 (Ferigile) RO001R, night 20261110: one meteor at frame 200 of
    the FF block that began at 21:20:28, so it flew at 21:20:36."""
    ff = "FF_RO001R_20261110_212028_000_0123456.fits"
    ftp = (
        "Meteor Count = 000001\n-----\n"
        f"{ff}\nRO001R 0001 0002 0025.00 0 0 0 0 0 0\n"
        "0200.0000 100.0 100.0 10.0 20.0 130.0 45.0 100 -1.00 0 10 0\n"
        "0210.0000 110.0 110.0 11.0 21.0 131.0 46.0 100 -0.50 0 10 0\n"
    )
    sess = tmp_path / "rms" / "CapturedFiles" / "RO001R_20261110_160000_000000"
    sess.mkdir(parents=True)
    (sess / "FTPdetectinfo_RO001R_20261110_160000_000000.txt").write_text(ftp)
    return {"stations": {"RO001R": {"rms_data_path": str(tmp_path / "rms")}}}


def test_a_meteor_late_in_its_ff_block_is_in_the_clip_it_flew_in(tmp_path):
    """The FF name is when the 256-frame block began, up to 10.24 s before the
    meteor. Matched on it, the RO001R meteor landed in the 21:20:12 clip, not
    the 21:20:32 clip the lock stitched it into."""
    dets = od._load_night_detections("RO001R", "20261110", _ro001r_night(tmp_path))
    assert len(dets) == 1
    earlier = "RO001R_20261110_212012_color.mkv"
    actual = "RO001R_20261110_212032_color.mkv"
    assert od.match_detections_to_clip(actual, dets, 20) == dets
    assert od.match_detections_to_clip(earlier, dets, 20) == []
    assert od.format_meteor_row(dets[0])[1] == "21:20:36Z"


def test_the_gmn_trajectory_attaches_to_the_meteor_time(tmp_path):
    dets = od._load_night_detections("RO001R", "20261110", _ro001r_night(tmp_path))
    assert od.attach_trajectories(dets, {"2026-11-10T21:20:36": _TRAJ}) == 1
    assert dets[0]["trajectory"] == _TRAJ
