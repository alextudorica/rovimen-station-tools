#!/usr/bin/env python3
"""Build measured meteor-shower activity profiles from GMN trajectories.

Streams GMN's monthly trajectory summaries (one line per multi-station
meteor, with its solar longitude and IAU shower code) for the given years,
counts each shower's meteors and all meteors in 0.5-degree solar-longitude
bins, and writes a small JSON the Showers page draws as each shower's shape.

The profile is the shower's count per sporadic meteor in each bin: clouds,
the Moon, the number of cameras and the network's growth change a night's
sporadics and its shower count alike, so they cancel. The reference is the
sporadics, not all meteors: a share of all meteors saturates where the
shower dominates (the Geminids are 76 % of GMN's meteors at their maximum),
which flattens the peak and hides most of its year-to-year variation, while
the ratio to sporadics stays proportional to the shower's rate.
Each year's ratio is computed on its own: a shower's profile is the mean of
the years, with their standard deviation as its uncertainty, scaled to 1 at
the mean's maximum; the page sets the height from the IMO ZHR. The summed
ZHR of all rated showers ("total") is averaged over the yearly totals, so its
uncertainty includes how the showers vary together from year to year (the
shared sporadic reference among them). Bins are given by their J2000 solar
longitude (as GMN and the IAU Meteor Data Center use), and the page turns
them into dates for the year it shows, so a curve lands on the same days as
the shower's peak and limits.

Usage:
    uv run tools/build_shower_profiles.py --years 2022-2025 \
        --output dashboard/shower_data/profiles.json
Nothing is kept on disk but the output; each month is streamed and counted
(about 5 minutes for four years). The shapes are multi-year averages, so a
yearly rerun (adding the year just ended) is enough; the file is committed
and deploys with the dashboard (/api/shower-profiles).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://globalmeteornetwork.org/data/traj_summary_data/monthly"
BIN_DEG = 0.5
N_BINS = int(360 / BIN_DEG)
COL_IAU_CODE = 4      # 3-letter shower code, "..." when unassociated
COL_SOL_LON = 5       # solar longitude, degrees
MIN_METEORS = 300     # fewer GMN meteors than this over all years: no profile
FLOOR = 0.05          # profile bins below this share of the maximum are dropped
MIN_PEAK_SHARE = 0.005  # a shower under 0.5 % of all meteors at its maximum is noise


def _month_url(year: int, month: int) -> str:
    return f"{BASE_URL}/traj_summary_monthly_{year:04d}{month:02d}.txt"


def count_month(year: int, month: int, total: list[int], by_shower: dict[str, list[int]],
                session: requests.Session) -> int:
    """Stream one monthly file into the bin counters; returns rows counted."""
    url = _month_url(year, month)
    for attempt in range(3):
        try:
            with session.get(url, stream=True, timeout=120) as r:
                if r.status_code == 404:
                    logger.warning("missing %s", url)
                    return 0
                r.raise_for_status()
                n = 0
                for raw in r.iter_lines(decode_unicode=False):
                    if not raw or raw[:1] == b"#":
                        continue
                    cells = raw.split(b";", COL_SOL_LON + 1)
                    if len(cells) <= COL_SOL_LON:
                        continue
                    try:
                        sol = float(cells[COL_SOL_LON])
                    except ValueError:
                        continue
                    b = int((sol % 360.0) / BIN_DEG) % N_BINS
                    total[b] += 1
                    code = cells[COL_IAU_CODE].strip().decode("ascii", "ignore")
                    if code and code != "...":
                        by_shower[code][b] += 1
                    n += 1
                return n
        except (requests.RequestException, OSError) as exc:
            logger.warning("%s attempt %d failed: %s", url, attempt + 1, exc)
            time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"could not read {url}")


def bin_sol(b: int) -> float:
    """J2000 solar longitude (degrees) of a bin's centre."""
    return round((b + 0.5) * BIN_DEG, 2)


def _smooth(v: list[float]) -> list[float]:
    """A 3-bin running mean (1.5 days): takes the night-to-night noise out of
    the smaller showers without flattening a sharp maximum much."""
    return [(v[i - 1] + v[i] + v[(i + 1) % N_BINS]) / 3 for i in range(N_BINS)]


def _mean_sd(xs: list[float]) -> tuple[float, float]:
    m = sum(xs) / len(xs)
    sd = (sum((x - m) ** 2 for x in xs) / (len(xs) - 1)) ** 0.5 if len(xs) > 1 else 0.0
    return m, sd


def build(years: list[int], zhr: dict[str, int] | None = None) -> dict:
    """Profiles per shower as the mean of the years' shapes with their
    year-to-year standard deviation, and the summed ZHR of every shower with
    an IMO rate (`zhr`), its uncertainty taken from the spread of the yearly
    totals so that showers varying together are not treated as independent."""
    total_y = {y: [0] * N_BINS for y in years}
    shower_y: dict[int, dict[str, list[int]]] = {y: defaultdict(lambda: [0] * N_BINS) for y in years}
    rows = 0
    with requests.Session() as session:
        for y in years:
            for m in range(1, 13):
                t0 = time.time()
                n = count_month(y, m, total_y[y], shower_y[y], session)
                rows += n
                logger.info("%04d-%02d: %d meteors (%.0f s)", y, m, n, time.time() - t0)

    total = [sum(total_y[y][b] for y in years) for b in range(N_BINS)]
    codes = set().union(*(shower_y[y].keys() for y in years))
    # Sporadics: the meteors GMN associates with no shower.
    spo_y = {y: [total_y[y][b] - sum(c[b] for c in shower_y[y].values()) for b in range(N_BINS)] for y in years}
    spo = [sum(spo_y[y][b] for y in years) for b in range(N_BINS)]
    showers = {}
    rel_y_by_code: dict[str, dict[int, list[float]]] = {}
    for code in codes:
        counts = [sum(shower_y[y][code][b] if code in shower_y[y] else 0 for y in years) for b in range(N_BINS)]
        n = sum(counts)
        if n < MIN_METEORS:
            continue
        # The season and the noise threshold come from all years pooled.
        share = _smooth([c / t if t else 0.0 for c, t in zip(counts, total)])
        smooth = _smooth([c / s if s > 0 else 0.0 for c, s in zip(counts, spo)])
        top = max(smooth)
        peak_bin = smooth.index(top)
        if share[peak_bin] < MIN_PEAK_SHARE:
            continue
        # Keep the contiguous run of bins around the maximum above the floor:
        # the shower's own season, not stray associations elsewhere in the year.
        keep = {peak_bin}
        for step in (-1, 1):
            i = peak_bin
            while True:
                i = (i + step) % N_BINS
                if smooth[i] / top < FLOOR or i in keep:
                    break
                keep.add(i)
        # Each year's shape on its own; the profile is their mean, scaled so
        # the mean's maximum is 1, with the years' standard deviation.
        per_year = {}
        for y in years:
            c = shower_y[y][code] if code in shower_y[y] else [0] * N_BINS
            per_year[y] = _smooth([ci / s if s > 0 else 0.0 for ci, s in zip(c, spo_y[y])])
        stats = {b: _mean_sd([per_year[y][b] for y in years]) for b in keep}
        scale = max(m for m, _ in stats.values()) or 1.0
        rel_y_by_code[code] = {y: [per_year[y][b] / scale if b in keep else 0.0 for b in range(N_BINS)]
                               for y in years}
        # Order by signed distance from the peak, so a season across the
        # 0/360-degree seam stays in one piece.
        bins = sorted(keep, key=lambda i: (i - peak_bin + N_BINS // 2) % N_BINS - N_BINS // 2)
        mean_peak = max(keep, key=lambda b: stats[b][0])
        showers[code] = {
            "n": n,
            "peak_sol": bin_sol(mean_peak),
            # the shower's share of all GMN meteors at its maximum (years pooled)
            "peak_share": round(share[peak_bin], 4),
            # [J2000 solar longitude, mean of the years, their standard
            # deviation], both as a fraction of the mean's maximum
            "profile": [[bin_sol(i), round(stats[i][0] / scale, 3), round(stats[i][1] / scale, 3)] for i in bins],
        }

    out = {
        "source": "Global Meteor Network trajectory summaries (globalmeteornetwork.org/data)",
        "years": years,
        "bin_deg": BIN_DEG,
        "meteors": rows,
        "built": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "showers": showers,
    }
    if zhr:
        # Sum the rated showers' ZHR curves within each year, then take the
        # mean and standard deviation of the yearly totals. Adding per-shower
        # errors in quadrature would assume the showers vary independently;
        # the same skies and network serve them all in a given year.
        summed = sorted(c for c in showers if zhr.get(c) is not None)
        yearly = {y: [sum(zhr[c] * rel_y_by_code[c][y][b] for c in summed) for b in range(N_BINS)] for y in years}
        out["total"] = {
            "showers": summed,
            # [J2000 solar longitude, mean ZHR of the years, standard deviation]
            "series": [[bin_sol(b), *(round(v, 2) for v in _mean_sd([yearly[y][b] for y in years]))]
                       for b in range(N_BINS)],
        }
    return out


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--years", default="2022-2025", help="e.g. 2022-2025 or 2023,2025")
    ap.add_argument("--output", type=Path, required=True)
    a = ap.parse_args()
    if "-" in a.years:
        lo, hi = (int(x) for x in a.years.split("-"))
        years = list(range(lo, hi + 1))
    else:
        years = [int(x) for x in a.years.split(",")]
    # The IMO rates the page draws heights from (dashboard/mdc_poller.py).
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))
    from mdc_poller import _ZHR
    out = build(years, {c: z for c, z in _ZHR.items() if c not in ("SPO", "ANT")})
    a.output.parent.mkdir(parents=True, exist_ok=True)
    tmp = a.output.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, separators=(",", ":")))
    tmp.replace(a.output)
    logger.info("%d showers with a profile from %d meteors -> %s", len(out["showers"]), out["meteors"], a.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
