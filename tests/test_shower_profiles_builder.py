"""tools/build_shower_profiles.py: a shower's profile is the mean of the
years' shapes with their spread, and the summed ZHR's uncertainty comes from
the spread of the yearly totals, not from adding showers' errors in
quadrature (which would assume they vary independently)."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "tools"))

import build_shower_profiles as bsp  # noqa: E402


def _fake_months(per_year):
    """count_month stand-in: year -> {code: {bin: count}}, plus 1000 other meteors per bin."""
    def count_month(year, month, total, by_shower, session):
        if month != 1:
            return 0
        for b in range(bsp.N_BINS):
            total[b] += 1000
        for code, bins in per_year[year].items():
            for b, c in bins.items():
                by_shower[code][b] += c
                total[b] += c
        return 1
    return count_month


def test_profiles_are_year_means_and_the_total_uses_yearly_sums(monkeypatch):
    peak = 280   # bin
    # Two showers that peak in the same bin and are both strong in 2024 and
    # weak in 2025: perfectly correlated year to year.
    per_year = {
        2024: {"AAA": {peak: 300, peak - 1: 150, peak + 1: 150}, "BBB": {peak: 300, peak - 1: 150, peak + 1: 150}},
        2025: {"AAA": {peak: 100, peak - 1: 50, peak + 1: 50}, "BBB": {peak: 100, peak - 1: 50, peak + 1: 50}},
    }
    monkeypatch.setattr(bsp, "count_month", _fake_months(per_year))
    monkeypatch.setattr(bsp, "MIN_METEORS", 10)
    out = bsp.build([2024, 2025], {"AAA": 10, "BBB": 10})

    prof = out["showers"]["AAA"]["profile"]
    top = max(prof, key=lambda r: r[1])
    assert top[1] == 1.0 and top[2] > 0.3          # mean scaled to 1, a real spread
    assert abs(top[0] - bsp.bin_sol(peak)) < 1e-9

    by_sol = {r[0]: r for r in out["total"]["series"]}
    mean, sd = by_sol[bsp.bin_sol(peak)][1:]
    assert abs(mean - 20) < 0.01                    # 10 + 10 at both showers' maximum
    # Correlated showers: the yearly totals swing by the full sum, so the
    # spread is twice one shower's (10 * sd each), not sqrt(2) times.
    one = 10 * top[2]
    assert abs(sd - 2 * one) < 0.05
    assert sd > (2 ** 0.5) * one + 0.5
    assert out["total"]["showers"] == ["AAA", "BBB"]


def test_a_dominant_shower_keeps_its_year_to_year_spread(monkeypatch):
    # A shower that outnumbers the background 3:1 and doubles from one year to
    # the next. As a share of all meteors it would move from 0.75 to 0.86 (a
    # spread of about 9 % of the mean); counted per sporadic it doubles.
    peak = 520
    per_year = {
        2024: {"GGG": {peak: 3000, peak - 1: 1500, peak + 1: 1500}},
        2025: {"GGG": {peak: 6000, peak - 1: 3000, peak + 1: 3000}},
    }
    monkeypatch.setattr(bsp, "count_month", _fake_months(per_year))
    out = bsp.build([2024, 2025], {"GGG": 100})
    top = max(out["showers"]["GGG"]["profile"], key=lambda r: r[1])
    # ratios 3000/1000 and 6000/1000 smoothed alike: sd / mean = 0.471
    assert abs(top[2] - 0.471) < 0.005
    mean, sd = {r[0]: r for r in out["total"]["series"]}[bsp.bin_sol(peak)][1:]
    assert abs(mean - 100) < 0.01 and abs(sd - 47.1) < 0.5
