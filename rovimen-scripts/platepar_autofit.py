#!/usr/bin/env python3
"""platepar_autofit.py — Automated platepar refit for a camera's last completed night.

Headless wrapper around RMS.Astrometry.AutoPlatepar.autoFitPlatepar() -- the
same code path SkyFit2's own "Auto Fit" button uses (single best-scored
frame, blind astrometry.net plate-solve, NN refinement, photometric-outlier
+ blended-star filtering, final fit). Doesn't need a decent starting
platepar, which is the point: this runs precisely when the deployed
platepar has already drifted.

Adds one thing RMS doesn't do out of the box: after the fit converges,
individual star matches whose residual exceeds a threshold are dropped and
the platepar is refit on the trimmed set. RMS's own outlier handling
(filterPhotometricOutliers, filterBlendedStars) is magnitude/blend-based,
not position-based.

The threshold self-calibrates per camera and per night: SIGMA_CLIP_FACTOR
times the fit's own median residual (not a fixed arcmin number -- a fixed
cut is a pixel-domain quantity in disguise and is only correct for one
specific lens' plate scale; see the PR #884 review). Two guards keep this
from silently masking a genuine distortion-model misfit instead of cleaning
up real outliers: the minimum-stars-after-reject floor (see
_min_stars_after_reject) scales with the configured distortion model's own
parameter count (a handful of stars cannot
meaningfully constrain a 7-term radial fit), and REJECT_FRACTION_CAP refuses
to trim at all if too large a fraction of matches are over threshold --
that pattern means the distortion model doesn't fit this glass, and
clipping those stars would bias the refit toward the field center while
making the reported residual look better, not actually improve the plate.

The result is STAGED, not applied live -- see accept_candidate(). The
candidate .cal file + a result.json sit under
~/rovimen_scripts/calibration_candidates/<cam>/, so an operator (or the
dashboard, via the trigger/accept endpoints in station_api.py) can review
the old-vs-new residual comparison before anything touches the live
platepar. --apply on the CLI skips staging entirely for the manual/SSH
workflow, matching the existing fleet convention (write a timestamped .bak
of the previous platepar before overwriting).

Standalone usage:
  python platepar_autofit.py -c config.json --camera RO000H [--date 20260322] [--apply]

Cron/trigger usage (see station_api.py's /api/calibration/autofit/<cam>):
  python platepar_autofit.py -c config.json --camera RO000H --json
"""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import math
import secrets
import shutil
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ff_thumbs
import rovimen_rms

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = Path.home() / 'rovimen_scripts' / 'config.json'
DEFAULT_MATCH_RADIUS_PX = 10.0
CANDIDATES_DIR_NAME = 'calibration_candidates'

# Outlier threshold = SIGMA_CLIP_FACTOR * median(residual), computed fresh
# from the fit's own residual distribution -- self-calibrates per camera and
# per night instead of needing a per-lens table (see module docstring).
DEFAULT_SIGMA_CLIP_FACTOR = 3.0

# MIN_STARS_AFTER_REJECT scales with the configured distortion model's own
# parameter count (platepar.poly_length -- e.g. 7 for the fleet-standard
# radial7-odd) rather than a flat number: a handful of stars cannot
# meaningfully constrain a several-term radial fit, and the previous fixed
# floor of 10 was well below what a full pointing+distortion refit needs.
MIN_STARS_PER_DISTORTION_PARAM = 3.5
MIN_STARS_ABSOLUTE_FLOOR = 8  # rigid-body fit alone needs >= 4; keep a margin

# If more than this fraction of matched stars are over the residual
# threshold, that's a distortion-model misfit (or a bad solve), not outlier
# cleanup -- refuse to trim and flag the run as suspect instead of biasing
# the refit toward the field center while the reported residual improves.
REJECT_FRACTION_CAP = 0.20


def _min_stars_after_reject(poly_length: int | None) -> int:
    if not poly_length:
        return MIN_STARS_ABSOLUTE_FLOOR
    return max(MIN_STARS_ABSOLUTE_FLOOR, math.ceil(MIN_STARS_PER_DISTORTION_PARAM * poly_length))


def _field_quadrant(img_x: float, img_y: float, x_res: float, y_res: float) -> str:
    """Coarse diagnostic label for where on the sensor a star match sits --
    not astronomically precise (image Y increases downward), just enough to
    tell an operator "rejects clustered in one corner" (distortion-model
    misfit) from "rejects scattered" (real per-star outliers)."""
    ew = 'E' if img_x >= x_res/2 else 'W'
    ns = 'S' if img_y >= y_res/2 else 'N'
    return ns + ew


def _field_radius_frac(img_x: float, img_y: float, x_res: float, y_res: float) -> float:
    """Distance from field center, normalized so the corner is ~1.0."""
    cx, cy = x_res/2, y_res/2
    corner_r = math.hypot(cx, cy)
    if corner_r == 0:
        return 0.0
    return math.hypot(img_x - cx, img_y - cy) / corner_r


def _night_date() -> str:
    """Return the date string (YYYYMMDD) for the most recently COMPLETED
    night (UTC). Always now - 1 day, never "today" -- a session starting
    tonight isn't processed until tomorrow's dawn no matter what time it is
    right now. See the #883 fix (routes/archive.py / station_api.py
    api_calibration_summary) for the fuller reasoning; this mirrors it
    rather than the older now.hour<12 branch some other station scripts
    still carry."""
    now = datetime.now(timezone.utc)
    return (now - timedelta(days=1)).strftime('%Y%m%d')


# Directory names never worth descending into when hunting for a camera's
# live .config. CapturedFiles/ArchivedFiles matter most: RMS copies the
# .config into EVERY night directory, so they are full of files that declare
# the right stationID and are the wrong answer -- writing a mask or a
# platepar into one changes nothing the camera does.
# The RMS config-directory resolver (stationID binding, directory heuristics,
# live-process check) lives in rovimen_rms so every station script resolves a
# camera's .config, platepar and mask the same way.
_CONFIG_SCAN_SKIP = rovimen_rms._CONFIG_SCAN_SKIP
_CONFIG_SCAN_MAX_DIRS = rovimen_rms._CONFIG_SCAN_MAX_DIRS
_live_rms_config_dirs = rovimen_rms.live_rms_config_dirs
_candidate_config_dirs = rovimen_rms.candidate_config_dirs
_station_id_of = rovimen_rms.station_id_of
resolve_rms_config_dir = rovimen_rms.resolve_rms_config_dir


def _find_night_dir(rms_data_path: Path, camera_code: str, date_str: str) -> Path | None:
    """Find this camera's capture directory for the given night. Prefers
    ArchivedFiles (the night has usually rotated out of CapturedFiles by
    the time this runs)."""
    for sub in ('ArchivedFiles', 'CapturedFiles'):
        parent = rms_data_path / sub
        if not parent.exists():
            continue
        matches = sorted(p for p in parent.glob(f'{camera_code}_{date_str}_*') if p.is_dir())
        if matches:
            return matches[-1]
    return None


def _candidates_dir(camera_code: str) -> Path:
    return Path.home() / 'rovimen_scripts' / CANDIDATES_DIR_NAME / camera_code


def run_autofit(
    camera_code: str,
    scfg: dict,
    date_str: str,
    *,
    arcmin_reject_threshold: float | None = None,
    sigma_clip_factor: float = DEFAULT_SIGMA_CLIP_FACTOR,
    match_radius_px: float = DEFAULT_MATCH_RADIUS_PX,
) -> dict:
    """Run an automated platepar refit for one camera/night. Never raises --
    always returns a result dict, and ALWAYS persists it to
    <candidates_dir>/<run_id>.json regardless of outcome (including early
    failures like "no data" or "fit failed").

    That unconditional write matters: station_api.py's autofit_status poll
    infers "still running" vs "finished" by comparing the trigger's log file
    mtime against the newest result JSON's mtime. If early-failure paths
    never wrote a result, a failed run would show as "running" forever --
    the poller would have nothing newer to compare against. All the actual
    fitting logic lives in _run_autofit_inner; this wrapper's only job is
    guaranteeing every exit path (success or failure) leaves a result
    behind."""
    run_id = f"{camera_code}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    result = _run_autofit_inner(
        camera_code, scfg, date_str, run_id,
        arcmin_reject_threshold=arcmin_reject_threshold,
        sigma_clip_factor=sigma_clip_factor,
        match_radius_px=match_radius_px,
    )
    # setdefault would no-op here: _run_autofit_inner's initial dict already
    # sets the key to None, so it's "present" even on early-failure paths
    # that never reach a later assignment.
    if result.get('finished_at') is None:
        result['finished_at'] = datetime.now(timezone.utc).isoformat()

    cand_dir = _candidates_dir(camera_code)
    try:
        cand_dir.mkdir(parents=True, exist_ok=True)
        (cand_dir / f'{run_id}.json').write_text(json.dumps(result, indent=2))
    except OSError:
        logger.exception('[%s] Could not write autofit result JSON', camera_code)

    return result


def _run_autofit_inner(
    camera_code: str,
    scfg: dict,
    date_str: str,
    run_id: str,
    *,
    arcmin_reject_threshold: float | None,
    sigma_clip_factor: float,
    match_radius_px: float,
) -> dict:
    """Does the actual fitting. See run_autofit for the result-persistence
    guarantee wrapped around this."""

    result: dict = {
        'camera': camera_code,
        'date': date_str,
        'run_id': run_id,
        'started_at': datetime.now(timezone.utc).isoformat(),
        'finished_at': None,
        'status': 'error',
        'detail': '',
    }

    if not ff_thumbs.ensure_rms_venv_on_path():
        result['detail'] = 'RMS venv not found / numpy unavailable'
        return result

    # Deferred until the RMS venv is confirmed on sys.path.
    import numpy as np
    import RMS.ConfigReader as rms_cr
    from RMS.Astrometry.ApplyAstrometry import xyToRaDecPP
    from RMS.Astrometry.AutoPlatepar import autoFitPlatepar
    from RMS.Astrometry.CheckFit import matchStarsResiduals, starListToDict
    from RMS.Astrometry.StarClasses import PairedStars
    from RMS.Formats import CALSTARS, StarCatalog
    from RMS.Formats.Platepar import Platepar, getCatalogStarsImagePositions
    from RMS.Math import angularSeparation

    rms_data_path = Path(scfg.get('rms_data_path', ''))
    night_dir = _find_night_dir(rms_data_path, camera_code, date_str)
    if night_dir is None:
        result['status'] = 'no_data'
        result['detail'] = f'No CapturedFiles/ArchivedFiles night directory for {date_str}'
        return result

    calstars_files = sorted(night_dir.glob('CALSTARS_*'))
    if not calstars_files:
        result['status'] = 'no_data'
        result['detail'] = 'No CALSTARS file for this night'
        return result

    config_dir = resolve_rms_config_dir(camera_code, scfg)
    if config_dir is None:
        result['detail'] = (f'could not identify an RMS config directory for {camera_code} '
                            f'(no ~/RMS_* declares stationID: {camera_code})')
        return result
    config_path = config_dir / '.config'
    if not config_path.exists():
        result['detail'] = f'RMS .config not found at {config_path}'
        return result

    rms_config = rms_cr.parse(str(config_path))

    old_platepar_path = config_dir / rms_config.platepar_name
    if not old_platepar_path.exists():
        result['detail'] = f'Platepar not found at {old_platepar_path}'
        return result

    old_platepar = Platepar()
    old_platepar.read(str(old_platepar_path))

    years_from_j2000 = (
        datetime.now(timezone.utc).replace(tzinfo=None) - datetime(2000, 1, 1, 12, 0, 0)
    ).total_seconds() / (365.25*24*3600)

    catalog_status = StarCatalog.readStarCatalog(
        rms_config.star_catalog_path, rms_config.star_catalog_file,
        years_from_J2000=years_from_j2000, lim_mag=rms_config.catalog_mag_limit,
        mag_band_ratios=rms_config.star_catalog_band_ratios,
    )
    if not catalog_status:
        result['detail'] = 'Could not load star catalog'
        return result
    catalog_stars = catalog_status[0]

    # Headless equivalent of SkyFit2's "Auto Fit": picks the best single
    # frame itself, blind-solves via astrometry.net (no decent starting
    # platepar required), NN-refines, filters photometric outliers + blends,
    # final fit. platepar_template carries station location/resolution only
    # -- old_platepar's actual pointing is NOT used as a starting guess.
    new_platepar, paired_stars, best_ff = autoFitPlatepar(
        str(night_dir), rms_config, catalog_stars, platepar_template=old_platepar,
        verbose=False,
    )

    if new_platepar is None or not paired_stars or len(paired_stars) == 0:
        result['status'] = 'fit_failed'
        result['detail'] = f'autoFitPlatepar could not solve a platepar (best frame tried: {best_ff})'
        result['finished_at'] = datetime.now(timezone.utc).isoformat()
        return result

    # Build a single-frame star_dict for the winning frame so the old and
    # new platepar's residuals are computed against the exact same star
    # data -- an apples-to-apples "before vs after" number.
    calstars_list, chunk_frames = CALSTARS.readCALSTARS(str(night_dir), calstars_files[-1].name)
    calstars_dict = {ff: stars for ff, stars in calstars_list}
    best_ff_stars = calstars_dict.get(best_ff)
    star_dict = (
        starListToDict(rms_config, ([[best_ff, best_ff_stars]], chunk_frames), max_ffs=1)
        if best_ff_stars else {}
    )

    def _residual_arcmin(pp) -> tuple[int, float | None]:
        """Returns (n_matched, median_residual_arcmin). matchStarsResiduals
        with sky_coords=True already converts its internal degree residuals
        to arcmin before returning (RMS.Astrometry.CheckFit.
        matchStarsResiduals: `global_dist_list.append(ang_dist*60)`,
        `unit_label = 'arcmin'`) -- confirmed against RMS source, not an
        assumption. n_matched is surfaced (not just the residual) because
        avg_dist alone is gameable: a fit biased toward the field center
        matches fewer edge stars and reports a lower residual purely from
        sample shrinkage, which would look like an improvement without one."""
        if not star_dict:
            return 0, None
        n_matched, avg_dist, _cost, _m = matchStarsResiduals(
            rms_config, pp, catalog_stars, star_dict, match_radius_px,
            ret_nmatch=True, sky_coords=True,
        )
        return n_matched, (None if n_matched == 0 else float(avg_dist))

    old_n_matched, old_residual_arcmin = _residual_arcmin(old_platepar)

    # --- Outlier rejection (not part of RMS's own autoFitPlatepar, which
    # only filters photometric outliers + blended stars) ---
    jd = new_platepar.JD
    sky_coords_arr = np.array(paired_stars.skyCoords())
    catalog_x, catalog_y, _catalog_mag = getCatalogStarsImagePositions(sky_coords_arr, jd, new_platepar)
    all_coords = paired_stars.allCoords()

    # Pass 1: compute every star's residual (and field position) before
    # deciding on anything -- the threshold itself is derived from this
    # distribution (sigma-clip), so it has to be known in full first.
    residuals_arcmin = []
    field_info = []  # (quadrant, radius_frac) per star, same order
    for cat_x, cat_y, sky_c, paired_data in zip(catalog_x, catalog_y, sky_coords_arr, all_coords):
        img_x, img_y = paired_data[0][0], paired_data[0][1]
        ra, dec, _mag = sky_c
        _, ra_img, dec_img, _ = xyToRaDecPP(
            [jd], [img_x], [img_y], [1], new_platepar, extinction_correction=False, jd_time=True,
        )
        ang_arcmin = 60*np.degrees(angularSeparation(
            np.radians(ra), np.radians(dec), np.radians(ra_img[0]), np.radians(dec_img[0]),
        ))
        residuals_arcmin.append(float(ang_arcmin))
        field_info.append((
            _field_quadrant(img_x, img_y, new_platepar.X_res, new_platepar.Y_res),
            _field_radius_frac(img_x, img_y, new_platepar.X_res, new_platepar.Y_res),
        ))

    if arcmin_reject_threshold is not None:
        threshold_used = arcmin_reject_threshold
    else:
        threshold_used = sigma_clip_factor * float(np.median(residuals_arcmin))

    keep_mask = [r <= threshold_used for r in residuals_arcmin]
    n_over_threshold = keep_mask.count(False)
    reject_fraction = n_over_threshold/len(keep_mask) if keep_mask else 0.0
    min_stars_floor = _min_stars_after_reject(getattr(new_platepar, 'poly_length', None))

    reject_applied = False
    reject_skipped_reason = None

    # Spatial breakdown of the (candidate) rejects, regardless of whether
    # trimming actually gets applied below -- "5 rejects all in the NE
    # corner" (distortion-model misfit) vs "5 rejects scattered" (real
    # per-star outliers) are different diagnoses, and the accept UI needs to
    # be able to tell them apart.
    reject_quadrants: dict[str, int] = {}
    reject_radius_fracs = []
    for keep, (quadrant, radius_frac) in zip(keep_mask, field_info):
        if not keep:
            reject_quadrants[quadrant] = reject_quadrants.get(quadrant, 0) + 1
            reject_radius_fracs.append(radius_frac)

    if n_over_threshold and reject_fraction > REJECT_FRACTION_CAP:
        reject_skipped_reason = 'high_reject_fraction'
        logger.warning(
            '[%s] %.0f%% of matches over threshold (cap %.0f%%) -- likely a distortion-model '
            'misfit, not outlier cleanup. Keeping untrimmed fit.',
            camera_code, 100*reject_fraction, 100*REJECT_FRACTION_CAP,
        )
    elif n_over_threshold and (len(keep_mask) - n_over_threshold) < min_stars_floor:
        reject_skipped_reason = 'low_stars'
    elif n_over_threshold:
        trimmed = PairedStars()
        trimmed.paired_stars = [
            ps for ps, keep in zip(paired_stars.paired_stars, keep_mask) if keep
        ]
        try:
            img_coords = np.array(trimmed.imageCoords())
            sky_coords_trimmed = np.array(trimmed.skyCoords())
            new_platepar.fitAstrometry(jd, img_coords, sky_coords_trimmed, first_platepar_fit=True)
            paired_stars = trimmed
            reject_applied = True
        except Exception as exc:
            reject_skipped_reason = 'refit_failed'
            logger.warning('[%s] Refit after outlier rejection failed, keeping untrimmed fit: %s',
                            camera_code, exc)

    new_n_matched, new_residual_arcmin = _residual_arcmin(new_platepar)

    # Stage the candidate -- never touches the live platepar. Uses run_id
    # (not a fresh timestamp) so the .cal file and its result JSON
    # (written by the run_autofit wrapper) share one id.
    cand_dir = _candidates_dir(camera_code)
    cand_dir.mkdir(parents=True, exist_ok=True)
    candidate_path = cand_dir / f'{run_id}.cal'
    new_platepar.write(str(candidate_path))

    result.update({
        'status': 'done',
        'finished_at': datetime.now(timezone.utc).isoformat(),
        'candidate_id': run_id,
        'candidate_path': str(candidate_path),
        # Required to apply this candidate via accept_candidate() -- see
        # its docstring for why this is checked rather than candidate_id
        # alone gating the accept call.
        'accept_token': secrets.token_hex(16),
        'best_frame': best_ff,
        'n_stars_used': len(paired_stars),
        'n_stars_over_threshold': n_over_threshold,
        'reject_fraction': round(reject_fraction, 3),
        'reject_applied': reject_applied,
        'reject_skipped_reason': reject_skipped_reason,
        'reject_by_quadrant': reject_quadrants,
        'reject_mean_field_radius_frac': (
            round(sum(reject_radius_fracs)/len(reject_radius_fracs), 3) if reject_radius_fracs else None
        ),
        'arcmin_reject_threshold': round(threshold_used, 3),
        'sigma_clip_factor': sigma_clip_factor if arcmin_reject_threshold is None else None,
        'old_residual_arcmin': round(old_residual_arcmin, 3) if old_residual_arcmin is not None else None,
        'old_n_matched': old_n_matched,
        'new_residual_arcmin': round(new_residual_arcmin, 3) if new_residual_arcmin is not None else None,
        'new_n_matched': new_n_matched,
    })

    return result


# Fewest successfully recalibrated frames a re-base will act on. The night's
# recalibrated platepars scatter by about an arcmin; taking the medoid of a
# handful of them would inherit that scatter instead of averaging through it.
REBASE_MIN_FRAMES = 20

# Distortion terms that must be identical between the base platepar and the
# recalibrated one being adopted. RMS's nightly recalibration fits only
# RA/Dec/rotation/scale and copies these across untouched, so a mismatch
# means the night ran against a DIFFERENT platepar than the one on disk --
# someone re-solved in SkyFit2 since. Adopting it would silently swap the
# distortion model, which is the one thing re-basing promises not to do.
_DISTORTION_KEYS = ('x_poly_fwd', 'y_poly_fwd', 'x_poly_rev', 'y_poly_rev',
                    'distortion_type', 'poly_length')


def _circular_median_deg(values: list[float]) -> float:
    """Median of angles in degrees, safe across the 0/360 seam."""
    if not values:
        raise ValueError('no values')
    ref = values[0]
    unwrapped = [ref + ((v - ref + 180.0) % 360.0 - 180.0) for v in values]
    return statistics.median(unwrapped) % 360.0


def _angsep_deg(az1: float, alt1: float, az2: float, alt2: float) -> float:
    """Angle on the sky between two horizontal directions. Azimuth degrees are
    not sky degrees except on the horizon, which is why this is a spherical
    separation and not a coordinate subtraction."""
    az1, alt1, az2, alt2 = (math.radians(v) for v in (az1, alt1, az2, alt2))
    cos_d = (math.sin(alt1) * math.sin(alt2)
             + math.cos(alt1) * math.cos(alt2) * math.cos(az1 - az2))
    return math.degrees(math.acos(max(-1.0, min(1.0, cos_d))))


def run_rebase_pointing(camera_code: str, scfg: dict, date_str: str,
                        *, min_frames: int = REBASE_MIN_FRAMES) -> dict:
    """Re-base a camera's platepar onto the pointing RMS recalibrated to.
    Never raises -- always returns a result dict, and ALWAYS persists it to
    <candidates_dir>/<run_id>.json regardless of outcome, the same
    unconditional-write guarantee run_autofit's wrapper makes (see its
    docstring): station_api.py's autofit_status poll infers "still running"
    by comparing the trigger's log mtime against the newest result JSON's
    mtime, and an early-failure path that never wrote a result used to leave
    a fast, clean failure (e.g. "could not identify an RMS config
    directory") looking identically "running" as a genuinely wedged process
    until station_api.py's 30-minute stale-log grace window lapsed -- long
    past the dashboard's ~7.5-minute poll timeout, so the UI reported a
    generic "timed out" instead of the real, already-known error. All the
    actual logic lives in _run_rebase_pointing_inner; this wrapper's only
    job is guaranteeing every exit path leaves a result behind, matching
    run_autofit.
    """
    run_id = f'{camera_code}_{datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")}'
    result = _run_rebase_pointing_inner(camera_code, scfg, date_str, run_id, min_frames=min_frames)
    if result.get('finished_at') is None:
        result['finished_at'] = datetime.now(timezone.utc).isoformat()

    cand_dir = _candidates_dir(camera_code)
    try:
        cand_dir.mkdir(parents=True, exist_ok=True)
        (cand_dir / f'{run_id}.json').write_text(json.dumps(result, indent=2))
    except OSError:
        logger.exception('[%s] Could not write re-base result JSON', camera_code)

    return result


def _run_rebase_pointing_inner(camera_code: str, scfg: dict, date_str: str, run_id: str,
                               *, min_frames: int = REBASE_MIN_FRAMES) -> dict:
    """Re-base a camera's platepar onto the pointing RMS recalibrated to.

    This is the cheap counterpart to run_autofit, and the right tool for the
    common case: the distortion model is fine, the camera has simply settled
    a little since the platepar was solved.

    It performs no fit at all. RMS already refits pointing every night --
    ApplyRecalibrate.recalibrateFF minimises over exactly four parameters
    (RA_d, dec_d, pos_angle_ref, F_scale) and copies the distortion across
    untouched -- and writes the per-frame results to
    platepars_all_recalibrated.json. Those entries are complete platepars
    carrying byte-identical distortion to the base one, so re-basing is a
    matter of choosing the frame that best represents the night and
    installing it.

    The medoid is chosen, not an average: the entries are complete platepars
    and averaging their fields independently would produce one that is
    internally inconsistent. Taking the frame nearest the night's median
    az/alt keeps a self-consistent platepar while rejecting the outlying
    frames, and the choice is made in the HORIZONTAL frame because that is
    what a ground-mounted camera holds fixed -- RA/Dec sweep across the night
    with the Earth's rotation, so a median of those is meaningless on
    stations whose recalibrated platepars carry per-frame epochs.

    Never touches the live platepar: the result is staged as a candidate the
    same way run_autofit's is, and applied only through accept_candidate.
    """
    result: dict = {
        'status': 'error', 'camera': camera_code, 'date': date_str,
        'run_id': run_id, 'mode': 'rebase',
        'started_at': datetime.now(timezone.utc).isoformat(), 'detail': '',
    }

    config_dir = resolve_rms_config_dir(camera_code, scfg)
    if config_dir is None:
        result['detail'] = f'could not identify an RMS config directory for {camera_code}'
        return result
    base_path = config_dir / 'platepar_cmn2010.cal'
    try:
        base = json.loads(base_path.read_text())
    except (OSError, ValueError) as exc:
        result['detail'] = f'could not read {base_path}: {exc}'
        return result

    rms_data_path = Path(scfg.get('rms_data_path', ''))
    night_dir = _find_night_dir(rms_data_path, camera_code, date_str)
    if night_dir is None:
        result['detail'] = f'no capture directory for {camera_code} on {date_str}'
        return result
    recalib_path = night_dir / 'platepars_all_recalibrated.json'
    try:
        recalibrated = json.loads(recalib_path.read_text())
    except (OSError, ValueError) as exc:
        result['detail'] = f'could not read {recalib_path}: {exc}'
        return result

    usable = [v for v in recalibrated.values()
              if v.get('auto_recalibrated') and 'az_centre' in v and 'alt_centre' in v]
    result['n_frames_total'] = len(recalibrated)
    result['n_frames_recalibrated'] = len(usable)
    if len(usable) < min_frames:
        result['detail'] = (f'only {len(usable)} of {len(recalibrated)} frames recalibrated '
                            f'on {date_str}; {min_frames} needed to re-base confidently')
        return result

    median_az = _circular_median_deg([e['az_centre'] for e in usable])
    median_alt = statistics.median([e['alt_centre'] for e in usable])
    chosen = min(usable, key=lambda e: _angsep_deg(
        e['az_centre'], e['alt_centre'], median_az, median_alt))

    mismatched = [k for k in _DISTORTION_KEYS
                  if k in base and k in chosen and base[k] != chosen[k]]
    if mismatched:
        result['detail'] = ('the night was processed against a different distortion model '
                            f'({", ".join(mismatched)} differ); re-basing would swap it. '
                            'Re-solve in SkyFit2 instead.')
        result['distortion_mismatch'] = mismatched
        return result

    offset_deg = _angsep_deg(base.get('az_centre', chosen['az_centre']),
                             base.get('alt_centre', chosen['alt_centre']),
                             chosen['az_centre'], chosen['alt_centre'])
    rot_base = base.get('rotation_from_horiz')
    rot_new = chosen.get('rotation_from_horiz')
    rot_delta = (abs((rot_new - rot_base + 180.0) % 360.0 - 180.0)
                 if rot_base is not None and rot_new is not None else None)
    scale_base, scale_new = base.get('F_scale'), chosen.get('F_scale')

    cand_dir = _candidates_dir(camera_code)
    cand_dir.mkdir(parents=True, exist_ok=True)
    candidate_path = cand_dir / f'{run_id}.cal'
    candidate_path.write_text(json.dumps(chosen, indent=4))

    result.update({
        'status': 'done',
        'finished_at': datetime.now(timezone.utc).isoformat(),
        'candidate_id': run_id,
        'candidate_path': str(candidate_path),
        'accept_token': secrets.token_hex(16),
        'source_night': date_str,
        'recalib_ok_pct': round(100.0 * len(usable) / len(recalibrated), 1),
        'pointing_shift_arcmin': round(offset_deg * 60, 2),
        'rotation_shift_arcmin': round(rot_delta * 60, 2) if rot_delta is not None else None,
        'scale_change_pct': (round(100.0 * (scale_new - scale_base) / scale_base, 3)
                             if scale_base else None),
        'distortion_preserved': True,
    })

    return result


def accept_candidate(camera_code: str, scfg: dict, candidate_id: str, accept_token: str = '') -> dict:
    """Apply a staged candidate as the live platepar. Backs up the current
    live platepar to a timestamped .bak first (same convention already used
    fleet-wide, e.g. platepar_cmn2010.cal.bak_20260709_064010). Never
    raises -- always returns a result dict.

    accept_token must match the token minted into the candidate's own result
    JSON when run_autofit produced it (PR #884 review: this is the first
    network-reachable mutation of a live platepar under readonly mode, so
    candidate_id alone -- a predictable, sequential-ish string -- isn't
    sufficient authorization; requiring the token the caller can only have
    gotten by actually reading this specific run's result closes blind/
    replay attempts against it). Loopback/manual callers (accept_candidate
    invoked directly, or the CLI's --apply) don't go through the network
    readonly gate at all, so this check is enforced here too rather than
    solely in station_api.py, in case accept_candidate is ever called from
    a new network-facing path that forgets to check first."""

    cand_dir = _candidates_dir(camera_code)
    candidate_path = cand_dir / f'{candidate_id}.cal'
    if not candidate_path.exists():
        return {'status': 'error', 'error': 'candidate not found — may have expired or already been applied'}

    result_json_path = cand_dir / f'{candidate_id}.json'
    try:
        staged_result = json.loads(result_json_path.read_text())
    except (OSError, ValueError):
        staged_result = {}
    expected_token = staged_result.get('accept_token', '')
    if not expected_token or not hmac.compare_digest(accept_token, expected_token):
        return {'status': 'error', 'error': 'invalid or missing accept_token'}

    config_dir = resolve_rms_config_dir(camera_code, scfg)
    if config_dir is None:
        return {'status': 'error',
                'error': f'could not identify an RMS config directory for {camera_code}'}
    config_path = config_dir / '.config'
    if not config_path.exists():
        return {'status': 'error', 'error': f'RMS .config not found at {config_path}'}

    if not ff_thumbs.ensure_rms_venv_on_path():
        return {'status': 'error', 'error': 'RMS venv not found / numpy unavailable'}

    import RMS.ConfigReader as rms_cr
    rms_config = rms_cr.parse(str(config_path))
    live_path = config_dir / rms_config.platepar_name

    applied_at = datetime.now(timezone.utc)
    backup_path = None
    if live_path.exists():
        backup_name = f'{live_path.name}.bak_{applied_at.strftime("%Y%m%d_%H%M%S")}'
        backup_path = live_path.parent / backup_name
        try:
            shutil.copy2(live_path, backup_path)
        except OSError as exc:
            return {'status': 'error', 'error': f'could not back up live platepar: {exc}'}

    try:
        shutil.copy2(candidate_path, live_path)
    except OSError as exc:
        return {'status': 'error', 'error': f'could not write live platepar: {exc}'}

    # Candidate applied -- remove the staged copy (and its result JSON) so a
    # stale candidate_id can't be re-accepted later.
    result_json = cand_dir / f'{candidate_id}.json'
    for p in (candidate_path, result_json):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass

    return {
        'status': 'applied',
        'applied_at': applied_at.isoformat(),
        'backup_path': str(backup_path) if backup_path else None,
        'candidate_id': candidate_id,
        'live_path': str(live_path),
    }


def _run_cli(cfg: dict, camera_code: str, date_str: str, *, apply: bool,
             arcmin_reject_threshold: float | None, sigma_clip_factor: float,
             match_radius_px: float, as_json: bool, rebase: bool = False) -> int:
    stations = cfg.get('stations', {})
    if camera_code not in stations:
        logger.error('Camera %s not in config', camera_code)
        return 1
    scfg = stations[camera_code]

    if rebase:
        result = run_rebase_pointing(camera_code, scfg, date_str)
    else:
        result = run_autofit(
            camera_code, scfg, date_str,
            arcmin_reject_threshold=arcmin_reject_threshold,
            sigma_clip_factor=sigma_clip_factor,
            match_radius_px=match_radius_px,
        )

    if apply and result['status'] == 'done':
        accept_result = accept_candidate(
            camera_code, scfg, result['candidate_id'], accept_token=result['accept_token'],
        )
        result['accept'] = accept_result

    if as_json:
        print(json.dumps(result, indent=2))
    elif rebase:
        if result['status'] == 'done':
            logger.info(
                '[%s] Re-base done from %s (%d/%d frames recalibrated): pointing moves %s\', '
                'rotation %s\', scale %s%% (candidate %s)',
                camera_code, result['source_night'], result['n_frames_recalibrated'],
                result['n_frames_total'], result['pointing_shift_arcmin'],
                result['rotation_shift_arcmin'], result['scale_change_pct'],
                result['candidate_id'],
            )
        else:
            logger.error('[%s] Re-base failed: %s', camera_code, result.get('detail'))
    else:
        if result['status'] == 'done':
            logger.info(
                '[%s] Auto-fit done: %d/%d stars used (%d over %.2f\' threshold, reject_applied=%s, '
                'skipped_reason=%s). Residual %s\'/%s stars -> %s\'/%s stars (candidate %s)',
                camera_code, result['n_stars_used'],
                result['n_stars_used'] + result['n_stars_over_threshold'],
                result['n_stars_over_threshold'], result['arcmin_reject_threshold'],
                result['reject_applied'], result['reject_skipped_reason'],
                result['old_residual_arcmin'], result['old_n_matched'],
                result['new_residual_arcmin'], result['new_n_matched'], result['candidate_id'],
            )
            if apply:
                logger.info('[%s] Applied: %s', camera_code, result.get('accept'))
        else:
            logger.warning('[%s] %s: %s', camera_code, result['status'], result['detail'])

    return 0 if result['status'] == 'done' else 1


def main() -> int:
    parser = argparse.ArgumentParser(description='Automated platepar refit from a night\'s CALSTARS data')
    parser.add_argument('-c', '--config', default=str(DEFAULT_CONFIG), help='Path to config.json')
    parser.add_argument('--camera', required=True, help='Camera code to refit (e.g. RO000H)')
    parser.add_argument('--date', help='Night date override (YYYYMMDD); defaults to last completed night')
    parser.add_argument('--apply', action='store_true',
                         help='Apply the fitted platepar live immediately (manual/SSH path only -- '
                              'the dashboard trigger always stages and requires a separate accept call)')
    parser.add_argument('--arcmin-reject', type=float, default=None,
                         help='Reject star matches beyond this fixed residual, in arcmin. Overrides the '
                              'default self-calibrating sigma-clip (see --sigma-clip-factor) entirely -- '
                              'only use this for manual one-off tuning, not as a standing per-camera value.')
    parser.add_argument('--sigma-clip-factor', type=float, default=DEFAULT_SIGMA_CLIP_FACTOR,
                         help='Reject star matches beyond this many times the fit\'s own median residual '
                              f'(default {DEFAULT_SIGMA_CLIP_FACTOR}). Ignored if --arcmin-reject is given.')
    parser.add_argument('--match-radius-px', type=float, default=DEFAULT_MATCH_RADIUS_PX,
                         help=f'Star match radius in pixels for residual comparison (default {DEFAULT_MATCH_RADIUS_PX})')
    parser.add_argument('--rebase', action='store_true',
                        help='Re-base pointing from the night\'s recalibrated platepars instead of '
                             'blind-solving. Preserves the distortion model; no fit is performed.')
    parser.add_argument('--json', action='store_true', help='Print the result as JSON (used by the trigger endpoint)')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%dT%H:%M:%S',
    )

    try:
        with open(args.config) as f:
            cfg = json.load(f)
    except FileNotFoundError:
        logger.error('Config not found: %s', args.config)
        return 1

    date_str = args.date or _night_date()
    return _run_cli(
        cfg, args.camera, date_str, apply=args.apply,
        arcmin_reject_threshold=args.arcmin_reject, sigma_clip_factor=args.sigma_clip_factor,
        match_radius_px=args.match_radius_px, as_json=args.json, rebase=args.rebase,
    )


if __name__ == '__main__':
    sys.exit(main())
