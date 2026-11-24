"""The IAU shower table: this year's dates from the Sun's real position, the
radiant and solar longitudes passed through for the Showers page, Wikipedia
links, and no NaN (which would make /api/showers unreadable in a browser)."""

from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "dashboard"))

import mdc_poller  # noqa: E402


def _record(code: str, name: str, los_b: str, los_e: str, los: str, ra: str, dec: str,
            dra: str, ddec: str, vg: str, origin: str = "", flags: str = "M", no: str = "00007") -> str:
    """One MDC line: 32+ quoted, pipe-separated columns (only the used ones set)."""
    cols = [""] * 33
    cols[0], cols[1], cols[3], cols[4], cols[5], cols[6] = "00066", no, code, " 1", "2020-01-01", name
    cols[7], cols[8], cols[9], cols[10] = " annual ", los_b, los_e, los
    cols[11], cols[12], cols[13], cols[14], cols[15] = ra, dec, dra, ddec, vg
    cols[21], cols[31] = flags, origin
    return '"' + '"|"'.join(cols) + '"'


def test_dates_follow_the_sun_not_a_linear_rule():
    # The old rule put the Perseids (140.0) on 9 August and the Orionids
    # (208) on 19 October; the Sun reaches them on 12-13 and 21-22.
    assert mdc_poller._sol_lon_to_date(140.0, 2026) in ("Aug 12", "Aug 13")
    assert mdc_poller._sol_lon_to_date(208.0, 2026) in ("Oct 21", "Oct 22")
    assert mdc_poller._sol_lon_to_date(283.15, 2026) in ("Jan 3", "Jan 4")
    eq = mdc_poller._sol_lon_to_datetime(360 - 0.3632, 2026)   # March equinox, J2000
    assert abs(eq - datetime.datetime(2026, 3, 20, 14, 46, tzinfo=datetime.timezone.utc)) < datetime.timedelta(hours=1)


def test_parse_passes_through_radiant_and_solar_longitudes_and_drops_nan(monkeypatch):
    monkeypatch.setattr(mdc_poller, "_WIKIPEDIA", {"PER": "Perseids"})
    raw = "\n".join([
        ":header",
        _record("PER", "Perseids", "115.0", "175.0", "140.1", "47.9", "57.9", "1.45", "0.2", "59.0",
                origin="109P/Swift-Tuttle"),
        # MDC writes "nan" where a value is unknown; it must not reach JSON
        _record("COR", "Corvids", "93.0", "100.0", "94.9", "191.6", "-19.2", "nan", "nan", "14.1", no="00063"),
    ])
    d = mdc_poller._parse(raw)
    per = d["PER"]
    assert per["sol_peak"] == 140.1 and per["ra"] == 47.9 and per["dec"] == 57.9 and per["dra"] == 1.45
    assert per["iau_no"] == 7 and per["parent"] == "109P/Swift-Tuttle"
    assert per["peak"] in ("Aug 12", "Aug 13") and per["wikipedia"] == "Perseids"
    assert "dra" not in d["COR"] and "ddec" not in d["COR"]
    json.dumps(d, allow_nan=False)   # raises on NaN / Infinity


def test_wikipedia_list_is_bundled_and_well_formed():
    data = json.loads((PROJECT / "dashboard" / "shower_data" / "wikipedia.json").read_text())
    assert data["PER"] == "Perseids" and data["GEM"] == "Geminids"
    # the comet page Wikipedia redirects "Camelopardalids" to is not a shower article
    assert "CAM" not in data
    assert all(isinstance(t, str) and t and "/" not in t for t in data.values())


def test_shower_profiles_file_is_bundled_and_well_formed():
    data = json.loads((PROJECT / "dashboard" / "shower_data" / "profiles.json").read_text())
    assert data["years"] and data["meteors"] > 100000
    per = data["showers"]["PER"]
    assert 139 < per["peak_sol"] < 141 and per["n"] > 10000
    # [solar longitude, mean of the years (1 at its maximum), standard deviation]
    assert max(p[1] for p in per["profile"]) == 1.0
    assert all(0 <= p[0] < 360 and p[2] >= 0 for p in per["profile"])
    total = data["total"]
    assert len(total["series"]) == 720 and "GEM" in total["showers"]
    gem_peak = max(total["series"], key=lambda r: r[1])
    assert 255 < gem_peak[0] < 265 and gem_peak[1] > 100   # the year's maximum is the Geminids
