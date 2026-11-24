"""The per-chunk stacker does less work per chunk and writes exactly the same files.

color_capture spawns ``stacker.py --chunk`` once per 20-second chunk, in one
serial lane, and on a six-camera station that lane fell ~40% behind every
night. Most of a chunk's cost was not the decode: a lossless WebP that the
overlay pass read straight back and replaced, a PNG round trip through the
disk, an ffmpeg process per thumbnail, an ffprobe per chunk, and a float32
cast of every decoded frame.

These tests pin both halves of the change: the cheaper path is the one taken,
and what lands on disk is what the old path wrote -- the same stack pixels
(alpha included), the same averages, the same pedestal maths.
"""

from __future__ import annotations

import io
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import flags_manager
import overlay
import rovimen_media
import stacker


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "rovimen-scripts"
FONT = SCRIPTS / "fonts" / "VCR_OSD_MONO_1.001.ttf"
LOGO = SCRIPTS / "assets" / "astromania_text.png"
GMN_LOGO = SCRIPTS / "assets" / "gmn_sphere.png"

CAM = "RO000E"
NIGHT = "20260818"
CHUNK = f"{CAM}_20260818_215030_color.mkv"
STACK_NAME = f"{CAM}_20260818_215030_stack.webp"
EPOCH = 1_800_000_000
CHUNK_EPOCH = int(datetime(2026, 8, 18, 21, 50, 30, tzinfo=timezone.utc).timestamp())
POINTING = {"az": 170.3, "alt": 40.1}

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


def _overlay_cfg(**overrides) -> dict:
    """The fleet's overlay block: cinema bar, operator logo, every field shown."""
    return {
        "enabled": True,
        "style": "cinema",
        "font": str(FONT),
        "font_size": 19,
        "logo": str(LOGO),
        "network": "ROVIMEN",
        "coords": "45.30N  26.11E",
        "text_opacity": 0.8,
        "show_logo": True,
        "show_network": True,
        "show_timestamp": True,
        "show_station": True,
        "show_coords": True,
        "show_pointing": True,
        **overrides,
    }


OVERLAY_VARIANTS = {
    "fleet": {},
    "gmn_corner_logo": {"gmn_logo": str(GMN_LOGO)},
    "no_logo": {"show_logo": False},
    "standard_style": {"style": "standard"},
    "standard_no_logo": {"style": "standard", "show_logo": False},
    "bar_only": {"show_logo": False, "show_network": False, "show_timestamp": False,
                 "show_station": False, "show_coords": False, "show_pointing": False},
}


def _sky(h: int = 360, w: int = 640, seed: int = 0) -> np.ndarray:
    """A noisy night sky with stars and a trail: detail every encoder has to work for."""
    rng = np.random.default_rng(seed)
    img = rng.integers(0, 40, size=(h, w, 3), dtype=np.uint8)
    img[rng.integers(0, h, 300), rng.integers(0, w, 300)] = 255
    img[h // 3, : w // 2] = (250, 240, 200)
    return img


def _legacy_stack(path: Path, image: np.ndarray, ocfg: dict, epoch: int = EPOCH) -> Path:
    """What the stacker published before: a lossless WebP, then annotated in place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    stacker._save_webp_lossless(image, path)
    assert overlay.annotate_still(path, path, ocfg, CAM, POINTING, epoch) is True
    return path


@pytest.fixture
def one_cpu():
    """Run this test, and every ffmpeg it starts, on a single CPU.

    ffmpeg's slice-threaded filters can make a render depend on the core count,
    and at sizes other than the fleet's 1280x720 even on thread timing -- the
    old path itself then differs from run to run. One CPU gives both paths the
    same repeatable graph, so any difference left is a real one.
    """
    if not hasattr(os, "sched_setaffinity"):
        pytest.skip("needs os.sched_setaffinity")
    before = os.sched_getaffinity(0)
    os.sched_setaffinity(0, {min(before)})
    yield
    os.sched_setaffinity(0, before)


def _pixels(path: Path) -> tuple[str, np.ndarray]:
    with Image.open(path) as im:
        return im.mode, np.asarray(im)


def _capture_env(tmp_path: Path, **cfg) -> tuple[dict, Path]:
    capture = tmp_path / "color_capture"
    night_dir = capture / CAM / NIGHT
    night_dir.mkdir(parents=True)
    mkv = night_dir / CHUNK
    mkv.write_bytes(b"\x00" * 1024)
    # Colour is not under test here: with sky match off for the stack and the
    # chunk flagged as already encoded, _finish_stack publishes *image* as given.
    camera = {"rotate": False, **POINTING, "color_adaptive_stack": {"enabled": False}}
    return {"videocapture_path": str(capture), "stations": {CAM: camera}, **cfg}, mkv


def _finish(cfg: dict, mkv: Path, image: np.ndarray) -> bool:
    return stacker._finish_stack(mkv, image, image // 2, CAM, NIGHT,
                                 "20260818", "215030", cfg, True)


def _one_more_generation(path: Path) -> tuple[str, np.ndarray]:
    """*path*'s pixels after the q95 re-save mark_still_overlaid gives a still."""
    with Image.open(path) as im:
        im.load()
        again = io.BytesIO()
        im.save(again, "WEBP", quality=95)
    return _pixels(again)


# ---------------------------------------------------------------------------
# The overlay rendered from memory is the overlay annotate_still wrote, one
# generation of WebP loss earlier.
# ---------------------------------------------------------------------------


@needs_ffmpeg
@pytest.mark.usefixtures("one_cpu")
class TestInMemoryOverlayMatchesTheFilePath:
    @pytest.mark.parametrize("variant", sorted(OVERLAY_VARIANTS))
    def test_pixels_are_annotate_stills_first_encode(self, tmp_path, variant):
        ocfg = _overlay_cfg(**OVERLAY_VARIANTS[variant])
        image = _sky(720, 1280)
        legacy = _legacy_stack(tmp_path / "legacy" / STACK_NAME, image, ocfg)

        rendered = overlay.render_still(Image.fromarray(image, "RGB"), ocfg, CAM, POINTING, EPOCH)
        assert rendered is not None
        new = tmp_path / "new" / STACK_NAME
        overlay.save_overlaid_webp(rendered, new)

        # annotate_still encodes the render twice (q95, then q95 again to stamp
        # the marker); the in-memory path once. Re-saving the new stack once
        # more must land exactly on the old one: same render, one encode fewer.
        legacy_mode, legacy_px = _pixels(legacy)
        again_mode, again_px = _one_more_generation(new)
        assert again_mode == legacy_mode
        assert np.array_equal(again_px, legacy_px), "the render itself must not change"
        assert overlay.still_is_overlaid(new) is True

        # And the dropped generation is loss the published stack no longer has.
        render_px = np.asarray(rendered.convert(legacy_mode), dtype=np.int16)
        new_err = np.abs(_pixels(new)[1].astype(np.int16) - render_px).mean()
        legacy_err = np.abs(legacy_px.astype(np.int16) - render_px).mean()
        assert new_err <= legacy_err

    def test_a_rotated_view_renders_like_its_copy(self, tmp_path):
        # apply_calibration_np rotates with np.rot90, which returns a strided view.
        image = _sky(720, 1280)[::-1, ::-1]
        assert not image.flags["C_CONTIGUOUS"]
        legacy = _legacy_stack(tmp_path / "legacy" / STACK_NAME, image, _overlay_cfg())
        new = tmp_path / "new" / STACK_NAME
        overlay.save_overlaid_webp(
            overlay.render_still(Image.fromarray(image, "RGB"), _overlay_cfg(), CAM, POINTING, EPOCH),
            new,
        )
        assert np.array_equal(_one_more_generation(new)[1], _pixels(legacy)[1])

    def test_a_failed_render_raises(self, monkeypatch):
        monkeypatch.setattr(overlay.subprocess, "run",
                            lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout=b""))
        with pytest.raises(RuntimeError, match="ffmpeg exited 1"):
            overlay.render_still(Image.fromarray(_sky(), "RGB"), _overlay_cfg(), CAM, POINTING, EPOCH)


def test_nothing_to_draw_renders_nothing(monkeypatch):
    def _must_not_run(*args, **kwargs):
        raise AssertionError("no font means no overlay, and no ffmpeg process")

    monkeypatch.setattr(overlay.subprocess, "run", _must_not_run)
    still = Image.fromarray(_sky(), "RGB")
    assert overlay.render_still(still, _overlay_cfg(font=""), CAM, POINTING, EPOCH) is None


def test_an_unmarkable_render_is_still_published(tmp_path, monkeypatch):
    # annotate_still published the unmarked q95 encode when the EXIF stamp
    # failed; the in-memory path must do the same rather than lose the stack.
    rendered = Image.fromarray(_sky(), "RGB")

    def _no_exif():
        raise RuntimeError("no EXIF support")

    monkeypatch.setattr(overlay, "_overlay_exif", _no_exif)
    dst = tmp_path / STACK_NAME
    overlay.save_overlaid_webp(rendered, dst)
    monkeypatch.undo()
    assert dst.stat().st_size > 0
    assert overlay.still_is_overlaid(dst) is False


def test_the_marked_stack_is_encoded_once(tmp_path, monkeypatch):
    saves: list[str] = []
    real_save = Image.Image.save

    def _counting_save(self, fp, *args, **kwargs):
        saves.append(str(args[0] if args else kwargs.get("format")))
        return real_save(self, fp, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "save", _counting_save)
    dst = tmp_path / STACK_NAME
    overlay.save_overlaid_webp(Image.fromarray(_sky(), "RGB"), dst)
    monkeypatch.undo()
    assert len(saves) == 1
    assert overlay.still_is_overlaid(dst) is True


# ---------------------------------------------------------------------------
# _finish_stack: one write per stack, the right fallbacks, a PIL thumbnail.
# ---------------------------------------------------------------------------


class TestFinishStackWritesOnce:
    @needs_ffmpeg
    @pytest.mark.usefixtures("one_cpu")
    def test_the_annotated_stack_skips_the_lossless_write(self, tmp_path, monkeypatch):
        cfg, mkv = _capture_env(tmp_path, overlay=_overlay_cfg())
        image = _sky(720, 1280)
        legacy = _legacy_stack(tmp_path / "legacy" / STACK_NAME, image, cfg["overlay"],
                               epoch=CHUNK_EPOCH)

        def _no_lossless(*args, **kwargs):
            raise AssertionError("an annotated stack must not be written lossless first")

        monkeypatch.setattr(stacker, "_save_webp_lossless", _no_lossless)
        assert _finish(cfg, mkv, image) is True

        stacks = mkv.parent / "stacks"
        stack = stacks / STACK_NAME
        # One q95 encode where annotate_still made two (see TestInMemoryOverlay...).
        assert np.array_equal(_one_more_generation(stack)[1], _pixels(legacy)[1])
        assert overlay.still_is_overlaid(stack) is True
        assert (stacks / STACK_NAME.replace(".webp", "_avg.webp")).stat().st_size > 0
        with Image.open(stacks / "thumbs" / STACK_NAME) as thumb:
            with Image.open(stack) as full:
                assert thumb.size == (480, (480 * full.height + full.width) // (2 * full.width) * 2)
                assert thumb.mode == full.mode
        assert flags_manager.load(CAM, NIGHT, cfg)["chunks"][CHUNK]["stacked"] is True
        assert [p for p in stacks.rglob("*") if ".tmp" in p.name] == []

    def test_a_failed_publish_falls_back_to_the_plain_stack(self, tmp_path, monkeypatch, caplog):
        cfg, mkv = _capture_env(tmp_path, overlay=_overlay_cfg())
        image = _sky(64, 96)
        monkeypatch.setattr(overlay, "render_still",
                            lambda still, *a, **k: still.convert("RGBA"))

        def _disk_full(rendered, dst):
            raise OSError("No space left on device")

        monkeypatch.setattr(overlay, "save_overlaid_webp", _disk_full)
        with caplog.at_level(logging.WARNING):
            assert _finish(cfg, mkv, image) is True

        stack = mkv.parent / "stacks" / STACK_NAME
        assert str(stack) in caplog.text
        mode, px = _pixels(stack)
        assert mode == "RGB" and np.array_equal(px, image), "the fallback is the lossless maxpixel"

    @pytest.mark.parametrize("ocfg", [None, _overlay_cfg(enabled=False), _overlay_cfg(font="")],
                             ids=["no_overlay_block", "overlay_disabled", "no_font"])
    def test_an_unannotated_stack_is_the_lossless_maxpixel(self, tmp_path, caplog, ocfg):
        cfg, mkv = _capture_env(tmp_path, **({"overlay": ocfg} if ocfg else {}))
        image = _sky(64, 96)
        with caplog.at_level(logging.WARNING):
            assert _finish(cfg, mkv, image) is True
        mode, px = _pixels(mkv.parent / "stacks" / STACK_NAME)
        assert mode == "RGB" and np.array_equal(px, image)
        assert "Overlay failed" not in caplog.text, "nothing to draw is not a failure"
        with Image.open(mkv.parent / "stacks" / "thumbs" / STACK_NAME) as thumb:
            assert thumb.size == (480, 320)


class TestThumbnail:
    @pytest.mark.parametrize("size, expected", [
        ((1280, 848), (480, 318)),
        ((1280, 720), (480, 270)),
        ((1280, 864), (480, 324)),
        ((1000, 333), (480, 160)),
        ((16, 16), (480, 480)),
    ])
    def test_the_size_follows_ffmpegs_scale_rule(self, tmp_path, size, expected):
        # An annotated stack is RGBA with partly transparent text pixels (ffmpeg's
        # drawtext blends alpha too); the thumbnail keeps that, as ffmpeg's did.
        still = Image.new("RGBA", size, (10, 20, 30, 255))
        still.paste((200, 200, 200, 128), (0, 0, max(1, size[0] // 2), max(1, size[1] // 2)))
        dst = tmp_path / "thumbs" / STACK_NAME
        stacker._save_thumbnail(still, dst)
        with Image.open(dst) as thumb:
            assert thumb.size == expected
            assert thumb.mode == "RGBA"

    @needs_ffmpeg
    @pytest.mark.parametrize("size", [(1280, 848), (1280, 720), (1000, 333), (641, 479)])
    def test_the_size_rule_agrees_with_ffmpeg(self, tmp_path, size):
        src = tmp_path / "src.png"
        Image.new("RGB", size).save(src)
        out = tmp_path / "out.png"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
                        "-vf", "scale=480:-2", str(out)], check=True)
        dst = tmp_path / STACK_NAME
        stacker._save_thumbnail(Image.new("RGB", size), dst)
        with Image.open(out) as ref, Image.open(dst) as thumb:
            assert thumb.size == ref.size

    def test_a_failure_is_logged_and_publishes_nothing(self, tmp_path, caplog):
        dst = tmp_path / "thumbs" / STACK_NAME
        with caplog.at_level(logging.WARNING):
            stacker._save_thumbnail(Image.new("RGB", (0, 0)), dst)
        assert f"Thumbnail failed for {STACK_NAME}" in caplog.text
        assert not dst.exists()


# ---------------------------------------------------------------------------
# _compute_maxpixel: integer accumulators and uint8 pedestal maths are
# bit-identical to the float32/int16 loop they replace.
# ---------------------------------------------------------------------------

H, W = 24, 40  # small frames keep the pipe tests fast; the maths is size-free


def _legacy_pedestal(frame: np.ndarray, pct: float) -> np.ndarray:
    return np.percentile(frame[::4, ::4], pct, axis=(0, 1)).astype(np.int16)


def _legacy_subtract_background(frame: np.ndarray, pct: float,
                                bg: np.ndarray | None = None) -> np.ndarray:
    if bg is None:
        bg = _legacy_pedestal(frame, pct)
    return np.clip(frame.astype(np.int16) - bg, 0, 255).astype(np.uint8)


def _legacy_accumulate(data: bytes, bg_pct=None, pedestal_per="batch",
                       h: int = H, w: int = W):
    """The float32/int16 accumulation loop, in effect.

    ``pedestal_per="frame"`` is the loop as it stood before 2026-09-26, with a
    pedestal measured on every frame; ``"batch"`` measures one per 10-frame
    batch on its middle frame, as the stacker does since.
    """
    frame_bytes = w * h * 3
    maxpx = np.zeros((h, w, 3), dtype=np.uint8)
    avgpx_sum = np.zeros((h, w, 3), dtype=np.float32)
    refpx_sum = np.zeros((h, w, 3), dtype=np.float32) if bg_pct is not None else None
    n = 0
    stream = io.BytesIO(data)
    while True:
        raw = stream.read(frame_bytes * 10)
        if not raw:
            break
        frames = len(raw) // frame_bytes
        if frames == 0:
            break
        arr = np.frombuffer(raw[:frames * frame_bytes], dtype=np.uint8).reshape(frames, h, w, 3)
        if bg_pct is not None:
            refpx_sum += arr.sum(axis=0, dtype=np.float32)
            batch_bg = (_legacy_pedestal(arr[frames // 2], bg_pct)
                        if pedestal_per == "batch" else None)
            for i in range(frames):
                adj = _legacy_subtract_background(arr[i], bg_pct, batch_bg)
                np.maximum(maxpx, adj, out=maxpx)
                avgpx_sum += adj.astype(np.float32)
        else:
            np.maximum(maxpx, arr.max(axis=0), out=maxpx)
            avgpx_sum += arr.sum(axis=0, dtype=np.float32)
        n += frames
    if n == 0:
        return None, None, None, 0
    avgpx = (avgpx_sum / n).clip(0, 255).astype(np.uint8)
    refpx = None if refpx_sum is None else (refpx_sum / n).clip(0, 255).astype(np.uint8)
    return maxpx, avgpx, refpx, n


class _FakeFfmpeg:
    """Stands in for the decode process: its stdout is a canned rgb24 stream."""

    def __init__(self, data: bytes):
        self.stdout = io.BufferedReader(io.BytesIO(data))

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


@pytest.fixture
def small_frames(monkeypatch):
    monkeypatch.setattr(stacker, "HEIGHT", H)
    monkeypatch.setattr(stacker, "WIDTH", W)
    monkeypatch.setattr(stacker, "passthrough_args", lambda: ["-fps_mode", "passthrough"])

    def feed(data: bytes):
        monkeypatch.setattr(stacker.subprocess, "Popen", lambda cmd, **kw: _FakeFfmpeg(data))

    return feed


def _stream(n_frames: int, seed: int = 0, tail: int = 0) -> bytes:
    rng = np.random.default_rng(seed)
    frames = rng.integers(0, 256, size=(n_frames, H, W, 3), dtype=np.uint8)
    # Dark and saturated stretches so every clamp in the pipeline is exercised.
    frames[: n_frames // 3] //= 8
    frames[-(n_frames // 4 or 1):, : H // 2] = 255
    return frames.tobytes() + b"\x07" * tail


class TestAccumulationIsBitIdentical:
    # A tail shorter than one frame (2880 bytes here) is a truncated last frame,
    # dropped mid-batch (37) or arriving alone after a full batch (30).
    @pytest.mark.parametrize("n_frames, tail",
                             [(23, 0), (10, 0), (1, 0), (37, 2000), (30, 2000), (0, 100)])
    @pytest.mark.parametrize("mode", ["plain", "pedestal"])
    def test_matches_the_float32_loop(self, small_frames, n_frames, tail, mode):
        data = _stream(n_frames, seed=n_frames, tail=tail)
        bg_pct = 20.0 if mode == "pedestal" else None
        small_frames(data)

        got = stacker._compute_maxpixel(Path("chunk.mkv"), bg_subtract_pct=bg_pct)
        want = _legacy_accumulate(data, bg_pct=bg_pct)

        assert got[3] == want[3]
        for g, w in zip(got[:3], want[:3]):
            if w is None:
                assert g is None
            else:
                assert g.dtype == np.uint8 and np.array_equal(g, w)

    def test_a_long_chunk_does_not_overflow(self, small_frames):
        # 600 saturated frames: 153,000 per pixel is past uint16, well inside uint32.
        data = np.full((600, H, W, 3), 255, dtype=np.uint8).tobytes()
        small_frames(data)
        maxpx, avgpx, _, n = stacker._compute_maxpixel(Path("chunk.mkv"))
        assert n == 600
        assert (avgpx == 255).all() and (maxpx == 255).all()


@pytest.mark.parametrize("pct", [0.0, 5.0, 20.0, 50.0, 100.0])
@pytest.mark.parametrize("seed", range(6))
def test_pedestal_subtraction_matches_int16_maths(pct, seed):
    rng = np.random.default_rng(seed)
    lo = int(rng.integers(0, 200))
    frame = rng.integers(lo, 256, size=(72, 128, 3), dtype=np.uint8)
    got = stacker._subtract_pedestal(frame[None].copy(), stacker._pedestal(frame, pct))[0]
    assert np.array_equal(got, _legacy_subtract_background(frame, pct))


def test_one_pedestal_is_subtracted_from_every_frame_of_a_batch():
    rng = np.random.default_rng(3)
    frames = rng.integers(0, 256, size=(7, 16, 24, 3), dtype=np.uint8)
    bg = np.array([10, 60, 200], dtype=np.uint8)
    want = np.clip(frames.astype(np.int16) - bg.astype(np.int16), 0, 255).astype(np.uint8)
    # A non-contiguous batch takes the broadcasting path and must agree.
    for batch in (frames.copy(), frames.copy()[:, ::-1]):
        expected = want if batch.flags["C_CONTIGUOUS"] else want[:, ::-1]
        assert np.array_equal(stacker._subtract_pedestal(batch, bg), expected)


def test_a_batch_pedestal_stays_within_a_level_of_per_frame(monkeypatch):
    # A light-polluted sky drifting slowly under noise: the pedestal of the
    # batch's middle frame stands in for each frame's own.
    monkeypatch.setattr(stacker, "HEIGHT", 72)
    monkeypatch.setattr(stacker, "WIDTH", 128)
    monkeypatch.setattr(stacker, "passthrough_args", lambda: ["-fps_mode", "passthrough"])
    rng = np.random.default_rng(5)
    base = np.linspace(60, 64, 40)[:, None, None, None] + rng.normal(0, 4, (40, 72, 128, 3))
    data = np.clip(base, 0, 255).astype(np.uint8)
    monkeypatch.setattr(stacker.subprocess, "Popen",
                        lambda cmd, **kw: _FakeFfmpeg(data.tobytes()))
    maxpx, avgpx, _, n = stacker._compute_maxpixel(Path("chunk.mkv"), bg_subtract_pct=20.0)
    old_max, old_avg, _, _ = _legacy_accumulate(data.tobytes(), bg_pct=20.0,
                                                pedestal_per="frame", h=72, w=128)
    assert n == 40
    assert np.abs(avgpx.astype(int) - old_avg.astype(int)).max() <= 1
    assert np.abs(maxpx.astype(int) - old_max.astype(int)).max() <= 2


# ---------------------------------------------------------------------------
# The chunk height comes from the Matroska header when it can, ffprobe otherwise.
# ---------------------------------------------------------------------------


def _ebml(eid: int, payload: bytes, unknown_size: bool = False) -> bytes:
    size = (1 << 56) - 1 if unknown_size else len(payload)
    return (eid.to_bytes((eid.bit_length() + 7) // 8, "big")
            + ((1 << 56) | size).to_bytes(8, "big") + payload)


def _uint(eid: int, value: int) -> bytes:
    return _ebml(eid, value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big"))


def _track(track_type: int, width: int = 0, height: int = 0, crop_bottom: int = 0) -> bytes:
    video = b""
    if width:
        video += _uint(0xB0, width)
    if height:
        video += _uint(0xBA, height)
    if crop_bottom:
        video += _uint(0x54AA, crop_bottom)
    return _ebml(0xAE, _uint(0xD7, 1) + _uint(0x83, track_type)
                 + (_ebml(0xE0, video) if video else b""))


def _mkv(*tracks: bytes, unknown_segment: bool = False, cluster_first: bool = False) -> bytes:
    body = _ebml(0x1549A966, _uint(0x2AD7B1, 1_000_000))            # Info
    if cluster_first:
        body += _ebml(0x1F43B675, b"\x00" * 16)
    body += _ebml(0x1654AE6B, b"".join(tracks)) + _ebml(0x1F43B675, b"\x00" * 64)
    return (_ebml(0x1A45DFA3, _ebml(0x4282, b"matroska"))
            + _ebml(0x18538067, body, unknown_size=unknown_segment))


class TestMatroskaFrameSize:
    @pytest.mark.parametrize("data, expected", [
        (_mkv(_track(1, 1280, 720)), (1280, 720)),
        (_mkv(_track(2), _track(1, 1280, 848)), (1280, 848)),
        (_mkv(_track(1, 1280, 720), unknown_segment=True), (1280, 720)),
        (_mkv(_track(1, 1280, 736, crop_bottom=16)), None),
        (_mkv(_track(1, 1280, 720), cluster_first=True), None),
        (_mkv(_track(2)), None),
        (_mkv(_track(1, 1280, 720))[:60], None),
        (b"\x00" * 1024, None),
        (b"", None),
    ], ids=["raw", "video_after_audio", "live_muxed", "cropped", "cluster_first",
            "no_video", "truncated", "zeros", "empty"])
    def test_reads_the_first_video_track(self, tmp_path, data, expected):
        mkv = tmp_path / CHUNK
        mkv.write_bytes(data)
        assert rovimen_media.mkv_frame_size(mkv) == expected

    def test_a_missing_file_reads_as_unknown(self, tmp_path):
        assert rovimen_media.mkv_frame_size(tmp_path / "gone.mkv") is None

    @needs_ffmpeg
    @pytest.mark.parametrize("height", [720, 848])
    def test_agrees_with_ffprobe_on_a_real_file(self, tmp_path, height):
        mkv = tmp_path / CHUNK
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                        "-i", f"color=c=black:s=1280x{height}:d=0.2", "-c:v", "ffv1", str(mkv)],
                       check=True)
        assert rovimen_media.mkv_frame_size(mkv) == (1280, height)
        assert stacker._probe_video_height(mkv) == height

    def test_a_raw_chunk_needs_no_ffprobe(self, tmp_path, monkeypatch):
        mkv = tmp_path / CHUNK
        mkv.write_bytes(_mkv(_track(1, 1280, 720)))

        def _no_ffprobe(*args, **kwargs):
            raise AssertionError("a 1280x720 header must not cost an ffprobe process")

        monkeypatch.setattr(stacker.subprocess, "run", _no_ffprobe)
        assert stacker._probe_video_height(mkv) == 720

    @pytest.mark.parametrize("data", [_mkv(_track(1, 1280, 848)), b"\x00" * 64])
    def test_anything_else_still_asks_ffprobe(self, tmp_path, monkeypatch, data):
        mkv = tmp_path / CHUNK
        mkv.write_bytes(data)
        calls = []

        def _ffprobe(cmd, **kwargs):
            calls.append(cmd[0])
            return subprocess.CompletedProcess(
                cmd, 0, stdout='{"streams": [{"height": 848}]}', stderr="")

        monkeypatch.setattr(stacker.subprocess, "run", _ffprobe)
        assert stacker._probe_video_height(mkv) == 848
        assert calls == ["ffprobe"]


# ---------------------------------------------------------------------------
# The CLI keeps OpenBLAS from spinning up a thread per core; importers keep theirs.
# ---------------------------------------------------------------------------


def _python(code: str) -> str:
    env = {k: v for k, v in os.environ.items() if k != "OPENBLAS_NUM_THREADS"}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env=env, cwd=SCRIPTS, check=True)
    return out.stdout.strip().splitlines()[-1]


def test_the_cli_runs_blas_single_threaded():
    code = (
        "import os, runpy, sys\n"
        f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "sys.argv = ['stacker.py', '--help']\n"
        "try:\n"
        f"    runpy.run_path({str(SCRIPTS / 'stacker.py')!r}, run_name='__main__')\n"
        "except SystemExit:\n"
        "    pass\n"
        "print(os.environ.get('OPENBLAS_NUM_THREADS'))\n"
    )
    assert _python(code) == "1"


def test_importing_the_stacker_leaves_blas_alone():
    code = (
        "import os, sys\n"
        f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "import stacker\n"
        "print(os.environ.get('OPENBLAS_NUM_THREADS'))\n"
    )
    assert _python(code) == "None"
