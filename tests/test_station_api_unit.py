"""The station API's own encodes yield to RMS capture.

The overlay gate, a lock and process_clip run ffmpeg as children of
rovimen-station-api; with no Nice or IO class in the unit they ran at full
priority beside RMS. The updater copies a changed unit over the installed one
(``reconcile_units``, exact sudoers cp rule per bundled unit) and restarts the
service, so the keys reach every station with the bundle.
"""

from __future__ import annotations

import configparser
from pathlib import Path

UNIT = Path(__file__).resolve().parents[1] / "rovimen-scripts" / "rovimen-station-api.service"


def _service() -> configparser.SectionProxy:
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    cp.read_string(UNIT.read_text())
    return cp["Service"]


def test_station_api_runs_below_capture():
    svc = _service()
    assert svc["Nice"] == "10"
    assert svc["IOSchedulingClass"] == "idle"

