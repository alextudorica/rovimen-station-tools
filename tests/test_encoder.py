"""Tests for encoder.py -- per-chunk MKV re-encoder pure-logic functions.

Covers: _cpu_filter_chain, _build_cmd, _libx264_thread_count, _batch_cfg,
the night rule the encoder uses (rovimen_night.night_of), and the
COMPRESSION_QP / COMPRESSION_CRF lookup dicts.

Skips process_chunk, process_night and the VAAPI probe (require ffmpeg/VAAPI).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from encoder import (
    COMPRESSION_CRF,
    COMPRESSION_QP,
    CRF_RATE_FLOOR,
    _batch_cfg,
    _build_cmd,
    _cpu_filter_chain,
    _libx264_thread_count,
)
from rovimen_night import night_of as _night_date


# ---------------------------------------------------------------------------
# Compression lookup dicts
# ---------------------------------------------------------------------------


class TestCalibrationCache:

    def setup_method(self):
        import encoder
        encoder._calibration_cache.clear()

    def test_cache_populated_and_retrievable(self):
        import encoder
        encoder._calibration_cache[('RO000H', '20260315')] = (1.1, 0.95, 1.05, 0.75)
        with encoder._calibration_cache_lock:
            cached = encoder._calibration_cache.get(('RO000H', '20260315'))
        assert cached == (1.1, 0.95, 1.05, 0.75)

    def test_different_stations_independent(self):
        import encoder
        encoder._calibration_cache[('RO000H', '20260315')] = (1.1, 0.95, 1.05, 0.75)
        encoder._calibration_cache[('RO000J', '20260315')] = (1.2, 0.90, 1.10, 0.80)
        assert encoder._calibration_cache[('RO000H', '20260315')] != encoder._calibration_cache[('RO000J', '20260315')]

    def test_cache_empty_after_clear(self):
        import encoder
        encoder._calibration_cache[('RO000H', '20260315')] = (1.1, 0.95, 1.05, 0.75)
        encoder._calibration_cache.clear()
        assert len(encoder._calibration_cache) == 0


class TestCompressionDicts:
    def test_qp_levels(self):
        assert COMPRESSION_QP == {1: 19, 2: 20, 3: 21, 4: 22}

    def test_crf_levels(self):
        assert COMPRESSION_CRF == {1: 20, 2: 21, 3: 22, 4: 23}

    def test_qp_and_crf_same_keys(self):
        assert set(COMPRESSION_QP.keys()) == set(COMPRESSION_CRF.keys())


# ---------------------------------------------------------------------------
# _cpu_filter_chain
# ---------------------------------------------------------------------------


class TestCpuFilterChain:
    def test_all_defaults_empty(self):
        """Identity gains, no rotate, no annotation -> empty filter list."""
        result = _cpu_filter_chain(1.0, 1.0, 1.0, 1.0, rotate=False)
        assert result == []

    def test_rotate_adds_vflip_hflip(self):
        result = _cpu_filter_chain(1.0, 1.0, 1.0, 1.0, rotate=True)
        assert 'vflip' in result
        assert 'hflip' in result
        assert result.index('vflip') < result.index('hflip')

    def test_non_identity_gains_add_colorchannelmixer(self):
        result = _cpu_filter_chain(1.2, 0.9, 1.1, 1.0, rotate=False)
        assert len(result) == 1
        filt = result[0]
        assert filt.startswith('colorchannelmixer=')
        assert 'rr=1.2000' in filt
        assert 'gg=0.9000' in filt
        assert 'bb=1.1000' in filt

    def test_near_identity_gains_skip_colorchannelmixer(self):
        """Gains within 1e-3 of 1.0 are treated as identity."""
        result = _cpu_filter_chain(1.0005, 0.9998, 1.0001, 1.0, rotate=False)
        for f in result:
            assert 'colorchannelmixer' not in f

    def test_gamma_not_one_adds_eq_filter(self):
        result = _cpu_filter_chain(1.0, 1.0, 1.0, 1.5, rotate=False)
        assert len(result) == 1
        # gamma=1.5 -> eq filter uses 1/1.5 = 0.6667
        assert result[0].startswith('eq=gamma=')
        val = float(result[0].split('=')[2])
        assert abs(val - 1.0 / 1.5) < 1e-3

    def test_gamma_identity_no_eq(self):
        result = _cpu_filter_chain(1.0, 1.0, 1.0, 1.0, rotate=False)
        for f in result:
            assert 'eq=' not in f

    def test_bar_h_positive_adds_pad(self):
        result = _cpu_filter_chain(1.0, 1.0, 1.0, 1.0, rotate=False, bar_h=40)
        assert any('pad=iw:ih+40:0:0:black' in f for f in result)

    def test_bar_h_zero_no_pad(self):
        result = _cpu_filter_chain(1.0, 1.0, 1.0, 1.0, rotate=False, bar_h=0)
        for f in result:
            assert 'pad=' not in f

    def test_annotation_appended_at_end(self):
        ann = ['drawtext=text=hello', 'drawtext=text=world']
        result = _cpu_filter_chain(1.0, 1.0, 1.0, 1.0, rotate=False,
                                   annotation=ann)
        assert result[-2:] == ann

    def test_full_range_adds_scale_at_start(self):
        result = _cpu_filter_chain(1.0, 1.0, 1.0, 1.0, rotate=True,
                                   full_range=True)
        assert result[0] == 'scale=in_range=full:out_range=full'

    def test_full_range_false_no_scale(self):
        result = _cpu_filter_chain(1.0, 1.0, 1.0, 1.0, rotate=False,
                                   full_range=False)
        for f in result:
            assert 'scale=' not in f

    def test_display_levels_contrast(self):
        dl = {'contrast': 1.3}
        result = _cpu_filter_chain(1.0, 1.0, 1.0, 1.0, rotate=False,
                                   display_levels=dl)
        assert any('contrast=1.3' in f for f in result)

    def test_display_levels_brightness(self):
        dl = {'brightness': 0.05}
        result = _cpu_filter_chain(1.0, 1.0, 1.0, 1.0, rotate=False,
                                   display_levels=dl)
        assert any('brightness=0.05' in f for f in result)

    def test_calibration_and_display_levels_are_separate_eq(self):
        """Calibration gamma/contrast (shared with the stack) and the
        video-only display_levels lift live in distinct eq= nodes: calibration
        first, display second, so the two never entangle."""
        dl = {'contrast': 1.2, 'brightness': 0.1}
        result = _cpu_filter_chain(1.0, 1.0, 1.0, 1.5, rotate=False,
                                   display_levels=dl)
        eq_filters = [f for f in result if f.startswith('eq=')]
        assert len(eq_filters) == 2
        calib_eq, display_eq = eq_filters
        assert 'gamma=' in calib_eq
        assert 'contrast=1.2' in display_eq
        assert 'brightness=0.1' in display_eq

    def test_ordering_full_range_rotate_gains_gamma_pad_annotation(self):
        """Full filter chain respects the expected ordering."""
        ann = ['drawtext=test']
        result = _cpu_filter_chain(1.2, 1.0, 1.0, 1.5, rotate=True,
                                   annotation=ann, bar_h=40,
                                   full_range=True)
        # Order: scale, vflip, hflip, colorchannelmixer, eq, pad, annotation
        names = []
        for f in result:
            if f.startswith('scale='):
                names.append('scale')
            elif f == 'vflip':
                names.append('vflip')
            elif f == 'hflip':
                names.append('hflip')
            elif f.startswith('colorchannelmixer='):
                names.append('ccm')
            elif f.startswith('eq='):
                names.append('eq')
            elif f.startswith('pad='):
                names.append('pad')
            elif f.startswith('drawtext='):
                names.append('drawtext')
        assert names == ['scale', 'vflip', 'hflip', 'ccm', 'eq', 'pad', 'drawtext']


# ---------------------------------------------------------------------------
# Stack <-> video colour-post consistency (issue #508)
#
# The reencoded clip and its stack thumbnail must share one colour-post path:
# same adaptive gains, same gamma, same color_post.contrast. The stack applies
# them via color_calibration.apply_calibration_np; the video applies them via
# _cpu_filter_chain -> color_calibration.build_ffmpeg_filter. These tests pin
# that both consume identical resolved parameters and the same builder, so the
# two paths cannot silently drift apart again.
# ---------------------------------------------------------------------------


class TestStackVideoColorPostConsistency:

    def test_video_path_delegates_to_shared_ffmpeg_builder(self):
        """The video calibration node is exactly what build_ffmpeg_filter emits
        for the same gains/gamma/contrast — no re-implemented, divergeable
        inline copy in the encoder."""
        import color_calibration as cc

        gr, gg, gb, gamma, contrast = 0.57, 0.89, 1.0, 0.75, 1.05
        chain = _cpu_filter_chain(gr, gg, gb, gamma, rotate=False,
                                  calib_contrast=contrast)
        expected = cc.build_ffmpeg_filter(
            gr, gg, gb, gamma, rotate=False, contrast=contrast,
        ).split(',')
        assert chain == expected

    def test_calib_contrast_reaches_the_video_eq(self):
        """color_post.contrast (the value the stack applies) is folded into the
        calibration eq of the video path — the pre-fix bug was the encoder
        dropping it and applying only display_levels."""
        chain = _cpu_filter_chain(1.0, 1.0, 1.0, 0.75, rotate=False,
                                  calib_contrast=1.08)
        calib_eq = next(f for f in chain if f.startswith('eq='))
        assert 'contrast=1.0800' in calib_eq

    def test_identity_calib_contrast_emits_no_contrast(self):
        """The fleet default (contrast 1.0) must not inject a contrast term,
        so cameras without an override are byte-for-byte unchanged."""
        chain = _cpu_filter_chain(1.0, 1.0, 1.0, 0.75, rotate=False,
                                  calib_contrast=1.0)
        assert all('contrast=' not in f for f in chain)

    def test_stack_and_video_resolve_the_same_color_post(self):
        """Both paths funnel through resolve_color_post/resolve_calibration_from_frame,
        so a per-camera override lands identically in the stack (numpy) and the
        video (ffmpeg) — same gains, same gamma, same contrast."""
        import numpy as np

        import color_calibration as cc

        # A camera with a real color_post override (gamma + contrast).
        station_cfg = {'color_post': {'gamma': 1.17, 'contrast': 1.05}}
        post = cc.resolve_color_post(station_cfg)

        rng = np.random.default_rng(7)
        ref = np.clip(rng.integers(0, 256, (80, 120, 3)), 0, 255).astype(np.uint8)
        gr, gg, gb, gamma = cc.resolve_calibration_from_frame(ref, station_cfg)

        # Stack path parameters.
        assert gamma == float(post['gamma'])

        # Video path: build the same filter the encoder would.
        vf = cc.build_ffmpeg_filter(
            gr, gg, gb, gamma, rotate=False, contrast=float(post['contrast']),
        )
        # gamma is inverted for ffmpeg's eq convention.
        assert f'gamma={1.0 / gamma:.4f}' in vf
        assert f'contrast={float(post["contrast"]):.4f}' in vf
        assert f'rr={gr:.4f}' in vf and f'gg={gg:.4f}' in vf and f'bb={gb:.4f}' in vf


# ---------------------------------------------------------------------------
# _build_cmd
# ---------------------------------------------------------------------------


class TestBuildCmd:
    """Test _build_cmd for various code paths.

    Mocks build_drawtext_annotations to avoid font-file dependencies.
    """

    @pytest.fixture
    def paths(self, tmp_path):
        src = tmp_path / "DE001B_20260101_210000_color.mkv"
        src.touch()
        dst = tmp_path / "DE001B_20260101_210000_color.mkv.tmp.mkv"
        return src, dst

    @pytest.mark.parametrize("level", [0, 5])
    def test_levels_outside_1_to_4_are_rejected(self, paths, level):
        """Level 0 (a plain copy) was unreachable: process_chunk maps any level
        outside 1-4 to 2 before building a command."""
        src, dst = paths
        with pytest.raises(ValueError):
            _build_cmd(
                src, dst, compression_level=level,
                gain_r=1.0, gain_g=1.0, gain_b=1.0, gamma=1.0,
                rotate=False, overlay_cfg=None, station_id='DE001B',
                station_cfg={}, chunk_epoch=0,
                vaapi_device='/dev/dri/renderD128', use_vaapi=True,
            )

    def test_vaapi_path_includes_device_and_codec(self, paths):
        src, dst = paths
        result = _build_cmd(
            src, dst, compression_level=2,
            gain_r=1.0, gain_g=1.0, gain_b=1.0, gamma=1.0,
            rotate=False, overlay_cfg=None, station_id='DE001B',
            station_cfg={}, chunk_epoch=0,
            vaapi_device='/dev/dri/renderD128', use_vaapi=True,
        )
        assert result is not None
        assert '-vaapi_device' in result
        assert '/dev/dri/renderD128' in result
        assert 'h264_vaapi' in result
        assert '-qp' in result
        assert str(COMPRESSION_QP[2]) in result

    def test_cpu_fallback_includes_libx264_and_crf(self, paths):
        src, dst = paths
        result = _build_cmd(
            src, dst, compression_level=2,
            gain_r=1.0, gain_g=1.0, gain_b=1.0, gamma=1.0,
            rotate=False, overlay_cfg=None, station_id='DE001B',
            station_cfg={}, chunk_epoch=0,
            vaapi_device='', use_vaapi=False,
        )
        assert result is not None
        assert 'libx264' in result
        assert '-crf' in result
        assert str(COMPRESSION_CRF[2]) in result
        assert '-vaapi_device' not in result

    @pytest.mark.parametrize('level', [1, 2, 3, 4])
    def test_vaapi_qp_per_level(self, paths, level):
        src, dst = paths
        result = _build_cmd(
            src, dst, compression_level=level,
            gain_r=1.0, gain_g=1.0, gain_b=1.0, gamma=1.0,
            rotate=False, overlay_cfg=None, station_id='DE001B',
            station_cfg={}, chunk_epoch=0,
            vaapi_device='/dev/dri/renderD128', use_vaapi=True,
        )
        qp_idx = result.index('-qp')
        assert result[qp_idx + 1] == str(COMPRESSION_QP[level])

    @pytest.mark.parametrize('level', [1, 2, 3, 4])
    def test_cpu_crf_per_level(self, paths, level):
        src, dst = paths
        result = _build_cmd(
            src, dst, compression_level=level,
            gain_r=1.0, gain_g=1.0, gain_b=1.0, gamma=1.0,
            rotate=False, overlay_cfg=None, station_id='DE001B',
            station_cfg={}, chunk_epoch=0,
            vaapi_device='', use_vaapi=False,
        )
        crf_idx = result.index('-crf')
        assert result[crf_idx + 1] == str(COMPRESSION_CRF[level])

    def test_vaapi_no_logo_uses_vf(self, paths):
        """Without a logo, VAAPI path uses simple -vf (not filter_complex)."""
        src, dst = paths
        result = _build_cmd(
            src, dst, compression_level=2,
            gain_r=1.0, gain_g=1.0, gain_b=1.0, gamma=1.0,
            rotate=True, overlay_cfg=None, station_id='DE001B',
            station_cfg={}, chunk_epoch=0,
            vaapi_device='/dev/dri/renderD128', use_vaapi=True,
        )
        assert '-vf' in result
        assert '-filter_complex' not in result

    def test_cpu_no_logo_uses_vf_or_bare(self, paths):
        """Without a logo, CPU path uses -vf (or omits it if no filters)."""
        src, dst = paths
        result = _build_cmd(
            src, dst, compression_level=2,
            gain_r=1.0, gain_g=1.0, gain_b=1.0, gamma=1.0,
            rotate=False, overlay_cfg=None, station_id='DE001B',
            station_cfg={}, chunk_epoch=0,
            vaapi_device='', use_vaapi=False,
        )
        # Identity gains, no rotate, no annotation -> no -vf needed
        assert '-filter_complex' not in result

    def test_cpu_with_rotate_has_vf(self, paths):
        src, dst = paths
        result = _build_cmd(
            src, dst, compression_level=2,
            gain_r=1.0, gain_g=1.0, gain_b=1.0, gamma=1.0,
            rotate=True, overlay_cfg=None, station_id='DE001B',
            station_cfg={}, chunk_epoch=0,
            vaapi_device='', use_vaapi=False,
        )
        assert '-vf' in result
        vf_idx = result.index('-vf')
        assert 'vflip' in result[vf_idx + 1]

    def test_cpu_rate_floor_present(self, paths):
        """CPU fallback includes CRF rate floor flags."""
        src, dst = paths
        result = _build_cmd(
            src, dst, compression_level=2,
            gain_r=1.0, gain_g=1.0, gain_b=1.0, gamma=1.0,
            rotate=False, overlay_cfg=None, station_id='DE001B',
            station_cfg={}, chunk_epoch=0,
            vaapi_device='', use_vaapi=False,
        )
        for flag in CRF_RATE_FLOOR:
            assert flag in result

    def test_output_is_last_element(self, paths):
        """Output path is always the last element in the command."""
        src, dst = paths
        for use_vaapi in (True, False):
            result = _build_cmd(
                src, dst, compression_level=2,
                gain_r=1.0, gain_g=1.0, gain_b=1.0, gamma=1.0,
                rotate=False, overlay_cfg=None, station_id='DE001B',
                station_cfg={}, chunk_epoch=0,
                vaapi_device='/dev/dri/renderD128', use_vaapi=use_vaapi,
            )
            assert result[-1] == str(dst)

    def test_starts_with_ffmpeg(self, paths):
        src, dst = paths
        result = _build_cmd(
            src, dst, compression_level=2,
            gain_r=1.0, gain_g=1.0, gain_b=1.0, gamma=1.0,
            rotate=False, overlay_cfg=None, station_id='DE001B',
            station_cfg={}, chunk_epoch=0,
            vaapi_device='', use_vaapi=False,
        )
        assert result[0] == 'ffmpeg'
        assert result[1] == '-y'

    @patch('encoder.build_drawtext_annotations',
           return_value=(['drawtext=text=TEST'], 40))
    def test_vaapi_with_logo_uses_filter_complex(self, mock_ann, tmp_path):
        src = tmp_path / "DE001B_20260101_210000_color.mkv"
        src.touch()
        dst = tmp_path / "out.mkv"
        logo = tmp_path / "logo.png"
        logo.write_bytes(b'\x89PNG\r\n\x1a\n')  # minimal header

        overlay_cfg = {
            'enabled': True,
            'font': '/fake/font.ttf',
            'font_size': 19,
            'network': 'ROVIMEN',
            'style': 'standard',
            'logo': str(logo),
            'logo_opacity': 0.8,
            'show_logo': True,
        }

        result = _build_cmd(
            src, dst, compression_level=2,
            gain_r=1.0, gain_g=1.0, gain_b=1.0, gamma=1.0,
            rotate=False, overlay_cfg=overlay_cfg, station_id='DE001B',
            station_cfg={}, chunk_epoch=1700000000,
            vaapi_device='/dev/dri/renderD128', use_vaapi=True,
        )

        assert '-filter_complex' in result
        fc = result[result.index('-filter_complex') + 1]
        assert 'overlay=' in fc
        # The astromania logo is centred horizontally and BOTTOM-anchored in the
        # bar — never centred over the video (no (H-h)/2).
        assert 'overlay=x=(W-w)/2:y=H-h-4' in fc
        assert '(H-h)/2' not in fc
        # VAAPI path still uploads the composited frame to the GPU.
        assert 'hwupload[out]' in fc

    @patch('encoder.build_drawtext_annotations',
           return_value=(['drawtext=text=TEST'], 40))
    def test_cpu_with_logo_uses_filter_complex(self, mock_ann, tmp_path):
        src = tmp_path / "DE001B_20260101_210000_color.mkv"
        src.touch()
        dst = tmp_path / "out.mkv"
        logo = tmp_path / "logo.png"
        logo.write_bytes(b'\x89PNG\r\n\x1a\n')

        overlay_cfg = {
            'enabled': True,
            'font': '/fake/font.ttf',
            'font_size': 19,
            'network': 'ROVIMEN',
            'style': 'standard',
            'logo': str(logo),
            'logo_opacity': 0.8,
            'show_logo': True,
        }

        result = _build_cmd(
            src, dst, compression_level=2,
            gain_r=1.0, gain_g=1.0, gain_b=1.0, gamma=1.0,
            rotate=False, overlay_cfg=overlay_cfg, station_id='DE001B',
            station_cfg={}, chunk_epoch=1700000000,
            vaapi_device='', use_vaapi=False,
        )

        assert '-filter_complex' in result
        assert 'libx264' in result
        fc = result[result.index('-filter_complex') + 1]
        assert 'overlay=x=(W-w)/2:y=H-h-4' in fc
        assert '(H-h)/2' not in fc
        assert fc.endswith('[out]')

    @patch('encoder.build_drawtext_annotations',
           return_value=(['drawtext=text=TEST'], 40))
    def test_cpu_with_bar_logo_and_gmn_relabels_final_stage(self, mock_ann, tmp_path):
        """libx264 + both logos: the branding fragment's terminal GMN pad is
        renamed to [out] via relabel_fragment_output, not string-sliced.

        The last overlay stage is the GMN corner (its output pad is what must
        become [out]); a stray internal label would produce a dangling graph.
        """
        src = tmp_path / "DE001B_20260101_210000_color.mkv"
        src.touch()
        dst = tmp_path / "out.mkv"
        wm = tmp_path / "astromania.png"
        gmn = tmp_path / "gmn.png"
        wm.write_bytes(b'\x89PNG\r\n\x1a\n')
        gmn.write_bytes(b'\x89PNG\r\n\x1a\n')

        overlay_cfg = {
            'enabled': True, 'font': '/fake/font.ttf', 'font_size': 19,
            'network': 'ROVIMEN', 'style': 'standard',
            'logo': str(wm), 'gmn_logo': str(gmn), 'show_logo': True,
        }

        result = _build_cmd(
            src, dst, compression_level=2,
            gain_r=1.0, gain_g=1.0, gain_b=1.0, gamma=1.0,
            rotate=False, overlay_cfg=overlay_cfg, station_id='DE001B',
            station_cfg={}, chunk_epoch=1700000000,
            vaapi_device='', use_vaapi=False,
        )

        fc = result[result.index('-filter_complex') + 1]
        # Both the bottom-bar astromania logo and the GMN corner are present, and
        # the graph terminates exactly once at [out] with no leftover [gmn*] pad.
        assert 'overlay=x=(W-w)/2:y=H-h-4' in fc    # logo centred, bottom-anchored
        assert '(H-h)/2' not in fc                    # never over the video centre
        assert 'W-w-14' in fc                         # GMN corner (br, margin 14)
        assert fc.endswith('[out]')
        assert fc.count('[out]') == 1
        assert '[gmn' not in fc                        # no dangling terminal pad

    def test_cpu_preset_default_is_fast(self, paths):
        src, dst = paths
        result = _build_cmd(
            src, dst, compression_level=2,
            gain_r=1.0, gain_g=1.0, gain_b=1.0, gamma=1.0,
            rotate=False, overlay_cfg=None, station_id='DE001B',
            station_cfg={}, chunk_epoch=0,
            vaapi_device='', use_vaapi=False,
        )
        idx = result.index('-preset')
        assert result[idx + 1] == 'fast'

    def test_cpu_preset_veryfast(self, paths):
        src, dst = paths
        result = _build_cmd(
            src, dst, compression_level=2,
            gain_r=1.0, gain_g=1.0, gain_b=1.0, gamma=1.0,
            rotate=False, overlay_cfg=None, station_id='DE001B',
            station_cfg={}, chunk_epoch=0,
            vaapi_device='', use_vaapi=False,
            preset='veryfast',
        )
        idx = result.index('-preset')
        assert result[idx + 1] == 'veryfast'

    def test_vaapi_no_preset(self, paths):
        src, dst = paths
        result = _build_cmd(
            src, dst, compression_level=2,
            gain_r=1.0, gain_g=1.0, gain_b=1.0, gamma=1.0,
            rotate=False, overlay_cfg=None, station_id='DE001B',
            station_cfg={}, chunk_epoch=0,
            vaapi_device='/dev/dri/renderD128', use_vaapi=True,
        )
        assert '-preset' not in result

    def test_full_range_in_filters(self, paths):
        """full_range=True propagates scale filter into the command."""
        src, dst = paths
        result = _build_cmd(
            src, dst, compression_level=2,
            gain_r=1.0, gain_g=1.0, gain_b=1.0, gamma=1.0,
            rotate=False, overlay_cfg=None, station_id='DE001B',
            station_cfg={}, chunk_epoch=0,
            vaapi_device='', use_vaapi=False,
            full_range=True,
        )
        vf_idx = result.index('-vf')
        assert 'scale=in_range=full:out_range=full' in result[vf_idx + 1]


# ---------------------------------------------------------------------------
# _libx264_thread_count
# ---------------------------------------------------------------------------


class TestLibx264ThreadCount:
    def test_with_encode_threads_override(self):
        cfg = {'_encode_threads': 4}
        assert _libx264_thread_count(cfg) == 4

    def test_with_encode_threads_as_string(self):
        cfg = {'_encode_threads': '6'}
        assert _libx264_thread_count(cfg) == 6

    @patch('encoder.os.cpu_count', return_value=8)
    def test_without_override_enough_cores(self, _mock_cpu):
        """With 8 cores and 2 cameras, max_threads=7 >= 2*2 -> 2 threads/cam."""
        cfg = {'stations': {'RO000H': {}, 'RO000J': {}}}
        result = _libx264_thread_count(cfg)
        assert result == 2

    @patch('encoder.os.cpu_count', return_value=2)
    def test_without_override_few_cores(self, _mock_cpu):
        """With 2 cores and 3 cameras, max_threads=1 < 3*2 -> 1 thread/cam."""
        cfg = {'stations': {'A': {}, 'B': {}, 'C': {}}}
        result = _libx264_thread_count(cfg)
        assert result == 1

    @patch('encoder.os.cpu_count', return_value=None)
    def test_cpu_count_none_fallback(self, _mock_cpu):
        """os.cpu_count() returning None falls back to 4."""
        cfg = {'stations': {'A': {}}}
        result = _libx264_thread_count(cfg)
        # total=4, max_threads=3, 1 camera -> 3 >= 1*2 -> 2
        assert result == 2

    def test_no_stations_key(self):
        """Missing stations key defaults to 1 camera."""
        cfg = {}
        result = _libx264_thread_count(cfg)
        assert result >= 1

    def test_empty_stations(self):
        """Empty stations dict defaults to 1 camera."""
        cfg = {'stations': {}}
        result = _libx264_thread_count(cfg)
        assert result >= 1


# ---------------------------------------------------------------------------
# _batch_cfg
# ---------------------------------------------------------------------------


class TestBatchCfg:
    @patch('encoder.os.cpu_count', return_value=6)
    def test_default_parallelism(self, _mock_cpu):
        cfg = {}
        result = _batch_cfg(cfg)
        # parallelism = 6 // 2 = 3, threads = 6 // 3 = 2
        assert result['_encode_parallelism'] == 3
        assert result['_encode_threads'] == 2

    @patch('encoder.os.cpu_count', return_value=6)
    def test_custom_parallelism(self, _mock_cpu):
        cfg = {'dawn_encode_parallelism': 2}
        result = _batch_cfg(cfg)
        assert result['_encode_parallelism'] == 2
        assert result['_encode_threads'] == 3  # 6 // 2

    def test_returns_copy(self):
        cfg = {'compression_level': 2}
        result = _batch_cfg(cfg)
        assert result is not cfg
        assert result['compression_level'] == 2

    def test_original_unchanged(self):
        cfg = {'compression_level': 2}
        _batch_cfg(cfg)
        assert '_encode_threads' not in cfg
        assert '_encode_parallelism' not in cfg

    @patch('encoder.os.cpu_count', return_value=1)
    def test_single_core(self, _mock_cpu):
        cfg = {}
        result = _batch_cfg(cfg)
        # parallelism = max(1, 1//2) = max(1, 0) = 1
        # threads = max(1, 1//1) = 1
        assert result['_encode_parallelism'] >= 1
        assert result['_encode_threads'] >= 1

    @patch('encoder.os.cpu_count', return_value=None)
    def test_cpu_count_none(self, _mock_cpu):
        """os.cpu_count() None falls back to 4."""
        cfg = {}
        result = _batch_cfg(cfg)
        # total=4, parallelism=4//2=2, threads=4//2=2
        assert result['_encode_parallelism'] == 2
        assert result['_encode_threads'] == 2

    def test_custom_cpu_cores_override(self):
        """_cpu_cores in cfg overrides os.cpu_count()."""
        cfg = {'_cpu_cores': 12}
        result = _batch_cfg(cfg)
        # parallelism = 12 // 2 = 6, threads = 12 // 6 = 2
        assert result['_encode_parallelism'] == 6
        assert result['_encode_threads'] == 2


# ---------------------------------------------------------------------------
# Night rule (rovimen_night.night_of, used by the encoder)
# ---------------------------------------------------------------------------


class TestNightDate:
    def test_evening_returns_same_date(self):
        assert _night_date('20260101', '210000') == '20260101'

    def test_morning_returns_previous_date(self):
        assert _night_date('20260102', '030000') == '20260101'

    def test_noon_boundary_returns_same_date(self):
        """Hour 12 is not < 12, so returns same date."""
        assert _night_date('20260101', '120000') == '20260101'

    def test_midnight_returns_previous_date(self):
        assert _night_date('20260102', '000000') == '20260101'

    def test_hour_11_returns_previous_date(self):
        assert _night_date('20260315', '113000') == '20260314'

    def test_new_year_boundary(self):
        """Morning of Jan 1 returns Dec 31 of previous year."""
        assert _night_date('20260101', '040000') == '20251231'

    def test_month_boundary(self):
        """Morning of March 1 returns Feb 28 (non-leap) or Feb 29 (leap)."""
        # 2026 is not a leap year
        assert _night_date('20260301', '050000') == '20260228'
        # 2028 is a leap year
        assert _night_date('20280301', '050000') == '20280229'

    def test_evening_just_after_noon(self):
        assert _night_date('20260601', '130000') == '20260601'
