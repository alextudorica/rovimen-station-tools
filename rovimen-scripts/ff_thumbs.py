#!/usr/bin/env python3
"""Shared FF (RMS FITS) maxpixel reading and BW WebP rendering.

Used by:
  * station_api.py   -- live thumbnail / capture serving (full-res, lossless)
  * detection_indexer.py -- tiny per-detection thumbnails pushed to the VPS
    during the night so every detection always has an image on the dashboard.

FF files are named ``FF_<cam>_<YYYYMMDD>_<HHMMSS>_<ms>_<frame>.fits`` and
their timestamps are always UTC.
"""

from __future__ import annotations

import io
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

import rovimen_night

logger = logging.getLogger(__name__)

# Frames RMS compresses into one FF file; the block lasts 256 / fps seconds.
FF_BLOCK_FRAMES = 256


_venv_probe_ok: bool | None = None


def ensure_rms_venv_on_path(base: Path | None = None) -> bool:
    """Add RMS venv site-packages and RMS source to sys.path.

    Probes the same venv locations the installer accepts (~/RMS/venv, ~/vRMS,
    ~/rms_venv) — stations differ (e.g. gmnro05/gmnro18/gmnmd01 use ~/vRMS),
    and probing only ~/RMS/venv left those stations without numpy, failing
    every FF thumbnail render. Only the site-packages built for the running
    Python version is added: on gmnro08 (Brașov) the indexer ran the system
    Python 3.8 and put vRMS's 3.12 numpy ahead of the system's own, so numpy
    never imported and no thumbnail was ever rendered. RMS source is looked up
    at ~/RMS and ~/source/RMS. Returns True if numpy is importable afterwards;
    the result for the default base is cached for the process lifetime.
    """
    global _venv_probe_ok
    if base is None and _venv_probe_ok is not None:
        return _venv_probe_ok
    home = base if base is not None else Path.home()
    pyver = f'python{sys.version_info.major}.{sys.version_info.minor}'
    for venv_root in (home / 'RMS' / 'venv', home / 'vRMS', home / 'rms_venv'):
        p = venv_root / 'lib' / pyver / 'site-packages'
        if p.is_dir() and str(p) not in sys.path:
            sys.path.insert(0, str(p))
    for rms_src in (home / 'RMS', home / 'source' / 'RMS'):
        if rms_src.is_dir() and str(rms_src) not in sys.path:
            sys.path.insert(0, str(rms_src))
    try:
        import numpy  # noqa: F401
        ok = True
    except ImportError:
        ok = False
    if base is None:
        _venv_probe_ok = ok
    return ok


def read_ff_maxpixel(ff_path: Path):
    """Read the maxpixel plane from an FF FITS file. Returns numpy array or None."""
    ensure_rms_venv_on_path()
    try:
        import numpy as np  # noqa: F401
    except ImportError:
        return None
    # Try astropy first
    try:
        from astropy.io import fits as afits
        with afits.open(str(ff_path)) as hdul:
            for ext in ('MAXPIXEL', 'MAX', 0):
                try:
                    if hdul[ext].data is not None:
                        return hdul[ext].data
                except (KeyError, IndexError):
                    continue
    except ImportError:
        pass
    except Exception:
        logger.debug('astropy read failed for %s', ff_path, exc_info=True)
    # Try RMS FFfile
    try:
        from RMS.Formats.FFfile import read as readFF
        ff = readFF(str(ff_path.parent), ff_path.name)
        if ff is not None:
            return ff.maxpixel
    except Exception:
        pass
    return None


def ff_maxpixel_to_webp(
    ff_paths,
    *,
    max_width: int | None = None,
    lossless: bool = True,
    quality: int = 80,
) -> io.BytesIO | None:
    """Combine FF maxpixels in memory and return a grayscale WebP as BytesIO.

    ``max_width`` scales the combined image down (aspect preserved) before
    encoding; ``lossless=False`` encodes lossy at ``quality``. Defaults keep
    the historical station_api behaviour: full resolution, lossless.
    No disk write.
    """
    if not ensure_rms_venv_on_path():
        return None
    import numpy as np
    combined = None
    for ff_path in ff_paths:
        data = read_ff_maxpixel(ff_path)
        if data is None:
            continue
        if data.ndim == 3:
            data = data[0] if data.shape[0] in (2, 4) else np.mean(data, axis=2)
        data = data.astype(np.uint8)
        combined = data if combined is None else np.maximum(combined, data)
    if combined is None:
        return None
    try:
        from PIL import Image
        img = Image.fromarray(combined, 'L').convert('RGB')
        if max_width is not None and img.width > max_width:
            new_h = max(1, round(img.height * max_width / img.width))
            img = img.resize((max_width, new_h), Image.LANCZOS)
        buf = io.BytesIO()
        if lossless:
            img.save(buf, format='webp', lossless=True)
        else:
            img.save(buf, format='webp', quality=quality)
        buf.seek(0)
        return buf
    except Exception:
        logger.exception('WebP encode failed for %s', [p.name for p in ff_paths])
        return None


def find_ff_maxpixels(
    cam: str,
    rms_dir: Path,
    chunk_dt: datetime,
    *,
    segment_duration: int = 20,
    fps: float = 25.0,
) -> list[Path]:
    """Find ALL FF FITS files that overlap a chunk window starting at ``chunk_dt``.

    ``chunk_dt`` is the chunk start as a naive UTC datetime. FF names carry
    UTC timestamps (the block's start), so matching is a plain epoch
    difference: blocks that began up to one block length (256 / ``fps``
    seconds, 10.24 s at 25 fps) before the chunk, and so still run into it,
    through 5 s past its end. The searched dates derive from the window
    endpoints, which also covers the midnight boundary. Returns FF paths
    sorted by time offset from the chunk start.
    """
    lead_s = FF_BLOCK_FRAMES / fps if fps > 0 else FF_BLOCK_FRAMES / 25.0
    win_start = chunk_dt - timedelta(seconds=lead_s)
    win_end = chunk_dt + timedelta(seconds=segment_duration + 5)
    search_dates = sorted({win_start.strftime('%Y%m%d'), win_end.strftime('%Y%m%d')})

    matches: list[tuple[float, Path]] = []
    for sub in ('CapturedFiles', 'ArchivedFiles'):
        parent = rms_dir / sub if (rms_dir / sub).exists() else rms_dir.parent / sub
        if not parent.exists():
            continue
        for date_dir in parent.iterdir():
            if not date_dir.is_dir() or not date_dir.name.startswith(cam):
                continue
            for search_date in search_dates:
                for ff in date_dir.glob(f'FF_{cam}_{search_date}_*.fits'):
                    ff_dt = rovimen_night.rms_frame_time(ff.name)
                    if ff_dt is None:
                        continue
                    ff_dt = ff_dt.replace(microsecond=0)  # matched to the second
                    diff = (ff_dt - chunk_dt).total_seconds()
                    if -lead_s <= diff <= segment_duration + 5:
                        matches.append((diff, ff))
    matches.sort()
    return [ff for _, ff in matches]
