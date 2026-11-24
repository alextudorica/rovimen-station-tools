#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["astropy", "numpy", "pillow", "requests", "scipy"]
# ///
"""Solve a spectro/widefield camera's fixed alt-az pointing from a night stack.

Whenever GH662S or GH662W is re-aimed, run this against one dark-night stack
(webp or png; the OSD banner is cropped automatically) to regenerate the
``stations.<code>.spectro`` config block that spectro_pipeline.py consumes.

Method: blind astrometric solve via nova.astrometry.net (the RMS project API
key), then conversion of the tangent-plane solution to a fixed alt-az camera
basis (optical axis c, image-right R, image-up U) at the stack's UTC time.
The basis is time-independent for a fixed camera; verify by solving a second
stack from a different hour: center az/alt must agree to well under a degree
(measured drift on GH662S: 0.1 deg over 3 h).

Usage (a uv script with its own environment: it needs scipy, which the
project does not carry):
    uv run --script tools/solve_spectro_pointing.py STACK.webp 2026-09-05T22:30:43 \
        --lat 44.607966 --lon 25.282230
"""

import argparse
import json
import sys
import time

import numpy as np

NOVA = "http://nova.astrometry.net/api/"
RMS_API_KEY = "sybwjtfjbrpgomep"  # RMS project's public nova key (RMS/Astrometry)
FRAME_W, FRAME_H = 1280, 720


def prepare(path: str) -> str:
    from PIL import Image
    from scipy.ndimage import median_filter

    im = np.asarray(Image.open(path).convert("L"), dtype=float)[: FRAME_H - 2]
    sub = im - median_filter(im, size=31)
    out = "/tmp/solve_input.png"
    Image.fromarray(np.clip(sub * 8 + 30, 0, 255).astype(np.uint8)).save(out)
    return out


def nova_solve(png: str, scale_lo: float, scale_hi: float) -> dict:
    import requests

    r = requests.post(NOVA + "login",
                      data={"request-json": json.dumps({"apikey": RMS_API_KEY})})
    session = r.json()["session"]
    r = requests.post(
        NOVA + "upload",
        data={"request-json": json.dumps({
            "session": session, "publicly_visible": "n",
            "scale_units": "arcsecperpix",
            "scale_lower": scale_lo, "scale_upper": scale_hi})},
        files={"file": ("stack.png", open(png, "rb"), "application/octet-stream")})
    subid = r.json()["subid"]
    for _ in range(120):
        time.sleep(5)
        jobs = [j for j in requests.get(NOVA + f"submissions/{subid}").json().get("jobs", []) if j]
        if jobs:
            st = requests.get(NOVA + f"jobs/{jobs[0]}").json().get("status")
            if st == "success":
                wcs = requests.get(f"http://nova.astrometry.net/wcs_file/{jobs[0]}").content
                return {"wcs": wcs, "job": jobs[0]}
            if st == "failure":
                raise RuntimeError("nova solve failed")
    raise RuntimeError("nova solve timed out")


def basis_from_wcs(wcs_bytes: bytes, iso_time: str, lat: float, lon: float) -> dict:
    from astropy.coordinates import AltAz, EarthLocation, SkyCoord
    from astropy.io import fits
    from astropy.time import Time
    from astropy.wcs import WCS
    import astropy.units as u

    hdr = fits.Header.fromstring(wcs_bytes.decode("latin1"), sep="")
    w = WCS(hdr)
    site = EarthLocation(lat=lat * u.deg, lon=lon * u.deg, height=200 * u.m)
    aa = AltAz(obstime=Time(iso_time), location=site)

    def altaz_vec(px, py):
        ra, dec = w.wcs_pix2world([[px, py]], 0)[0]
        s = SkyCoord(ra=ra * u.deg, dec=dec * u.deg).transform_to(aa)
        az, al = np.radians(s.az.deg), np.radians(s.alt.deg)
        return np.array([np.cos(al) * np.sin(az), np.cos(al) * np.cos(az), np.sin(al)])

    cx, cy = FRAME_W / 2, FRAME_H / 2 - 1
    c = altaz_vec(cx, cy)
    vx, vy = altaz_vec(cx + 120, cy), altaz_vec(cx, cy - 100)
    R = vx - (vx @ c) * c
    R /= np.linalg.norm(R)
    U = vy - (vy @ c) * c
    U /= np.linalg.norm(U)
    ang = lambda v: np.degrees(np.arctan2(np.linalg.norm(np.cross(v, c)), v @ c))
    scale_x, scale_y = 120 / ang(vx), 100 / ang(vy)
    az_c = float(np.degrees(np.arctan2(c[0], c[1])) % 360)
    alt_c = float(np.degrees(np.arcsin(c[2])))
    return {
        "az_center": round(az_c, 2), "alt_center": round(alt_c, 2),
        "scale_x": round(float(scale_x), 2), "scale_y": round(float(scale_y), 2),
        "basis": [[round(float(v), 6) for v in row] for row in (c, R, U)],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("stack")
    ap.add_argument("iso_time", help="UTC of the stack, e.g. 2026-09-05T22:30:43")
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--scale-range", default="130:170",
                    help="arcsec/px bounds; use 240:450 for the 1.7 mm widefield")
    args = ap.parse_args()

    lo, _, hi = args.scale_range.partition(":")
    png = prepare(args.stack)
    solved = nova_solve(png, float(lo), float(hi))
    block = basis_from_wcs(solved["wcs"], args.iso_time, args.lat, args.lon)
    print("config block (merge into stations.<code>.spectro; keep dispersion keys):")
    print(json.dumps(block, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
