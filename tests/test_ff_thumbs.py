"""Tests for the shared FF maxpixel -> WebP module (rovimen-scripts/ff_thumbs.py)."""

from __future__ import annotations

import io
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits as afits
from PIL import Image

import ff_thumbs

CAM = "RO000P"
NIGHT = "20260723"


def _write_ff(path: Path, data: np.ndarray) -> Path:
    """Write a synthetic FF FITS file with a MAXPIXEL extension."""
    path.parent.mkdir(parents=True, exist_ok=True)
    primary = afits.PrimaryHDU()
    maxpixel = afits.ImageHDU(data=data, name="MAXPIXEL")
    afits.HDUList([primary, maxpixel]).writeto(path, overwrite=True)
    return path


def _gradient(w: int = 64, h: int = 48) -> np.ndarray:
    return np.tile(np.linspace(0, 255, w, dtype=np.uint8), (h, 1))


# ---------------------------------------------------------------------------
# read_ff_maxpixel
# ---------------------------------------------------------------------------

def test_read_ff_maxpixel_returns_array(tmp_path):
    data = _gradient()
    ff = _write_ff(tmp_path / f"FF_{CAM}_{NIGHT}_201148_123_0000000.fits", data)
    out = ff_thumbs.read_ff_maxpixel(ff)
    assert out is not None
    assert np.array_equal(out.astype(np.uint8), data)


def test_read_ff_maxpixel_missing_file(tmp_path):
    assert ff_thumbs.read_ff_maxpixel(tmp_path / "nope.fits") is None


# ---------------------------------------------------------------------------
# ff_maxpixel_to_webp
# ---------------------------------------------------------------------------

def test_webp_bytes_from_single_ff(tmp_path):
    ff = _write_ff(tmp_path / f"FF_{CAM}_{NIGHT}_201148_123_0000000.fits", _gradient())
    buf = ff_thumbs.ff_maxpixel_to_webp([ff])
    assert buf is not None
    head = buf.read(12)
    assert head[:4] == b"RIFF" and head[8:12] == b"WEBP"


def test_webp_combines_multiple_ffs_with_maximum(tmp_path):
    a = np.zeros((16, 16), dtype=np.uint8)
    a[4, 4] = 200
    b = np.zeros((16, 16), dtype=np.uint8)
    b[10, 10] = 150
    ff_a = _write_ff(tmp_path / f"FF_{CAM}_{NIGHT}_201148_123_0000000.fits", a)
    ff_b = _write_ff(tmp_path / f"FF_{CAM}_{NIGHT}_201208_456_0000256.fits", b)
    buf = ff_thumbs.ff_maxpixel_to_webp([ff_a, ff_b])
    assert buf is not None
    img = np.asarray(Image.open(buf).convert("L"))
    # Lossless: both bright pixels must survive the combine.
    assert img[4, 4] == 200
    assert img[10, 10] == 150


def test_webp_max_width_downscales(tmp_path):
    ff = _write_ff(
        tmp_path / f"FF_{CAM}_{NIGHT}_201148_123_0000000.fits",
        _gradient(w=1920, h=1080),
    )
    buf = ff_thumbs.ff_maxpixel_to_webp([ff], max_width=480, lossless=False)
    assert buf is not None
    img = Image.open(buf)
    assert img.width == 480
    assert img.height == 270


def test_webp_max_width_never_upscales(tmp_path):
    ff = _write_ff(tmp_path / f"FF_{CAM}_{NIGHT}_201148_123_0000000.fits", _gradient(w=64, h=48))
    buf = ff_thumbs.ff_maxpixel_to_webp([ff], max_width=480)
    img = Image.open(buf)
    assert img.width == 64


def test_webp_returns_none_for_no_readable_ffs(tmp_path):
    assert ff_thumbs.ff_maxpixel_to_webp([]) is None
    assert ff_thumbs.ff_maxpixel_to_webp([tmp_path / "missing.fits"]) is None


def test_lossy_smaller_than_lossless(tmp_path):
    rng = np.random.default_rng(42)
    noisy = rng.integers(0, 255, size=(480, 640), dtype=np.uint8)
    ff = _write_ff(tmp_path / f"FF_{CAM}_{NIGHT}_201148_123_0000000.fits", noisy)
    lossless = ff_thumbs.ff_maxpixel_to_webp([ff])
    lossy = ff_thumbs.ff_maxpixel_to_webp([ff], lossless=False, quality=80)
    assert lossy.getbuffer().nbytes < lossless.getbuffer().nbytes


# ---------------------------------------------------------------------------
# find_ff_maxpixels
# ---------------------------------------------------------------------------

@pytest.fixture()
def rms_dir(tmp_path):
    d = tmp_path / "RMS_data"
    session = d / "CapturedFiles" / f"{CAM}_{NIGHT}_195500_000000"
    session.mkdir(parents=True)
    for hhmmss in ("201138", "201148", "201158", "213000"):
        _write_ff(
            session / f"FF_{CAM}_{NIGHT}_{hhmmss}_123_0000000.fits",
            np.zeros((4, 4), dtype=np.uint8),
        )
    return d


def test_find_ff_overlapping_chunk_window(rms_dir):
    # Chunk starts 20:11:48 UTC, 20 s long -> FF blocks from -10.24 s (one
    # 256-frame block at 25 fps) to +25 s included.
    found = ff_thumbs.find_ff_maxpixels(CAM, rms_dir, datetime(2026, 7, 23, 20, 11, 48))
    names = [f.name for f in found]
    assert f"FF_{CAM}_{NIGHT}_201138_123_0000000.fits" in names
    assert f"FF_{CAM}_{NIGHT}_201148_123_0000000.fits" in names
    assert f"FF_{CAM}_{NIGHT}_201158_123_0000000.fits" in names
    assert f"FF_{CAM}_{NIGHT}_213000_123_0000000.fits" not in names


def test_find_ff_block_that_began_before_the_chunk(tmp_path):
    """An FF block lasts 256 frames (10.24 s at 25 fps), so the block that
    began at 19:05:04 holds a meteor at 19:05:13, inside chunk 19:05:12."""
    d = tmp_path / "RMS_data"
    session = d / "CapturedFiles" / f"{CAM}_{NIGHT}_160000_000000"
    for hhmmss in ("190450", "190504"):
        _write_ff(session / f"FF_{CAM}_{NIGHT}_{hhmmss}_000_0000000.fits",
                  np.zeros((4, 4), dtype=np.uint8))
    found = ff_thumbs.find_ff_maxpixels(CAM, d, datetime(2026, 7, 23, 19, 5, 12))
    assert [f.name for f in found] == [f"FF_{CAM}_{NIGHT}_190504_000_0000000.fits"]


def test_find_ff_block_length_follows_the_frame_rate(tmp_path):
    d = tmp_path / "RMS_data"
    session = d / "CapturedFiles" / f"{CAM}_{NIGHT}_160000_000000"
    _write_ff(session / f"FF_{CAM}_{NIGHT}_190458_000_0000000.fits",
              np.zeros((4, 4), dtype=np.uint8))
    chunk = datetime(2026, 7, 23, 19, 5, 12)
    # 14 s before the chunk: past a 25 fps block (10.24 s), inside a 15 fps one.
    assert ff_thumbs.find_ff_maxpixels(CAM, d, chunk) == []
    assert len(ff_thumbs.find_ff_maxpixels(CAM, d, chunk, fps=15.0)) == 1


def test_find_ff_sorted_by_offset(rms_dir):
    found = ff_thumbs.find_ff_maxpixels(CAM, rms_dir, datetime(2026, 7, 23, 20, 11, 48))
    offsets = [int(f.name.split("_")[3]) for f in found]
    assert offsets == sorted(offsets)


def test_find_ff_no_match_for_other_cam(rms_dir):
    assert ff_thumbs.find_ff_maxpixels("XX0001", rms_dir, datetime(2026, 7, 23, 20, 11, 48)) == []


def test_find_ff_midnight_wraparound(tmp_path):
    d = tmp_path / "RMS_data"
    session = d / "CapturedFiles" / f"{CAM}_{NIGHT}_195500_000000"
    session.mkdir(parents=True)
    _write_ff(
        session / f"FF_{CAM}_20260724_000005_123_0000000.fits",
        np.zeros((4, 4), dtype=np.uint8),
    )
    # Chunk starting 23:59:55 UTC on the previous date overlaps the 00:00:05 FF.
    found = ff_thumbs.find_ff_maxpixels(CAM, d, datetime(2026, 7, 23, 23, 59, 55))
    assert len(found) == 1


def test_venv_probe_accepts_all_installer_locations(tmp_path, monkeypatch):
    """The venv probe must accept every location the installer accepts
    (~/RMS/venv, ~/vRMS, ~/rms_venv) — probing only ~/RMS/venv left ~/vRMS
    stations (gmnro05/gmnro18/gmnmd01) without numpy, failing every render."""
    import sys

    pyver = f"python{sys.version_info.major}.{sys.version_info.minor}"
    for venv_root in ("RMS/venv", "vRMS", "rms_venv"):
        base = tmp_path / venv_root.replace("/", "_")
        sp = base / venv_root / "lib" / pyver / "site-packages"
        sp.mkdir(parents=True)
        before = list(sys.path)
        try:
            ff_thumbs.ensure_rms_venv_on_path(base=base)
            assert str(sp) in sys.path, f"{venv_root} not added to sys.path"
        finally:
            sys.path[:] = before


def test_venv_probe_skips_site_packages_of_another_python(tmp_path):
    """gmnro08 (Brașov) ran the indexer on system Python 3.8 with a 3.12 vRMS:
    putting the 3.12 site-packages first shadowed the system numpy with one
    that cannot load, so no FF thumbnail was ever rendered there."""
    import sys

    other = f"python{sys.version_info.major}.{sys.version_info.minor + 1}"
    sp = tmp_path / "vRMS" / "lib" / other / "site-packages"
    sp.mkdir(parents=True)
    before = list(sys.path)
    try:
        ff_thumbs.ensure_rms_venv_on_path(base=tmp_path)
        assert str(sp) not in sys.path
    finally:
        sys.path[:] = before


def test_venv_probe_adds_rms_source_dirs(tmp_path):
    import sys

    base = tmp_path / "home"
    src = base / "source" / "RMS"
    src.mkdir(parents=True)
    before = list(sys.path)
    try:
        ff_thumbs.ensure_rms_venv_on_path(base=base)
        assert str(src) in sys.path
    finally:
        sys.path[:] = before
