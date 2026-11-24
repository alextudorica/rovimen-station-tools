"""Adaptive "sky match" colour — the fleet default for clips and stacks.

Covers the pieces that replaced the per-night frozen gains: the per-chunk
per-channel curve fit, the chroma scale with its pre-compensation, the
moonlight/twilight brightness lift (and the small ephemeris behind it), the
ffmpeg rendering of the same curve, the encoder wiring (per clip, explicit
BT.709 conversion + tags), the stacker wiring, and the legacy opt-out.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import color_calibration as cc

UTC = timezone.utc
GHIRDOVENI = (44.607966, 25.282230)
HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _sky(shape=(90, 160, 3), rgb=(84.0, 67.0, 85.6), noise=6.0, seed=3) -> np.ndarray:
    """A raw IR-magenta night sky (the RO001L 02:51 raw sky) with a few stars."""
    rng = np.random.default_rng(seed)
    img = np.empty(shape, dtype=np.float32)
    for c, v in enumerate(rgb):
        img[..., c] = v
    img += rng.normal(0.0, noise, size=shape)
    h, w = shape[:2]
    for y, x in ((0.05, 0.05), (0.45, 0.62), (0.78, 0.2)):
        img[int(y * h), int(x * w)] = 250.0
    return np.clip(img, 0, 255).astype(np.uint8)


def _sky_ratios(img: np.ndarray) -> tuple[float, float, float]:
    px = cc.sky_pixels(img)
    r, g, b = (float(px[:, i].mean()) for i in range(3))
    luma = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return r / g, b / g, luma


# ---------------------------------------------------------------------------
# Ephemeris + brightness lift
# ---------------------------------------------------------------------------

class TestSunMoon:
    def test_new_moon_at_the_2026_08_12_solar_eclipse(self):
        e = cc.sun_moon_state(datetime(2026, 8, 12, 17, 46, tzinfo=UTC), 64.13, -21.94)
        assert e["moon_illum"] < 0.01
        assert abs(e["moon_alt"] - e["sun_alt"]) < 1.5

    def test_full_moon_at_the_2026_08_28_lunar_eclipse(self):
        e = cc.sun_moon_state(datetime(2026, 8, 28, 4, 13, tzinfo=UTC), 44.65, 25.67)
        assert e["moon_illum"] > 0.99
        assert e["moon_rel_flux"] > 0.95

    def test_sun_altitude_at_the_june_solstice(self):
        e = cc.sun_moon_state(datetime(2026, 6, 21, 12, 0, tzinfo=UTC), 51.477, 0.0)
        assert abs(e["sun_alt"] - 61.95) < 0.3


class TestBrightnessBoost:
    P = cc.resolve_adaptive({})

    def test_no_location_or_time_means_the_dark_target(self):
        assert cc.sky_brightness_boost(self.P, None, datetime(2026, 9, 23, 20, tzinfo=UTC)) == 0.0
        assert cc.sky_brightness_boost(self.P, GHIRDOVENI, None) == 0.0

    def test_the_reference_clip_time_gets_no_lift(self):
        # RO001L_20260924_025155 — the fleet reference — sun at -14 deg, moon down.
        when = datetime(2026, 9, 24, 2, 52, 5, tzinfo=UTC)
        assert cc.sky_brightness_boost(self.P, GHIRDOVENI, when) == 0.0

    def test_a_gibbous_moon_brightens_the_sky_a_bit(self):
        when = datetime(2026, 9, 23, 20, 0, tzinfo=UTC)
        boost = cc.sky_brightness_boost(self.P, GHIRDOVENI, when)
        assert 0.15 < boost < float(self.P["moon_boost"])

    def test_late_twilight_lifts_most_and_is_capped(self):
        when = datetime(2026, 9, 24, 3, 51, tzinfo=UTC)  # sun ~ -3.6 deg
        boost = cc.sky_brightness_boost(self.P, GHIRDOVENI, when)
        assert boost == pytest.approx(float(self.P["twilight_boost"]), abs=0.01)
        capped = cc.sky_brightness_boost({**self.P, "max_boost": 0.3}, GHIRDOVENI, when)
        assert capped == pytest.approx(0.3)


# ---------------------------------------------------------------------------
# Fit + numpy rendering
# ---------------------------------------------------------------------------

class TestSkyMatchFit:
    def test_sky_lands_on_the_target(self):
        img = _sky()
        curve = cc.resolve_adaptive_curve(img, {}, "stack")
        rg, bg, luma = _sky_ratios(cc.apply_adaptive_np(img, curve))
        p = cc.resolve_adaptive({}, "stack")
        assert rg == pytest.approx(float(p["r_g"]), abs=0.012)
        assert bg == pytest.approx(float(p["b_g"]), abs=0.012)
        assert luma == pytest.approx(float(p["sky_luma"]), abs=1.0)

    def test_desaturation_is_pre_compensated_so_the_sky_still_lands(self):
        img = _sky()
        for sat in (1.0, 0.94, 0.8):
            cfg = {"color_adaptive": {"saturation": sat}}
            curve = cc.resolve_adaptive_curve(img, cfg, "stack")
            rg, bg, _ = _sky_ratios(cc.apply_adaptive_np(img, curve))
            assert rg == pytest.approx(0.935, abs=0.015), sat
            assert bg == pytest.approx(1.13, abs=0.015), sat

    def test_black_and_white_stay_put(self):
        curve = cc.resolve_adaptive_curve(_sky(), {}, "stack")
        edge = np.array([[[0, 0, 0], [255, 255, 255]]], dtype=np.uint8)
        out = cc.apply_adaptive_np(edge, curve)
        assert out[0, 0].tolist() == [0, 0, 0]
        assert out[0, 1].tolist() == [255, 255, 255]

    def test_different_skies_converge_on_one_look(self):
        """Moonlit grey-green, dark magenta and a dim camera all get one colour;
        the two the exponent cap can reach also get one brightness, and the
        moonlit one lands brighter instead of being crushed to it."""
        skies = [_sky(rgb=(111, 95, 106)), _sky(rgb=(84, 67, 86)), _sky(rgb=(24, 13, 30), noise=3)]
        out = []
        for img in skies:
            curve = cc.resolve_adaptive_curve(img, {}, "stack")
            out.append(_sky_ratios(cc.apply_adaptive_np(img, curve)))
        for rg, bg, _ in out:
            assert rg == pytest.approx(out[1][0], abs=0.02)
            assert bg == pytest.approx(out[1][1], abs=0.02)
        assert out[2][2] == pytest.approx(out[1][2], abs=1.5)
        assert out[1][2] + 5 < out[0][2] < 99

    def test_moonlight_raises_the_target(self):
        img = _sky()
        dark = cc.resolve_adaptive_curve(img, {}, "stack", location=GHIRDOVENI,
                                         when=datetime(2026, 9, 24, 2, 52, tzinfo=UTC))
        moon = cc.resolve_adaptive_curve(img, {}, "stack", location=GHIRDOVENI,
                                         when=datetime(2026, 9, 23, 20, 0, tzinfo=UTC))
        assert moon.boost > dark.boost == 0.0
        assert sum(moon.target) > sum(dark.target) * 1.15

    def test_too_dark_to_fit_is_left_alone(self):
        assert cc.resolve_adaptive_curve(np.zeros((32, 32, 3), np.uint8), {}, "stack") is None

    def test_already_calibrated_skips_the_second_chroma_scale(self):
        curve = cc.resolve_adaptive_curve(_sky(), {}, "stack", already_calibrated=True)
        assert curve.saturation == 1.0

    def test_frames_fit_uses_every_frame(self):
        frames = np.stack([_sky(seed=s) for s in range(4)])
        curve = cc.resolve_adaptive_curve(frames, {}, "video")
        out = np.stack([cc.apply_adaptive_np(f, curve) for f in frames])
        rg, bg, _ = _sky_ratios(out.astype(np.float32).mean(axis=0))
        assert rg == pytest.approx(0.935, abs=0.015) and bg == pytest.approx(1.13, abs=0.015)

    def test_rotation(self):
        img = _sky()
        img[0, 0] = (255, 255, 255)
        curve = cc.resolve_adaptive_curve(img, {}, "stack")
        out = cc.apply_adaptive_np(img, curve, rotate=True)
        assert out[-1, -1].tolist() == [255, 255, 255]


class TestSharedCurve:
    """One exponent for all three channels (2026-09-25): under a bright moon
    the per-channel exponents reached 2-3 and turned moonlit cloud pink while
    the sky average sat on target."""

    @staticmethod
    def _scene(sky=(84.0, 67.0, 86.0), cloud_scale=2.4, clip_frac=0.0, seed=5):
        """A sky with a bright cloud of the SAME raw colour, plus stars."""
        rng = np.random.default_rng(seed)
        img = np.empty((120, 200, 3), np.float32)
        img[:] = sky
        img[:40, :80] = np.array(sky) * cloud_scale         # moonlit cloud
        img += rng.normal(0.0, 3.0, img.shape)
        if clip_frac:
            n = int(clip_frac * 120)
            img[-n:, :, :] = 255.0                            # overexposed band
        img[60, 150] = img[90, 30] = 255.0
        return np.clip(img, 0, 255).astype(np.uint8)

    @staticmethod
    def _ratios(region):
        r, g, b = (float(region[..., c].astype(np.float32).mean()) for c in range(3))
        return r / g, b / g

    def test_bright_cloud_keeps_the_sky_colour(self):
        img = self._scene()
        curve = cc.resolve_adaptive_curve(img, {}, "stack")
        out = cc.apply_adaptive_np(img, curve)
        sky = self._ratios(out[60:, 100:])
        cloud = self._ratios(out[5:35, 5:75])
        assert cloud[0] == pytest.approx(sky[0], abs=0.03)
        assert cloud[1] == pytest.approx(sky[1], abs=0.03)

    def test_gains_only_attenuate_and_the_exponent_is_shared(self):
        curve = cc.resolve_adaptive_curve(self._scene(), {}, "stack")
        assert max(curve.gains) == pytest.approx(1.0)
        assert all(0.0 < a <= 1.0 for a in curve.gains)
        assert cc.ADAPTIVE_DEFAULTS["exp_min"] <= curve.exponent <= cc.ADAPTIVE_DEFAULTS["exp_max"]

    def test_moonlit_sky_is_capped_not_crushed(self):
        img = _sky(rgb=(180.0, 150.0, 170.0))
        curve = cc.resolve_adaptive_curve(img, {}, "stack")
        assert curve.capped and curve.exponent == pytest.approx(cc.ADAPTIVE_DEFAULTS["exp_max"])
        rg, bg, luma = _sky_ratios(cc.apply_adaptive_np(img, curve))
        assert rg == pytest.approx(0.935, abs=0.02) and bg == pytest.approx(1.13, abs=0.02)
        assert luma > 2 * cc.ADAPTIVE_DEFAULTS["sky_luma"]

    def test_overexposed_frame_gets_gains_but_no_tone_curve(self):
        img = self._scene(sky=(232.0, 200.0, 169.0), cloud_scale=1.0, clip_frac=0.3)
        curve = cc.resolve_adaptive_curve(img, {}, "stack")
        assert curve.tone_skipped and curve.exponent == 1.0
        assert curve.clipped_frac > cc.ADAPTIVE_DEFAULTS["clip_max_frac"]
        out = cc.apply_adaptive_np(img, curve)
        rg, bg = self._ratios(out[:60, :])
        assert rg == pytest.approx(0.935, abs=0.03) and bg == pytest.approx(1.13, abs=0.03)
        assert out[-1, 0].tolist() == [255, 255, 255]

    def test_a_mostly_clipped_frame_is_left_as_captured(self):
        img = self._scene(sky=(255.0, 242.0, 218.0), cloud_scale=1.0, clip_frac=0.6)
        assert cc.resolve_adaptive_curve(img, {}, "stack") is None

    def test_a_few_clipped_stars_do_not_switch_off_the_curve(self):
        curve = cc.resolve_adaptive_curve(self._scene(), {}, "stack")
        assert not curve.tone_skipped and curve.exponent != 1.0

    def test_saturated_pixels_stay_white_through_the_knee(self):
        curve = cc.resolve_adaptive_curve(self._scene(), {}, "stack")
        ramp = np.stack([np.arange(256, dtype=np.uint8)] * 3, axis=-1)[None]
        out = cc.apply_adaptive_np(ramp, curve)
        assert out[0, 255].tolist() == [255, 255, 255]
        assert (np.diff(out[0].astype(int), axis=0) >= 0).all(), "curve must be monotonic"

    @pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
    def test_ffmpeg_matches_numpy_across_the_knee(self, tmp_path):
        curve = cc.resolve_adaptive_curve(self._scene(), {}, "video")
        img = np.zeros((64, 256, 3), np.uint8)
        img[:, :, 0] = np.arange(256, dtype=np.uint8)[None, :]
        img[:, :, 1] = np.arange(256, dtype=np.uint8)[::-1][None, :]
        img[:, :, 2] = np.arange(256, dtype=np.uint8)[None, :]
        src = tmp_path / "ramp.rgb"
        src.write_bytes(img.tobytes())
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "rawvideo",
             "-pix_fmt", "rgb24", "-s", "256x64", "-i", str(src),
             "-vf", cc.build_adaptive_ffmpeg_filter(curve),
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            capture_output=True, check=True).stdout
        got = np.frombuffer(out, np.uint8).reshape(64, 256, 3).astype(int)
        want = cc.apply_adaptive_np(img, curve, neutral_highlights=False).astype(int)
        assert np.abs(got - want).max() <= 2


class TestConfig:
    def test_enabled_by_default_everywhere(self):
        assert cc.adaptive_enabled({})
        assert cc.adaptive_enabled({}, "video") and cc.adaptive_enabled({}, "stack")

    def test_camera_and_product_opt_out(self):
        assert not cc.adaptive_enabled({"color_adaptive": {"enabled": False}})
        cfg = {"color_adaptive_video": {"enabled": False}}
        assert not cc.adaptive_enabled(cfg, "video") and cc.adaptive_enabled(cfg, "stack")

    def test_product_override_merges_over_camera_over_fleet(self):
        cfg = {"color_adaptive": {"b_g": 1.2, "sky_luma": 50}, "color_adaptive_stack": {"sky_luma": 40}}
        assert cc.resolve_adaptive(cfg, "stack")["sky_luma"] == 40
        assert cc.resolve_adaptive(cfg, "video")["sky_luma"] == 50
        assert cc.resolve_adaptive(cfg, "video")["b_g"] == 1.2
        assert cc.resolve_adaptive(cfg, "video")["r_g"] == cc.ADAPTIVE_DEFAULTS["r_g"]

    def test_unknown_product_is_rejected(self):
        with pytest.raises(ValueError):
            cc.resolve_adaptive({}, "night")


# ---------------------------------------------------------------------------
# ffmpeg rendering
# ---------------------------------------------------------------------------

class TestFfmpegFilter:
    def test_filter_is_rgb_only_and_comma_free_inside_the_lut(self):
        curve = cc.resolve_adaptive_curve(_sky(), {}, "video")
        vf = cc.build_adaptive_ffmpeg_filter(curve)
        lut, mixer = vf.split(",")
        assert lut.startswith("lutrgb=") and mixer.startswith("colorchannelmixer=")
        assert "eq=" not in vf, "eq=saturation shifts the sky toward green"

    def test_no_mixer_without_desaturation(self):
        curve = cc.resolve_adaptive_curve(_sky(), {"color_adaptive": {"saturation": 1.0}}, "video")
        assert "," not in cc.build_adaptive_ffmpeg_filter(curve)

    @pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
    def test_ffmpeg_render_matches_numpy(self, tmp_path):
        img = _sky(shape=(64, 64, 3))
        src = tmp_path / "in.rgb"
        src.write_bytes(img.tobytes())
        curve = cc.resolve_adaptive_curve(img, {}, "video")
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "rawvideo",
             "-pix_fmt", "rgb24", "-s", "64x64", "-i", str(src),
             "-vf", cc.build_adaptive_ffmpeg_filter(curve),
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            capture_output=True, check=True).stdout
        got = np.frombuffer(out, np.uint8).reshape(64, 64, 3).astype(int)
        want = cc.apply_adaptive_np(img, curve, neutral_highlights=False).astype(int)
        assert np.abs(got - want).max() <= 2


# ---------------------------------------------------------------------------
# Encoder wiring
# ---------------------------------------------------------------------------

def _make_clip(path: Path, rgb=(0x54, 0x43, 0x56), seconds: int = 1) -> None:
    """A tiny full-range, BT.709-tagged magenta 'night sky' clip, like the cameras'."""
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", f"color=c=0x{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}:s=160x96:r=25:d={seconds},"
               "noise=alls=12:allf=t",
         "-vf", "scale=out_color_matrix=bt709:out_range=pc,format=yuvj420p",
         "-c:v", "libx264", "-preset", "ultrafast", "-crf", "12",
         "-colorspace", "bt709", "-color_range", "pc", str(path)],
        check=True)


class TestEncoderCommand:
    def _cmd(self, calib_filters, use_vaapi=False):
        import encoder
        return encoder._build_cmd(
            Path("in.mkv"), Path("out.mkv"), 1, gain_r=1.0, gain_g=1.0, gain_b=1.0,
            gamma=1.0, rotate=False, overlay_cfg=None, station_id="", station_cfg={},
            chunk_epoch=0, vaapi_device="/dev/dri/renderD128", use_vaapi=use_vaapi,
            calib_filters=calib_filters)

    @pytest.mark.parametrize("use_vaapi", [False, True])
    def test_adaptive_clip_converts_explicitly_to_bt709(self, use_vaapi):
        nodes = ["lutrgb=r=255*exp(1.2*log(val/255))+0.5:g=val:b=val"]
        joined = " ".join(self._cmd(nodes, use_vaapi=use_vaapi))
        assert "lutrgb=" in joined
        assert "scale=out_color_matrix=bt709:out_range=tv" in joined
        assert "colorchannelmixer=rr=1.0000" not in joined

    @pytest.mark.parametrize("use_vaapi", [False, True])
    def test_one_explicit_conversion_straight_after_the_colour_nodes(self, use_vaapi):
        """The OSD bar and logo must run on YUV that is already BT.709: an RGB
        frame reaching the logo overlay is converted implicitly per frame (2.7x
        the CPU of the whole chain, and a small B/G shift)."""
        import encoder
        chain = encoder._cpu_filter_chain(1.0, 1.0, 1.0, 1.0, rotate=False, bar_h=40,
                                          annotation=["drawtext=text=X"],
                                          calib_filters=["lutrgb=r=val", "colorchannelmixer=rr=1"])
        conv = "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p"
        assert chain[:3] == ["lutrgb=r=val", "colorchannelmixer=rr=1", conv]
        assert chain.index(conv) < chain.index("pad=iw:ih+40:0:0:black")
        joined = " ".join(self._cmd(["lutrgb=r=val"], use_vaapi=use_vaapi))
        assert joined.count("out_color_matrix") == 1
        assert "setparams=color_primaries=bt709" in joined

    def test_full_range_station_converts_to_full_range(self):
        import encoder
        chain = encoder._cpu_filter_chain(1.0, 1.0, 1.0, 1.0, rotate=False, full_range=True,
                                          calib_filters=["lutrgb=r=val"])
        assert "scale=out_color_matrix=bt709:out_range=pc,format=yuv420p" in chain

    def test_legacy_clip_is_untouched(self):
        joined = " ".join(self._cmd(None))
        assert "out_color_matrix" not in joined

    def test_cpu_chain_uses_the_adaptive_nodes_instead_of_gains(self):
        import encoder
        chain = encoder._cpu_filter_chain(0.7, 1.0, 0.8, 1.2, rotate=True,
                                          calib_contrast=1.3, calib_filters=["lutrgb=r=val"])
        conv = "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p"
        assert chain == ["vflip", "hflip", "lutrgb=r=val", conv]
        # sky too dark to fit: colour left as captured, but still converted and tagged once
        assert encoder._cpu_filter_chain(0.7, 1.0, 0.8, 1.0, rotate=False, calib_filters=[]) == [conv]

    @pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
    def test_every_clip_is_fitted_on_its_own_sky(self, tmp_path, monkeypatch):
        """No per-night cache: two clips of one night get two fits and two
        different curves, and the output is BT.709-tagged."""
        import encoder
        import flags_manager

        station, night = "RO000H", "20260322"
        cap = tmp_path / "color_capture" / station / night
        cap.mkdir(parents=True)
        cfg = {
            "videocapture_path": str(tmp_path / "color_capture"),
            "segment_duration": 20, "compression_level": 2,
            "stations": {station: {"rotate": False}},
            "services": {"reencode": {"enabled": True}},
            "overlay": {"enabled": False},
            "capabilities": {"hwencode": False},
        }
        clips = []
        for hhmm, rgb in (("210000", (0x54, 0x43, 0x56)), ("211000", (0x6f, 0x5f, 0x6a))):
            mkv = cap / f"{station}_{night}_{hhmm}_color.mkv"
            _make_clip(mkv, rgb)
            flags_manager.mark_ready(station, night, mkv.name, cfg)
            clips.append(mkv)

        fits = []
        real = cc.resolve_adaptive_from_mkv

        def spy(mkv, station_cfg, **kw):
            curve = real(mkv, station_cfg, **kw)
            fits.append((Path(mkv).name, curve))
            return curve

        monkeypatch.setattr(cc, "resolve_adaptive_from_mkv", spy)
        encoder._calibration_cache.clear()
        for mkv in clips:
            assert encoder.process_chunk(mkv, cfg)

        assert [name for name, _ in fits] == [c.name for c in clips]
        assert ((fits[0][1].gains, fits[0][1].exponent)
                != (fits[1][1].gains, fits[1][1].exponent))
        tags = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=color_space,color_primaries,color_transfer,color_range",
             "-of", "csv=p=0", str(clips[0])],
            capture_output=True, text=True, check=True).stdout.strip()
        assert tags.split(",") == ["tv", "bt709", "bt709", "bt709"]


# ---------------------------------------------------------------------------
# Stacker wiring
# ---------------------------------------------------------------------------

class TestStackerWiring:
    def _finish(self, tmp_path, maxpx, station_cfg, is_encoded=False):
        import stacker
        mkv = tmp_path / "RO000T_20260923_235000_color.mkv"
        mkv.touch()
        cfg = {"videocapture_path": str(tmp_path), "overlay": {"enabled": False},
               "stations": {"RO000T": station_cfg}}
        assert stacker._finish_stack(
            mkv=mkv, maxpx=maxpx, avgpx=maxpx.copy(), station_id="RO000T",
            night_str="20260923", date_str="20260923", time_str="235000", cfg=cfg,
            is_encoded=is_encoded, refpx=None)
        stack = tmp_path / "RO000T" / "20260923" / "stacks" / "RO000T_20260923_235000_stack.webp"
        return np.asarray(Image.open(stack).convert("RGB"))

    def test_stack_sky_is_matched_and_rotated(self, tmp_path):
        img = _sky()
        img[0, 0] = (255, 255, 255)
        out = self._finish(tmp_path, img, {"rotate": True})
        assert out[-1, -1].tolist() == [255, 255, 255]
        rg, bg, _ = _sky_ratios(out)
        assert rg == pytest.approx(0.935, abs=0.015) and bg == pytest.approx(1.13, abs=0.015)

    def test_encoded_chunk_keeps_the_clip_colour_untouched(self, tmp_path):
        """The re-encode already coloured and turned the clip; a second curve
        would colour it twice."""
        img = _sky()
        img[0, 0] = (255, 255, 255)
        out = self._finish(tmp_path, img, {"rotate": True}, is_encoded=True)
        assert np.array_equal(out, img)

    def test_legacy_camera_keeps_the_gains_pipeline(self, tmp_path, monkeypatch):
        called = {}

        def fake(ref, station_cfg, product=None):
            called["product"] = product
            return 1.0, 1.0, 1.0, 1.0

        monkeypatch.setattr(cc, "resolve_calibration_from_frame", fake)
        self._finish(tmp_path, _sky(), {"color_adaptive": {"enabled": False}})
        assert called["product"] == "stack"


# ---------------------------------------------------------------------------
# Processed timelapse + details clip
# ---------------------------------------------------------------------------

def test_processed_timelapse_does_not_recalibrate_sky_matched_stacks(tmp_path, monkeypatch):
    import timelapse_build

    def boom(*a, **k):
        raise AssertionError("sky-matched stacks must not be calibrated twice")

    monkeypatch.setattr(cc, "apply_calibration_np", boom)
    stacks = tmp_path / "stacks"
    stacks.mkdir()
    for i in range(4):
        Image.fromarray(_sky(seed=i)).save(stacks / f"GH662S_20260916_18{i:02d}00_stack.webp")
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg not installed")
    out = timelapse_build.build_timelapse("20260916", "GH662S", stacks, tmp_path / "out",
                                          station_cfg={"rms": False}, cfg={})
    assert out is not None and out.is_file()


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_details_clip_repeats_the_base_colour_tags(tmp_path):
    import overlay_details
    tagged = tmp_path / "tagged.mkv"
    _make_clip(tagged)
    args = overlay_details._colour_tag_args(tagged)
    assert args[args.index("-colorspace") + 1] == "bt709"
    assert args[args.index("-color_range") + 1] == "pc"
    plain = tmp_path / "plain.mkv"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "color=c=gray:s=64x64:d=1", "-c:v", "libx264", "-preset",
                    "ultrafast", str(plain)], check=True)
    assert "-colorspace" not in overlay_details._colour_tag_args(plain)
