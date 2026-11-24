"""Cache-Control policy for thumbnail responses served by Flask.

The nginx X-Accel path already sends 30-day immutable; these tests pin the
same policy onto the Flask-served miss tiers via _thumb_cache_control so a
thumb gets identical browser caching no matter which tier served it.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "dashboard"))

from routes.media import _THUMB_IMMUTABLE_MAX_AGE_S, _thumb_cache_control

TODAY = "20260704"


def test_past_night_is_immutable():
    cc = _thumb_cache_control("20260703", TODAY)
    assert cc == f"public, max-age={_THUMB_IMMUTABLE_MAX_AGE_S}, immutable"


def test_old_archive_night_is_immutable():
    cc = _thumb_cache_control("20250101", TODAY)
    assert "immutable" in cc


def test_tonight_gets_short_ttl():
    # Tonight's BW thumb is overwritten in place by the color pass, so it
    # must NOT be pinned as immutable in the browser.
    cc = _thumb_cache_control(TODAY, TODAY)
    assert cc == "public, max-age=300"
    assert "immutable" not in cc


def test_future_date_gets_short_ttl():
    cc = _thumb_cache_control("20990101", TODAY)
    assert "immutable" not in cc
