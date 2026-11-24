"""Pixel -> sky geometry from RMS platepars, numpy only, and a two-camera trail consistency test.

Port of the fireball trigger replay geometry (``scripts/geometry.py`` in the replay
workspace) for the VPS, which has no RMS install. It maps RMS platepar pixel
coordinates to local alt/az and ECEF unit rays, and keeps the replay's
``pair_consistency`` plane-intersection test.

Projection model
----------------
The same route as RMS ``xyToRaDecPP`` -> ``cyXYToRADec``
(``RMS/Astrometry/CyFunctions.pyx``), ported function by function:

1. Distortion: poly3+radial[3|5], radial3/4/5-all and radial3/5/7/9-odd, with
   ``equal_aspect``, ``force_distortion_centre`` and ``asymmetry_corr``; the
   corrected offset divided by ``F_scale`` is in degrees from the field centre.
2. Sky: ``pointingCorrection`` (reference hour angle, refraction of the centre,
   precession and nutation of centre and position angle to J2000), then the
   azimuthal-equidistant step to RA/Dec and ``eqRefractionApparentToTrue``.
3. Alt/az: J2000 -> epoch of date (``equatorialCoordPrecession``) and
   ``cyraDec2AltAz`` without refraction, so ``Camera.altaz`` returns the true
   (geometric) direction. Everything is evaluated at the platepar's own JD; the
   pixel -> alt/az mapping drifts by under 0.01 deg over months.

The shortcut through the platepar's stored ``az_centre``/``alt_centre``/
``rotation_from_horiz`` is not used: on the fleet's platepars those fields sit
0.06-3.5 deg (13 deg on one RO000R file) from where RMS actually maps the image
centre, because the recalibration paths do not keep them current.

Validation (2026-10-17), 9x9 pixel grid (corners included) on all 731 distinct
platepars of the fireball trigger replay (an unpublished replay dataset),
against RMS (``xyToRaDecPP`` then ``trueRaDec2ApparentAltAz(refraction=False)``):
at the same JD the error is at most 1.2e-6 deg; with RMS at a fixed 2026-10-09 JD
and this module at the platepar JD, max 0.0060 deg, p99 0.0056 deg, including
pixels below 5 deg altitude. The replay's own ``geometry.py`` converts RMS's
J2000 RA/Dec to alt/az without precessing to the date, which rotates its sky by
0.34-0.37 deg; this module does precess. ``tests/test_fireball_geometry.py``
pins RMS reference values for four platepars.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

import numpy as np

logger = logging.getLogger(__name__)

A_WGS = 6378137.0
E2 = 6.69437999014e-3

_RADIAL_TYPES = (
    "radial3-all", "radial4-all", "radial5-all",
    "radial3-odd", "radial5-odd", "radial7-odd", "radial9-odd",
)
_POLY_TYPES = ("poly3+radial", "poly3+radial3", "poly3+radial5")


# --------------------------------------------------------------------------------------------- earth


def geodetic_to_ecef(lat: float, lon: float, h: float) -> np.ndarray:
    la, lo = np.radians(lat), np.radians(lon)
    n = A_WGS / np.sqrt(1 - E2 * np.sin(la) ** 2)
    return np.array([(n + h) * np.cos(la) * np.cos(lo), (n + h) * np.cos(la) * np.sin(lo),
                     (n * (1 - E2) + h) * np.sin(la)])


def _bowring_lat(p: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    x, y, z = p[:, 0], p[:, 1], p[:, 2]
    b = A_WGS * np.sqrt(1 - E2)
    ep2 = (A_WGS ** 2 - b ** 2) / b ** 2
    r = np.hypot(x, y)
    th = np.arctan2(z * A_WGS, r * b)
    lat = np.arctan2(z + ep2 * b * np.sin(th) ** 3, r - E2 * A_WGS * np.cos(th) ** 3)
    return lat, r


def ecef_height(p: np.ndarray) -> np.ndarray:
    """Geodetic height (m) of ECEF points, (3,) or (n,3) (Bowring, one iteration is plenty here)."""
    lat, r = _bowring_lat(np.atleast_2d(p))
    n = A_WGS / np.sqrt(1 - E2 * np.sin(lat) ** 2)
    return r / np.cos(lat) - n


def ecef_latlon(p: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Geodetic latitude and longitude (degrees) of ECEF points, (3,) or (n,3)."""
    p = np.atleast_2d(p)
    lat, _ = _bowring_lat(p)
    return np.degrees(lat), np.degrees(np.arctan2(p[:, 1], p[:, 0]))


def enu_to_ecef(lat: float, lon: float, v: np.ndarray) -> np.ndarray:
    la, lo = np.radians(lat), np.radians(lon)
    r = np.array([[-np.sin(lo), -np.sin(la) * np.cos(lo), np.cos(la) * np.cos(lo)],
                  [np.cos(lo), -np.sin(la) * np.sin(lo), np.cos(la) * np.sin(lo)],
                  [0, np.cos(la), np.sin(la)]])
    return v @ r.T


# ---------------------------------------------------------------------------------------- distortion


def _undistort(pp: dict, x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Pixel -> offset from the field centre in degrees (RMS ``cyXYToRADec``, distortion step)."""
    x_res, y_res = float(pp["X_res"]), float(pp["Y_res"])
    xp = np.asarray(pp["x_poly_fwd"], float)
    yp = np.asarray(pp["y_poly_fwd"], float)
    dist = pp["distortion_type"]
    xi = x - x_res / 2.0
    yi = y - y_res / 2.0

    if dist.startswith("poly3+radial"):
        x0, y0 = xp[0], yp[0]
        r = np.sqrt((xi - x0) ** 2 + (yi - y0) ** 2)
        dx = (x0 + xp[1] * xi + xp[2] * yi + xp[3] * xi ** 2 + xp[4] * xi * yi + xp[5] * yi ** 2
              + xp[6] * xi ** 3 + xp[7] * xi ** 2 * yi + xp[8] * xi * yi ** 2 + xp[9] * yi ** 3
              + xp[10] * xi * r + xp[11] * yi * r)
        dy = (y0 + yp[1] * xi + yp[2] * yi + yp[3] * xi ** 2 + yp[4] * xi * yi + yp[5] * yi ** 2
              + yp[6] * xi ** 3 + yp[7] * xi ** 2 * yi + yp[8] * xi * yi ** 2 + yp[9] * yi ** 3
              + yp[10] * yi * r + yp[11] * xi * r)
        if dist.endswith(("+radial3", "+radial5")):
            dx = dx + xp[12] * xi * r ** 3
            dy = dy + yp[12] * yi * r ** 3
        if dist.endswith("+radial5"):
            dx = dx + xp[13] * xi * r ** 5
            dy = dy + yp[13] * yi * r ** 5
    else:
        off = 0
        if pp.get("force_distortion_centre", False):
            x0, y0 = 0.5 / (x_res / 2.0), 0.5 / (y_res / 2.0)
            off += 2
        else:
            x0, y0 = xp[0], xp[1]
        x0 *= x_res / 2.0
        y0 *= y_res / 2.0
        # C fmod semantics: the Cython source is compiled with cdivision=True
        x0 = -x_res / 2.0 + np.fmod(x0 + x_res / 2.0, x_res)
        y0 = -y_res / 2.0 + np.fmod(y0 + y_res / 2.0, y_res)
        if pp.get("equal_aspect", True):
            xy = 0.0
            off += 1
        else:
            xy = xp[2 - off]
        if pp.get("asymmetry_corr", False):
            a1 = xp[3 - off]
            a2 = np.fmod(xp[4 - off] * 2 * np.pi, 2 * np.pi)
        else:
            a1 = a2 = 0.0
            off += 2
        k = [xp[i - off] if len(xp) > i - off else 0.0 for i in (5, 6, 7, 8)]
        k1, k2, k3, k4 = k
        r = np.sqrt((xi - x0) ** 2 + ((1.0 + xy) * (yi - y0)) ** 2)
        r = r + a1 * (1.0 + xy) * (yi - y0) * np.cos(a2) - a1 * (xi - x0) * np.sin(a2)
        r = r / (x_res / 2.0)
        if dist == "radial3-all":
            rc = r + k1 * r ** 2 + k2 * r ** 3
        elif dist == "radial4-all":
            rc = r + k1 * r ** 2 + k2 * r ** 3 + k3 * r ** 4
        elif dist == "radial5-all":
            rc = r + k1 * r ** 2 + k2 * r ** 3 + k3 * r ** 4 + k4 * r ** 5
        elif dist == "radial3-odd":
            rc = r + k1 * r ** 3
        elif dist == "radial5-odd":
            rc = r + k1 * r ** 3 + k2 * r ** 5
        elif dist == "radial7-odd":
            rc = r + k1 * r ** 3 + k2 * r ** 5 + k3 * r ** 7
        else:  # radial9-odd
            rc = r + k1 * r ** 3 + k2 * r ** 5 + k3 * r ** 7 + k4 * r ** 9
        with np.errstate(divide="ignore", invalid="ignore"):
            r_scale = np.where(r == 0, 0.0, rc / r - 1)
        dx = (xi - x0) * r_scale - x0
        dy = (yi - y0) * r_scale * (1.0 + xy) - y0 * (1.0 + xy) + yi * xy

    scale = float(pp["F_scale"])
    return (xi + dx) / scale, (yi + dy) / scale


# ------------------------------------------------------------------------------------------------ sky
# Ports of the RMS CyFunctions.pyx astrometry helpers. Angles in radians unless the name says _deg.

J2000_JD = 2451545.0


def _gmst_deg(jd: float) -> float:
    """RMS ``cyjd2LST(jd, 0)`` and the platepar ``Ho`` formula (Meeus)."""
    t = (jd - J2000_JD) / 36525.0
    gst = 280.46061837 + 360.98564736629 * (jd - J2000_JD) + 0.000387933 * t ** 2 - t ** 3 / 38710000.0
    return math.fmod(gst + 360, 360)


def _lst(jd: float, lon_deg: float) -> float:
    return math.radians(math.fmod(_gmst_deg(jd) + lon_deg + 360, 360))


def _precession_angles(start: float, final: float) -> tuple[float, float, float]:
    tt = (start - J2000_JD) / 36525.0
    t = (final - start) / 36525.0
    zeta = ((2306.2181 + 1.39656 * tt - 0.000139 * tt ** 2) * t + (0.30188 - 0.000344 * tt) * t ** 2
            + 0.017998 * t ** 3) / 3600
    z = ((2306.2181 + 1.39656 * tt - 0.000139 * tt ** 2) * t + (1.09468 + 0.000066 * tt) * t ** 2
         + 0.018203 * t ** 3) / 3600
    theta = ((2004.3109 - 0.85330 * tt - 0.000217 * tt ** 2) * t - (0.42665 + 0.000217 * tt) * t ** 2
             - 0.041833 * t ** 3) / 3600
    return math.radians(zeta), math.radians(z), math.radians(theta)


def _precess(start: float, final: float, ra: np.ndarray, dec: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """RMS ``equatorialCoordPrecession`` (Meeus 21.4), vectorised."""
    zeta, z, theta = _precession_angles(start, final)
    a = np.cos(dec) * np.sin(ra + zeta)
    b = np.cos(theta) * np.cos(dec) * np.cos(ra + zeta) - np.sin(theta) * np.sin(dec)
    c = np.sin(theta) * np.cos(dec) * np.cos(ra + zeta) + np.cos(theta) * np.sin(dec)
    ra_c = np.fmod(np.arctan2(a, b) + z + 2 * np.pi, 2 * np.pi)
    near_pole = (np.pi / 2 - np.abs(dec)) < np.radians(0.5)
    dec_c = np.where(near_pole, np.sign(dec) * np.arccos(np.clip(np.sqrt(a ** 2 + b ** 2), -1, 1)),
                     np.arcsin(np.clip(c, -1, 1)))
    return ra_c, dec_c


def _precess_with_rotation(start: float, final: float, ra: float, dec: float, rot: float) -> tuple[float, float, float]:
    """RMS ``equatorialCoordAndRotPrecession``: precession + nutation of the centre and field rotation."""
    if start == final:
        return ra, dec, rot
    zeta, z, theta = _precession_angles(start, final)
    cze, sze, cth, sth, cz, sz = (math.cos(zeta), math.sin(zeta), math.cos(theta), math.sin(theta),
                                  math.cos(z), math.sin(z))
    p = np.array([[cze * cth * cz - sze * sz, -sze * cth * cz - cze * sz, -sth * cz],
                  [cze * cth * sz + sze * cz, -sze * cth * sz + cze * cz, -sth * sz],
                  [cze * sth, -sze * sth, cth]])
    v0 = np.array([math.cos(dec) * math.cos(ra), math.cos(dec) * math.sin(ra), math.sin(dec)])
    v1 = p @ v0
    tt = (start - J2000_JD) / 36525.0
    om = math.radians(125.04452 - 1934.136261 * tt)
    ls = math.radians(280.4665 + 36000.7698 * tt)
    lm = math.radians(218.3165 + 481267.8813 * tt)
    dpsi = math.radians((-17.2 * math.sin(om) - 1.32 * math.sin(2 * ls) - 0.23 * math.sin(2 * lm)
                         + 0.21 * math.sin(2 * om)) / 3600)
    deps = math.radians((9.2 * math.cos(om) + 0.57 * math.cos(2 * ls) + 0.1 * math.cos(2 * lm)
                         - 0.09 * math.cos(2 * om)) / 3600)
    eps_m = np.array([[1, 0, 0], [0, math.cos(deps), math.sin(deps)], [0, -math.sin(deps), math.cos(deps)]])
    psi_m = np.array([[math.cos(dpsi), -math.sin(dpsi), 0], [math.sin(dpsi), math.cos(dpsi), 0], [0, 0, 1]])
    v1 = psi_m @ (eps_m @ v1)
    n0 = np.cross(v0, v1)
    n1 = p @ n0
    v0, v1 = v0 / np.linalg.norm(v0), v1 / np.linalg.norm(v1)
    n0, n1 = n0 / np.linalg.norm(n0), n1 / np.linalg.norm(n1)
    ra1 = math.fmod(math.atan2(v1[1], v1[0]) + 2 * math.pi, 2 * math.pi)
    dec1 = math.asin(max(-1.0, min(1.0, v1[2])))
    par0 = np.array([-math.sin(ra), math.cos(ra), 0.0])
    par1 = np.array([-math.sin(ra1), math.cos(ra1), 0.0])
    par0 = par0 - (par0 @ v0) * v0
    par1 = par1 - (par1 @ v1) * v1
    par0, par1 = par0 / np.linalg.norm(par0), par1 / np.linalg.norm(par1)
    ang0 = math.atan2(np.cross(n0, par0) @ v0, n0 @ par0)
    ang1 = math.atan2(np.cross(n1, par1) @ v1, n1 @ par1)
    rot1 = math.fmod(rot + (ang1 - ang0) + math.pi, 2 * math.pi) - math.pi
    return ra1, dec1, rot1


def _radec_to_altaz(ra, dec, jd: float, lat: float, lon: float):
    """RMS ``cyraDec2AltAz`` (same epoch, no refraction): (azimuth, elevation)."""
    ha = np.fmod(_lst(jd, math.degrees(lon)) - ra + np.pi, 2 * np.pi) - np.pi
    az = np.pi + np.arctan2(np.sin(ha), np.cos(ha) * math.sin(lat) - np.tan(dec) * math.cos(lat))
    s = math.sin(lat) * np.sin(dec) + math.cos(lat) * np.cos(dec) * np.cos(ha)
    return az, np.arcsin(np.clip(s, -1.0, 1.0))


def _altaz_to_radec(az, alt, jd: float, lat: float, lon: float):
    """RMS ``cyaltAz2RADec``."""
    ha = np.arctan2(-np.sin(az), np.tan(alt) * math.cos(lat) - np.cos(az) * math.sin(lat))
    ra = np.fmod(_lst(jd, math.degrees(lon)) - ha + 2 * np.pi, 2 * np.pi)
    dec = np.arcsin(np.clip(math.sin(lat) * np.sin(alt) + math.cos(lat) * np.cos(alt) * np.cos(az), -1, 1))
    return ra, dec


def _refr_apparent_to_true(alt):
    """RMS ``refractionApparentToTrue`` (Explanatory Supplement 1992, p. 144)."""
    e = np.maximum(np.degrees(alt), -0.5)
    return alt - np.radians(1.0 / (60 * np.tan(np.radians(e + 7.31 / (e + 4.4)))))


def _refr_true_to_apparent(alt):
    """RMS ``refractionTrueToApparent``."""
    e = np.maximum(np.degrees(alt), -0.5)
    return alt + np.radians(1.02 / (60 * np.tan(np.radians(e + 10.3 / (e + 5.11)))))


def _eq_refraction(ra, dec, jd: float, lat: float, lon: float, to_true: bool):
    """RMS ``eqRefractionApparentToTrue`` / ``eqRefractionTrueToApparent`` (J2000 in and out)."""
    ra, dec = _precess(J2000_JD, jd, ra, dec)
    az, alt = _radec_to_altaz(ra, dec, jd, lat, lon)
    alt = _refr_apparent_to_true(alt) if to_true else _refr_true_to_apparent(alt)
    ra, dec = _altaz_to_radec(az, alt, jd, lat, lon)
    return _precess(jd, J2000_JD, ra, dec)


def _pointing(pp: dict, jd: float) -> tuple[float, float, float]:
    """RMS ``pointingCorrection``: J2000 centre and position angle used for the sky step at ``jd``."""
    lat, lon = math.radians(float(pp["lat"])), math.radians(float(pp["lon"]))
    h0 = math.radians(float(pp["Ho"]))
    ra_now = math.fmod(math.radians(float(pp["RA_d"])) + math.radians(_gmst_deg(jd)) - h0 + 2 * math.pi,
                       2 * math.pi)
    dec = math.radians(float(pp["dec_d"]))
    if pp.get("refraction", True):
        r, d = _eq_refraction(np.array([ra_now]), np.array([dec]), jd, lat, lon, to_true=False)
        ra_now, dec = float(r[0]), float(d[0])
    return _precess_with_rotation(jd, J2000_JD, ra_now, dec, math.radians(float(pp["pos_angle_ref"])))


# -------------------------------------------------------------------------------------------- camera


@dataclass
class Camera:
    code: str
    lat: float
    lon: float
    elev: float
    pos: np.ndarray
    pp: dict
    jd: float = field(default=J2000_JD, repr=False)
    pointing: tuple[float, float, float] = field(default=(0.0, 0.0, 0.0), repr=False)

    def altaz(self, x, y) -> tuple[np.ndarray, np.ndarray]:
        """True azimuth (from N through E) and altitude, degrees, of image pixels (x right, y down).

        Same route as RMS ``xyToRaDecPP`` followed by J2000 -> epoch of date and alt/az without
        refraction, evaluated at the platepar's own JD.
        """
        x = np.atleast_1d(np.asarray(x, float))
        y = np.atleast_1d(np.asarray(y, float))
        xc, yc = _undistort(self.pp, x, y)
        ra0, dec0, pa = self.pointing
        rad = np.radians(np.hypot(xc, yc))
        theta = np.fmod(np.pi / 2 - pa + np.arctan2(yc, xc), 2 * np.pi)
        s = np.sin(dec0) * np.cos(rad) + np.cos(dec0) * np.sin(rad) * np.cos(theta)
        dec = np.arctan2(s, np.sqrt(np.clip(1 - s ** 2, 0, None)))
        st = np.sin(theta) * np.sin(rad) / np.cos(dec)
        ct = (np.cos(rad) - np.sin(dec) * np.sin(dec0)) / (np.cos(dec) * np.cos(dec0))
        ra = np.fmod(ra0 - np.arctan2(st, ct) + 2 * np.pi, 2 * np.pi)
        lat, lon = math.radians(self.lat), math.radians(self.lon)
        if self.pp.get("refraction", True):
            ra, dec = _eq_refraction(ra, dec, self.jd, lat, lon, to_true=True)
        ra, dec = _precess(J2000_JD, self.jd, ra, dec)
        az, alt = _radec_to_altaz(ra, dec, self.jd, lat, lon)
        return np.degrees(az) % 360.0, np.degrees(alt)

    def rays(self, x, y) -> np.ndarray:
        """(n,3) unit ECEF direction vectors of image pixels."""
        az, alt = self.altaz(x, y)
        a, e = np.radians(az), np.radians(alt)
        enu = np.stack([np.cos(e) * np.sin(a), np.cos(e) * np.cos(a), np.sin(e)], axis=1)
        return enu_to_ecef(self.lat, self.lon, enu)


_REQUIRED = ("lat", "lon", "X_res", "Y_res", "F_scale", "RA_d", "dec_d", "pos_angle_ref", "distortion_type")


def camera_from_platepar(code: str, pp: dict) -> Camera:
    """Camera from a parsed RMS ``platepar_cmn2010.cal`` or the station ``/api/platepar`` dict.

    Missing ``elev`` -> 0 m; missing ``refraction`` -> True and missing ``equal_aspect`` -> True
    (RMS defaults); missing ``Ho`` is recomputed from ``JD`` as RMS does when reading a platepar.
    ``x_poly``/``y_poly`` stand in for absent forward polynomials (RMS v1 platepars).
    Raises ValueError when the platepar cannot be used.
    """
    if not isinstance(pp, dict):
        raise ValueError(f"{code}: platepar is not a dict")  # noqa: TRY004 (callers catch ValueError)
    missing = [k for k in _REQUIRED if pp.get(k) is None]
    if pp.get("JD") is None and pp.get("Ho") is None:
        missing.append("JD")
    if missing:
        raise ValueError(f"{code}: platepar lacks {', '.join(missing)}")
    p = dict(pp)
    for fwd, legacy in (("x_poly_fwd", "x_poly"), ("y_poly_fwd", "y_poly")):
        if p.get(fwd) is None:
            p[fwd] = p.get(legacy)
        if p.get(fwd) is None or len(p[fwd]) == 0:
            raise ValueError(f"{code}: platepar lacks {fwd}")
    dist = p["distortion_type"]
    if dist not in _RADIAL_TYPES and dist not in _POLY_TYPES:
        raise ValueError(f"{code}: unsupported distortion_type {dist!r}")
    need = {"poly3+radial": 12, "poly3+radial3": 13, "poly3+radial5": 14}.get(dist)
    if need and (len(p["x_poly_fwd"]) < need or len(p["y_poly_fwd"]) < need):
        raise ValueError(f"{code}: {dist} needs {need} polynomial terms")
    try:
        if float(p["F_scale"]) <= 0 or float(p["X_res"]) <= 0 or float(p["Y_res"]) <= 0:
            raise ValueError("non-positive F_scale or resolution")
        lat, lon = float(p["lat"]), float(p["lon"])
        elev = float(p.get("elev") or 0.0)
        # Any epoch gives the same pixel -> alt/az mapping; the platepar's own JD keeps it closest to RMS.
        jd = float(p["JD"]) if p.get("JD") is not None else J2000_JD + 9500.0
        if p.get("Ho") is None:
            p["Ho"] = _gmst_deg(jd)
        pointing = _pointing(p, jd)
        probe = Camera(code, lat, lon, elev, geodetic_to_ecef(lat, lon, elev), p, jd, pointing)
        az, alt = probe.altaz([p["X_res"] / 2], [p["Y_res"] / 2])
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError(f"{code}: bad platepar values ({exc})") from exc
    if not (np.all(np.isfinite(az)) and np.all(np.isfinite(alt))):
        raise ValueError(f"{code}: platepar maps the image centre to a non-finite direction")
    return probe


# ----------------------------------------------------------------------------------- consistency test


@dataclass
class Obs:
    """One FR line (or any trail) seen by one camera: rays + times."""
    cam: Camera
    rays: np.ndarray  # (n,3) unit ECEF
    t: np.ndarray     # unix seconds per ray

    @property
    def normal(self) -> np.ndarray:
        if len(self.rays) >= 3:
            _, _, vt = np.linalg.svd(self.rays)
            n = vt[-1]
        else:
            n = np.cross(self.rays[0], self.rays[-1])
        nn = np.linalg.norm(n)
        return n / nn if nn > 0 else n


def ray_plane(origin: np.ndarray, d: np.ndarray, p0: np.ndarray, n: np.ndarray) -> np.ndarray:
    """Distance along ray(s) ``d`` from ``origin`` to the plane through ``p0`` with normal ``n``."""
    den = d @ n
    with np.errstate(divide="ignore", invalid="ignore"):
        return ((p0 - origin) @ n) / den


def _ll_ends(p: np.ndarray) -> list[list[float]]:
    lat, lon = ecef_latlon(p[[0, -1]])
    return [[round(float(lat[i]), 3), round(float(lon[i]), 3)] for i in (0, 1)]


def _theil_sen(t: np.ndarray, x: np.ndarray) -> float:
    """Median of the pairwise slopes dx/dt (pairs closer than 0.05 s skipped); robust to ~29% outliers."""
    i, j = np.triu_indices(len(t), 1)
    dt = t[j] - t[i]
    m = dt > 0.05
    if not m.any():
        return float(np.polyfit(t - t[0], x, 1)[0])
    return float(np.median((x[j] - x[i])[m] / dt[m]))


def pair_consistency(a: Obs, b: Obs, hmin=20e3, hmax=170e3, max_dt=2.5, vmin=8.0, vmax=80.0) -> dict:
    """Plane-intersection test between two single-camera trails.

    Returns a dict with ok flag and diagnostics (convergence angle, heights, speeds, time residual).
    Trails too short for a plane (<2 distinct rays) fall back to closest approach of rays. When the
    plane test gets past the height check it also returns ``ll_a``/``ll_b`` (lat/lon of the first and
    last valid point of each trail, degrees) and ``h_mean_km``.
    """
    res: dict = {"ok": False}
    sep = np.linalg.norm(a.cam.pos - b.cam.pos)
    if len(a.rays) < 2 or len(b.rays) < 2:
        return res | {"why": "short"}
    na, nb = a.normal, b.normal
    if np.linalg.norm(na) == 0 or np.linalg.norm(nb) == 0:
        return res | {"why": "short"}
    q = np.degrees(np.arccos(min(1.0, abs(na @ nb))))
    res["Q"] = round(float(q), 1)
    if sep < 5e3:  # same station: no triangulation, accept if both point into the same sky region
        ma = a.rays.mean(0) / np.linalg.norm(a.rays.mean(0))
        mb = b.rays.mean(0) / np.linalg.norm(b.rays.mean(0))
        ang = np.degrees(np.arccos(np.clip(ma @ mb, -1, 1)))
        dt = abs(np.median(a.t) - np.median(b.t))
        return res | {"ok": bool(ang < 60 and dt < max_dt), "why": "same_station", "ang": round(float(ang), 1)}
    if q < 2.0:
        # near-parallel planes: closest approach of ray pairs
        best = 1e12
        w0 = a.cam.pos - b.cam.pos
        for da in a.rays[:: max(1, len(a.rays) // 10)]:
            for db in b.rays[:: max(1, len(b.rays) // 10)]:
                aa, bb, cc, dd, ee = da @ da, da @ db, db @ db, da @ w0, db @ w0
                den = aa * cc - bb * bb
                if den < 1e-12:
                    continue
                s, tt = (bb * ee - cc * dd) / den, (aa * ee - bb * dd) / den
                if s <= 0 or tt <= 0:
                    continue
                pa, pb = a.cam.pos + s * da, b.cam.pos + tt * db
                h = ecef_height((pa + pb) / 2)[0]
                if hmin <= h <= hmax:
                    best = min(best, float(np.linalg.norm(pa - pb)))
        return res | {"ok": bool(best < 5e3), "why": "parallel", "miss_km": round(best / 1e3, 1)}
    # points of A on plane B and of B on plane A
    sa = ray_plane(a.cam.pos, a.rays, b.cam.pos, nb)
    sb = ray_plane(b.cam.pos, b.rays, a.cam.pos, na)
    pa = a.cam.pos + sa[:, None] * a.rays
    pb = b.cam.pos + sb[:, None] * b.rays
    ha, hb = ecef_height(pa), ecef_height(pb)
    oka = (sa > 0) & (ha > hmin) & (ha < hmax) & (sa < 1500e3)
    okb = (sb > 0) & (hb > hmin) & (hb < hmax) & (sb < 1500e3)
    res.update(fa=round(float(oka.mean()), 2), fb=round(float(okb.mean()), 2))
    if oka.mean() < 0.6 or okb.mean() < 0.6:
        return res | {"why": "height", "ha": [round(float(np.nanmedian(ha)) / 1e3)],
                      "hb": [round(float(np.nanmedian(hb)) / 1e3)]}
    pa, pb, ta, tb = pa[oka], pb[okb], a.t[oka], b.t[okb]
    u = np.cross(na, nb)
    u /= np.linalg.norm(u)
    ref = pa[0]
    xa, xb = (pa - ref) @ u, (pb - ref) @ u
    res.update(h_a=[round(float(ha[oka].max()) / 1e3, 1), round(float(ha[oka].min()) / 1e3, 1)],
               h_b=[round(float(hb[okb].max()) / 1e3, 1), round(float(hb[okb].min()) / 1e3, 1)],
               ll_a=_ll_ends(pa), ll_b=_ll_ends(pb),
               h_mean_km=round(float(np.concatenate([ha[oka], hb[okb]]).mean()) / 1e3, 1))
    # Speed from each camera along the common line, by Theil-Sen: a least-squares fit took the
    # head centroid's first frames, which sit on a static star or jump before they lock on, as
    # real motion (20261021_201634: 74 km/s in the first 0.4 s on RO0007 (Bistrița)). The track
    # is also split, because a fireball that decelerates averages to a speed it never had: the
    # same event ran at 17 km/s down to 40 km and at 7 km/s below 34 km.
    speeds, prof = [], []
    for x, t, h in ((xa, ta, ha[oka]), (xb, tb, hb[okb])):
        if len(x) >= 3 and np.ptp(t) >= 0.12:
            speeds.append(abs(_theil_sen(t, x)) / 1e3)
            n = len(x)
            first, last = slice(0, max(3, n // 2)), slice(n - max(3, n // 3), n)
            prof.append([round(abs(_theil_sen(t[first], x[first])) / 1e3, 1),
                         round(float(h[first].max()) / 1e3, 1), round(float(h[first].min()) / 1e3, 1),
                         round(abs(_theil_sen(t[last], x[last])) / 1e3, 1),
                         round(float(h[last].max()) / 1e3, 1), round(float(h[last].min()) / 1e3, 1)])
    res["v"] = [round(s, 1) for s in speeds]
    res["v_prof"] = prof  # per camera: [v first half, h hi, h lo, v last third, h hi, h lo]
    # timing: fit x(t) on the longer camera, predict the other camera's times
    (x1, t1), (x2, t2) = sorted(((xa, ta), (xb, tb)), key=lambda z: -len(z[0]))
    if len(x1) >= 3 and np.ptp(t1) >= 0.12:
        k, c = np.polyfit(t1 - t1[0], x1, 1)
        if abs(k) > 1e3:
            dt = float(np.median((x2 - c) / k + t1[0] - t2))
        else:
            dt = float(np.median(t2) - np.median(t1))
    else:
        dt = float(np.median(t2) - np.median(t1))
    res["dt"] = round(dt, 2)
    gap = max(0.0, float(max(xa.min(), xb.min()) - min(xa.max(), xb.max()))) / 1e3
    res["gap_km"] = round(gap, 1)
    vok = all(vmin <= s <= vmax for s in speeds) if speeds else True
    res["ok"] = bool(abs(dt) <= max_dt and vok and gap < 150)
    res["why"] = "ok" if res["ok"] else ("dt" if abs(dt) > max_dt else ("speed" if not vok else "gap"))
    return res
