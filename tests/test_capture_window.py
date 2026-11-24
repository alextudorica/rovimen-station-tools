"""Footage window per station and across the fleet (sun -9° → -9° + 30 min).

The dashboard's time pickers are limited to it: per station on the Video DB,
the fleet's earliest start to latest end on Detections / Multi-Station / Grid.
Observed on gmnro20 (Brăila), night of 2026-10-20: first colour chunk 16:04,
last 04:13 UTC.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))

from astronomy import (COLOUR_TAIL_MIN, _compute_twilight, capture_window,  # noqa: E402
                       network_capture_window)

BRAILA = (44.941595, 27.60250)
BUCHAREST = (44.65, 25.67)
BERLIN = (52.52, 13.40)


def test_window_matches_the_footage_observed_at_braila():
    start, _ = capture_window(*BRAILA, "20261020")      # evening of 10-20
    _, end = capture_window(*BRAILA, "20261019")        # morning of 10-20
    assert abs(start - (16 * 60 + 4)) <= 3               # first chunk 16:04
    assert abs(end - (4 * 60 + 13)) <= 3                 # last chunk 04:13


def test_window_is_sun_minus_9_plus_the_colour_tail():
    start, end = capture_window(*BUCHAREST, "20261021")
    nine = _compute_twilight(*BUCHAREST, "20261021", horizon=-9.0)
    assert start == nine["sunset_min"]
    assert end == nine["sunrise_min"] + COLOUR_TAIL_MIN


def test_network_window_takes_earliest_start_and_latest_end():
    windows = {"east": capture_window(*BUCHAREST, "20261021"),
               "west": capture_window(*BERLIN, "20261021")}
    net = network_capture_window(windows)
    assert net["earliest_station"] == "east" and net["latest_station"] == "west"
    assert net["capture_start_min"] == windows["east"][0]
    assert net["capture_end_min"] == windows["west"][1]
    assert net["station_count"] == 2


def test_network_window_orders_around_noon_not_midnight():
    # an end just before noon is later in the night than one just after midnight
    net = network_capture_window({"a": (1000, 30), "b": (1050, 700)})
    assert net["capture_start_min"] == 1000 and net["capture_end_min"] == 700
    # and an evening start is earlier than one after midnight
    assert network_capture_window({"a": (20, 300), "b": (1300, 300)})["earliest_station"] == "b"


def test_network_window_empty():
    assert network_capture_window({}) is None
