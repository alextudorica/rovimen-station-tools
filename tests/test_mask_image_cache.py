"""Tests for the mask-image proxy's disk cache (routes/station_ops.py).

The bug these pin: the cache had no expiry, because it was modelled on the
plot-image cache. That one is keyed by a per-night FILENAME, so its entries
are immutable by construction and may live forever. A mask is one fixed path
whose content changes whenever an operator redraws it, so the first fetch won
permanently -- every mask cached on the prod box was stamped 2026-07-30 and
had been served unchanged for 53 days, including for cameras whose station
had since stopped reporting a mask at all. The Calibration page draws that
file over a night stack as a claim about what the camera is excluding now.
"""

from __future__ import annotations

import time

import pytest

from routes import station_ops


def test_ttl_is_bounded_and_not_absurd():
    """A mask changes rarely, so the exact number is a judgement call -- but
    it must exist, and it must be short enough that a redraw shows up in the
    same sitting."""
    assert 60 <= station_ops.MASK_CACHE_TTL <= 3600


class _Cache:
    """A cache file whose mtime the test controls."""

    def __init__(self, tmp_path, age_s: float):
        self.path = tmp_path / "RO000A.bmp"
        self.path.write_bytes(b"BM-cached")
        when = time.time() - age_s
        import os
        os.utime(self.path, (when, when))

    def is_fresh(self) -> bool:
        """The predicate the view uses, exercised directly: the view itself
        needs a Flask app, a station tunnel and a thumb-cache root to call."""
        return (
            self.path.exists()
            and (time.time() - self.path.stat().st_mtime) < station_ops.MASK_CACHE_TTL
        )


def test_a_fresh_entry_is_served(tmp_path):
    assert _Cache(tmp_path, age_s=10).is_fresh() is True


def test_an_entry_past_the_ttl_is_not_served(tmp_path):
    assert _Cache(tmp_path, age_s=station_ops.MASK_CACHE_TTL + 60).is_fresh() is False


def test_the_53_day_case_is_not_served(tmp_path):
    """The exact shape found on prod on 2026-09-21."""
    assert _Cache(tmp_path, age_s=53 * 86400).is_fresh() is False


def test_a_missing_entry_is_not_served(tmp_path):
    cache = _Cache(tmp_path, age_s=10)
    cache.path.unlink()
    assert cache.is_fresh() is False


def test_the_view_evicts_on_404_rather_than_waiting_for_the_ttl():
    """A station answering 404 is positive evidence that any cached mask is
    wrong -- drawing it anyway asserts something the station just denied. Read
    off the source because the view needs a full app context to call."""
    import inspect
    src = inspect.getsource(station_ops.register_station_ops_routes)
    start = src.index("def api_mask_image_proxy")
    body = src[start:src.index("def api_mask_write_proxy")]
    assert "resp.status_code == 404" in body
    assert "unlink" in body


def test_the_plot_image_cache_is_deliberately_left_unbounded():
    """Not an oversight: its key contains the night's filename, so an entry
    can never go stale. If this ever gains a TTL it should be for a reason
    other than the mask bug."""
    import inspect
    src = inspect.getsource(station_ops.register_station_ops_routes)
    start = src.index("def api_rms_plot_image_proxy")
    body = src[start:start + 1500]
    assert "MASK_CACHE_TTL" not in body
