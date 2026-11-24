"""Colour parity between the clip path and the stack path.

Three defects this covers:

1. Night-level composites (``np.maximum`` over a night of stacks, and the
   ``max - avg`` meteor composite) were saved with no white balance of their
   own.  Per-pixel max is not linear — it lifts every pixel to its brightest
   value of the night — so a residual cast that reads as near-black at chunk
   level becomes a visible colour wash at composite level.
2. ``full_range_fix`` was honoured when deriving the clip's gains but not the
   stack's, so on those stations the two derived gains from differently
   scaled references.
3. ``background_subtract_pct`` moved the stack's white-balance reference
   toward black while the clip still derived its gains from untouched frames.
"""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

import color_calibration as cc
import stacker
import timelapse_build


def _cast_image(shape=(64, 96, 3), rgb=(150, 100, 190)) -> np.ndarray:
    """Uniform-ish image with a magenta cast plus a little texture."""
    img = np.zeros(shape, dtype=np.uint8)
    for c, v in enumerate(rgb):
        img[:, :, c] = v
    rng = np.random.default_rng(7)
    noise = rng.integers(-6, 7, size=shape, endpoint=False)
    return np.clip(img.astype(np.int16) + noise, 0, 255).astype(np.uint8)


# The legacy pipeline (neutral 1:1:1 composites, color_post tone pass) runs
# only for a camera with sky match turned off; sky match is the fleet default.
LEGACY = {'color_adaptive': {'enabled': False}}


def _ratios(img: np.ndarray) -> tuple[float, float]:
    f = img.astype(np.float32)
    luma = f.mean(axis=2)
    lo, hi = np.percentile(luma, 10.0), np.percentile(luma, 85.0)
    m = (luma >= lo) & (luma <= hi)
    px = f[m]
    r, g, b = px[:, 0].mean(), px[:, 1].mean(), px[:, 2].mean()
    return float(r / g), float(b / g)


class TestRebalanceComposite:
    def test_neutralises_a_cast(self):
        img = _cast_image()
        rg0, bg0 = _ratios(img)
        assert rg0 > 1.2 and bg0 > 1.5, 'fixture should start strongly magenta'

        out = cc.rebalance_composite(img, LEGACY)
        rg1, bg1 = _ratios(out)

        assert abs(rg1 - 1.0) < 0.05
        assert abs(bg1 - 1.0) < 0.05

    def test_adaptive_composite_lands_on_the_sky_match_ratio(self):
        img = _cast_image()
        params = cc.resolve_adaptive({}, 'stack')
        rg1, bg1 = _ratios(cc.rebalance_composite(img, {}))
        assert abs(rg1 - float(params['r_g'])) < 0.05
        assert abs(bg1 - float(params['b_g'])) < 0.05

    def test_does_not_re_apply_the_tone_curve(self):
        """Gamma/contrast/vibrance are already baked into the inputs, so a
        neutral composite must come back unchanged, not brightened again."""
        neutral = np.full((32, 32, 3), 120, dtype=np.uint8)
        out = cc.rebalance_composite(neutral, LEGACY)
        assert abs(float(out.mean()) - 120.0) <= 1.0
        # Sky match only white-balances the composite: attenuation-only gains,
        # no tone curve, so it can never come back brighter.
        assert float(cc.rebalance_composite(neutral, {}).mean()) <= 120.5

    def test_skips_a_near_black_composite(self):
        """A difference image with no sky has no meaningful grey point."""
        dark = np.zeros((32, 32, 3), dtype=np.uint8)
        dark[:, :, 2] = 3
        out = cc.rebalance_composite(dark, {})
        assert np.array_equal(out, dark)

    def test_honours_per_camera_knees(self):
        img = _cast_image()
        out = cc.rebalance_composite(img, {'color_post': {'knee_low': 0.1, 'knee_high': 0.2}})
        assert out.shape == img.shape

    def test_rejects_non_rgb(self):
        gray = np.full((16, 16), 100, dtype=np.uint8)
        assert np.array_equal(cc.rebalance_composite(gray, {}), gray)


class TestNightStackIsCalibrated:
    def test_night_stack_is_rebalanced(self, tmp_path):
        thumbs = tmp_path / 'thumbs'
        thumbs.mkdir()
        out_dir = tmp_path / 'out'
        out_dir.mkdir()

        for hhmm in ('210000', '220000', '230000'):
            Image.fromarray(_cast_image()).save(
                thumbs / f'RO000T_20260401_{hhmm}_stack.webp', lossless=True)

        timelapse_build.build_night_stack('20260401', 'RO000T', thumbs, out_dir,
                                          station_cfg=LEGACY)

        produced = out_dir / 'RO000T_20260401_night_stack.webp'
        assert produced.exists()
        rg, bg = _ratios(np.asarray(Image.open(produced).convert('RGB')))
        assert abs(rg - 1.0) < 0.08, f'R/G still {rg:.3f}'
        assert abs(bg - 1.0) < 0.08, f'B/G still {bg:.3f}'

    def test_night_stack_is_rebalanced_to_the_sky_match_ratio(self, tmp_path):
        thumbs = tmp_path / 'thumbs'
        thumbs.mkdir()
        out_dir = tmp_path / 'out'
        out_dir.mkdir()
        for hhmm in ('210000', '220000', '230000'):
            Image.fromarray(_cast_image()).save(
                thumbs / f'RO000T_20260401_{hhmm}_stack.webp', lossless=True)

        timelapse_build.build_night_stack('20260401', 'RO000T', thumbs, out_dir,
                                          station_cfg={})

        params = cc.resolve_adaptive({}, 'stack')
        rg, bg = _ratios(np.asarray(Image.open(
            out_dir / 'RO000T_20260401_night_stack.webp').convert('RGB')))
        assert abs(rg - float(params['r_g'])) < 0.08, f'R/G {rg:.3f}'
        assert abs(bg - float(params['b_g'])) < 0.08, f'B/G {bg:.3f}'

    def test_night_products_are_not_fed_back_as_frames(self, tmp_path):
        """``*_stack.webp`` also matches ``*_color_meteor_stack.webp``, which
        lives in the same folder — including it appends a max-of-differences
        frame to the timelapse and to the night stack."""
        d = tmp_path / 'stacks'
        d.mkdir()
        for name in ('RO000T_20260401_210000_stack.webp',
                     'RO000T_20260401_210020_stack.webp',
                     'RO000T_20260401_color_meteor_stack.webp',
                     'RO000T_20260401_210000_stack_avg.webp'):
            Image.fromarray(_cast_image((8, 8, 3))).save(d / name, lossless=True)

        names = [p.name for p in timelapse_build.chunk_stacks(d)]

        assert names == ['RO000T_20260401_210000_stack.webp',
                         'RO000T_20260401_210020_stack.webp']


PASSTHROUGH = ['-fps_mode', 'passthrough']


@pytest.fixture(autouse=True)
def _modern_ffmpeg(monkeypatch):
    """Pin the passthrough flag so these tests do not depend on the CI box's ffmpeg."""
    monkeypatch.setattr(stacker, "passthrough_args", lambda: list(PASSTHROUGH))


class TestDecodeArgsRange:
    def test_full_range_prefix_matches_the_clip_path(self):
        _, vf = stacker._decode_args(0, 0, full_range=True)
        assert vf == ['-vf', 'scale=in_range=full:out_range=full'] + PASSTHROUGH

    def test_full_range_composes_with_the_bar_crop(self):
        _, vf = stacker._decode_args(40, 0, full_range=True)
        assert vf == ['-vf', 'scale=in_range=full:out_range=full,crop=in_w:in_h-40:0:0'] + PASSTHROUGH

    def test_off_by_default(self):
        assert stacker._decode_args(0, 0)[1] == PASSTHROUGH
        assert stacker._decode_args(40, 0)[1] == ['-vf', 'crop=in_w:in_h-40:0:0'] + PASSTHROUGH


class TestDecodeArgsFrameRate:
    """Chunks whose container implies a nominal 1000 fps must not be padded.

    ffmpeg's default CFR output duplicates every coded frame up to the nominal
    rate, so a 500-frame chunk decodes to ~20,000 frames and the stack costs 40x
    what it should. Asserting the flag is present in every argument combination
    is the only check that catches a regression here: the output is numerically
    identical either way, just 40x slower to produce.
    """

    def test_passthrough_is_always_emitted(self):
        for bar_h in (0, 40):
            for full_range in (False, True):
                _, vf = stacker._decode_args(bar_h, 0, full_range=full_range)
                assert vf[-2:] == PASSTHROUGH, (bar_h, full_range, vf)


class TestWhiteBalanceReference:
    def test_pedestal_subtraction_does_not_move_the_reference(self, tmp_path, monkeypatch):
        """refpx must be the un-subtracted mean: it is the frame the clip's
        own gains were derived from, so the two stay in one colour space."""
        captured = {}

        def fake_resolve(ref, station_cfg, product=None):
            captured['ref'] = ref
            return 1.0, 1.0, 1.0, 1.0

        monkeypatch.setattr(cc, 'resolve_calibration_from_frame', fake_resolve)

        sky = np.full((16, 16, 3), 120, dtype=np.uint8)
        pedestal_removed = np.full((16, 16, 3), 5, dtype=np.uint8)
        mkv = tmp_path / 'RO000T_20260401_210000_color.mkv'
        mkv.touch()

        stacker._finish_stack(
            mkv=mkv, maxpx=sky.copy(), avgpx=pedestal_removed,
            station_id='RO000T', night_str='20260401', date_str='20260401',
            time_str='210000',
            cfg={'videocapture_path': str(tmp_path), 'overlay': {'enabled': False},
                 'stations': {'RO000T': LEGACY}},
            is_encoded=False, refpx=sky,
        )

        assert captured['ref'] is sky, 'gains must come from the un-subtracted mean'

    def test_reference_falls_back_to_avgpx_when_no_pedestal(self, tmp_path, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            cc, 'resolve_calibration_from_frame',
            lambda ref, cfg, product=None: (captured.setdefault('ref', ref), 1.0, 1.0, 1.0, 1.0)[1:])

        avg = np.full((16, 16, 3), 90, dtype=np.uint8)
        mkv = tmp_path / 'RO000T_20260401_210000_color.mkv'
        mkv.touch()

        stacker._finish_stack(
            mkv=mkv, maxpx=avg.copy(), avgpx=avg,
            station_id='RO000T', night_str='20260401', date_str='20260401',
            time_str='210000',
            cfg={'videocapture_path': str(tmp_path), 'overlay': {'enabled': False},
                 'stations': {'RO000T': LEGACY}},
            is_encoded=False, refpx=None,
        )

        assert captured['ref'] is avg
