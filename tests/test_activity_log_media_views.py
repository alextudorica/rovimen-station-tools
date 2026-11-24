"""Product views must reach the activity log.

/thumbnail/, /video/, /timelapse/ and /stream/ were skipped as "huge volume,
rarely insightful". They are the only record of a product being watched rather
than listed, and skipping them made the timelapse play count unanswerable: the
player fetches /timelapse/<host>/<cam>/<date>/<file>, so reports could only see
explicit downloads and showed near-zero use of a tab opened 584 times in 25 days.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))
import security


WATCHED = [
    "/timelapse/gmnro20/RO001M/20260918/RO001M_20260918_timelapse.mp4",
    "/video/gmnro20/RO001M/20260918/RO001M_20260918_010203_color.mp4",
    "/thumbnail/gmnro20/RO001M/20260918/x.webp",
    "/stream/gmnro20/RO001M",
]

STILL_SKIPPED = [
    "/static/dashboard.js",
    "/favicon.ico",
    "/sw.js",
    "/api/auth/status",
    "/api/status/gmnro20",
    "/api/vitals/gmnro20",
    "/api/events/status",
    "/internal/thumb_cache/abc.webp",
]


def _skipped(path: str) -> bool:
    return any(path == p or (p.endswith("/") and path.startswith(p))
               for p in security._ACTIVITY_SKIP_PREFIXES)


@pytest.mark.parametrize("path", WATCHED)
def test_product_views_are_logged(path):
    assert not _skipped(path), f"{path} must reach the activity log"


@pytest.mark.parametrize("path", STILL_SKIPPED)
def test_assets_and_timer_polls_stay_out(path):
    assert _skipped(path), f"{path} is machinery, not a view"


def test_skip_list_holds_no_media_prefix():
    """Guard against a future 'the log is too big' change quietly reverting this."""
    for prefix in ("/timelapse/", "/video/", "/thumbnail/", "/stream/"):
        assert prefix not in security._ACTIVITY_SKIP_PREFIXES
