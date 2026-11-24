"""Contract between the Calibration page's plot list and the server's
allow-list.

``/api/calibration/plot/<kind>/<camera>`` exists so a plot can be added to
the page by naming it in two places instead of growing another Flask route
each time. Two places is one more than one, so this pins them together: a
kind the page asks for and the server does not know 404s, and the card
renders a permanently empty tile with no error anywhere.

It also pins each suffix against archive_upload's upload globs -- a plot the
station never ships is one the resolver can never find, however well the
page asks for it.
"""

from __future__ import annotations

import re
from pathlib import Path

from routes.archive import CALIBRATION_PLOT_SUFFIXES, RMS_PLOT_LABELS

REPO = Path(__file__).parent.parent
CALIBRATION_JS = REPO / 'dashboard' / 'static' / 'dashboard-calibration.js'


def _kinds_requested_by_the_page() -> list[str]:
    src = CALIBRATION_JS.read_text()
    block = re.search(r'const PLOTS = \[(.*?)\n\];', src, re.DOTALL)
    assert block, 'could not find the PLOTS list in dashboard-calibration.js'
    return re.findall(r"kind:\s*'([a-z_]+)'", block.group(1))


def test_the_page_asks_only_for_kinds_the_server_serves():
    unknown = [k for k in _kinds_requested_by_the_page() if k not in CALIBRATION_PLOT_SUFFIXES]
    assert not unknown, f'dashboard-calibration.js requests unserved plot kinds: {unknown}'


def test_every_served_kind_is_actually_on_the_page():
    """Not a style rule: an entry here that nothing renders is dead surface
    area on an authenticated endpoint."""
    requested = set(_kinds_requested_by_the_page())
    unused = [k for k in CALIBRATION_PLOT_SUFFIXES if k not in requested]
    assert not unused, f'CALIBRATION_PLOT_SUFFIXES has kinds the page never requests: {unused}'


def test_every_suffix_is_one_the_archive_actually_carries():
    unknown = [s for s in CALIBRATION_PLOT_SUFFIXES.values() if s not in RMS_PLOT_LABELS]
    assert not unknown, f'suffixes the archive does not classify as RMS plots: {unknown}'


def test_the_astrometry_report_is_included():
    """The plot an operator judges a platepar by -- star matches drawn on
    sky. Named explicitly so removing it is a deliberate act."""
    assert CALIBRATION_PLOT_SUFFIXES['astrometry_report'] == '_calib_report_astrometry.jpg'
