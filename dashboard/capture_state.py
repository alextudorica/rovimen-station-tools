"""Per-camera capture state for /api/overview, from what RMS wrote.

The overview used to count every registry camera of a station with any
``rms-*`` unit up as "capturing": 59 on 2026-11-04 at 10:00 UTC, in daylight,
including gmnro13 (Drăgșina), offline since 2026-09-30, and RO0015 (Iași) and
RO001S (Ferigile), which wrote no FF file for weeks with their units active.
Stations now report per-camera evidence (``capture`` on /api/status, built by
``station_collect.summarise_capture``): the newest FF file, the FF count over
the last half hour against what the frame rate allows, RMS's own capture state
from its log, camera_stall_guard's standing verdict and FF counts per night.

States:

- ``recording``: an FF file within RECORDING_MAX_AGE_S of the station's report.
- ``degraded``: recording, but under DEGRADED_YIELD of the expected FF files
  over the window, or camera_stall_guard holds a degraded/lossy verdict.
- ``stalled``: no recent FF file while RMS says it is capturing, the stall
  guard says stalled, or the sun is below NIGHT_SUN_ALT_DEG at the station.
- ``idle``: no recent FF file and it is not night for RMS (daytime).
- ``no_rms``: the station reports, but no RMS process runs for this camera.
- ``offline``: the station has not reported for STALE_REPORT_S.
- ``unknown``: the station's bundle does not report capture evidence yet.

``last_night`` compares the FF count of the newest finished night with the
night's length (sun below -9 deg, where RMS captures: astronomy.CAPTURE_HORIZON).
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

import astronomy

RECORDING_MAX_AGE_S = 120     # FF every ~10 s; the report itself is cached <= 60 s
STALE_REPORT_S = 15 * 60
DEGRADED_YIELD = 0.5
MIN_WINDOW_EXPECTED = 30      # judge yield only over >= ~5 min of session
NIGHT_SUN_ALT_DEG = -12.0     # well past RMS's -9 deg start: it must be capturing
STARTING_GRACE_S = 300        # RMS's first FF file follows its switch to night by ~1 min
FULL_NIGHT = 0.9
PARTIAL_NIGHT = 0.25


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def sun_altitude_deg(lat: float, lon: float, when: datetime) -> float:
    """Solar altitude in degrees (NOAA low-precision; ~0.2 deg)."""
    n = (when - datetime(2000, 1, 1, 12, tzinfo=timezone.utc)).total_seconds() / 86400.0
    mean_lon = (280.460 + 0.9856474 * n) % 360.0
    g = math.radians((357.528 + 0.9856003 * n) % 360.0)
    ecl = math.radians(mean_lon + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g))
    eps = math.radians(23.439 - 0.0000004 * n)
    ra = math.atan2(math.cos(eps) * math.sin(ecl), math.cos(ecl))
    dec = math.asin(math.sin(eps) * math.sin(ecl))
    gmst = (280.46061837 + 360.98564736629 * n) % 360.0
    ha = math.radians(gmst + lon) - ra
    lat_r = math.radians(lat)
    return math.degrees(math.asin(
        math.sin(lat_r) * math.sin(dec) + math.cos(lat_r) * math.cos(dec) * math.cos(ha)))


def last_finished_night(now: datetime, sun_alt: float | None) -> str:
    """The newest night (its evening's date) whose capture has ended.

    Before noon UTC with the sun up, last night is over; otherwise the night
    of the evening before is the newest finished one."""
    evening = (now - timedelta(hours=12)).date()
    if sun_alt is not None and sun_alt > -9.0 and now.hour < 12:
        return evening.strftime("%Y%m%d")
    return (evening - timedelta(days=1)).strftime("%Y%m%d")


def expected_ffs(lat: float | None, lon: float | None, night: str, period_s: float) -> int | None:
    """FF files a full night allows at the station (sun below -9 deg)."""
    if lat is None or lon is None or period_s <= 0:
        return None
    try:
        tw = astronomy._compute_twilight(float(lat), float(lon), night,
                                         horizon=astronomy.CAPTURE_HORIZON)
    except (TypeError, ValueError):
        return None
    hours = tw.get("duration_hours")
    if not hours:
        return None
    return int(hours * 3600 / period_s)


def _night_verdict(n_ff: int, expected: int | None) -> str:
    if n_ff <= 0:
        return "none"
    if not expected:
        return "full"
    frac = n_ff / expected
    if frac >= FULL_NIGHT:
        return "full"
    return "partial" if frac >= PARTIAL_NIGHT else "poor"


def camera_capture_state(
    code: str,
    status: dict[str, Any] | None,
    *,
    online: bool,
    now: datetime,
    lat: float | None,
    lon: float | None,
) -> dict[str, Any]:
    """``{state, detail, newest_ff, last_night}`` for one registry camera."""
    status = status or {}
    reported = _parse_ts(status.get("last_updated"))
    sun_alt = sun_altitude_deg(float(lat), float(lon), now) if lat is not None and lon is not None else None
    out: dict[str, Any] = {"state": "unknown", "detail": "", "newest_ff": None, "last_night": None}

    if not online or reported is None or (now - reported).total_seconds() > STALE_REPORT_S:
        out.update(state="offline", detail="station not reporting")
        return out
    blocks = status.get("capture")
    if not isinstance(blocks, dict):
        out["detail"] = "station bundle does not report capture yet"
        return out
    block = blocks.get(code)
    if block is None:
        block = next((v for k, v in blocks.items() if k.upper() == code.upper()), None)
    if not isinstance(block, dict):
        out.update(state="no_rms", detail="no RMS process for this camera")
        return out

    newest = _parse_ts(block.get("newest_ff"))
    out["newest_ff"] = block.get("newest_ff")
    period = float(block.get("ff_period_s") or 10.24)
    nights = block.get("nights") or {}
    night = last_finished_night(now, sun_alt)
    n_ff = int(nights.get(night) or 0)
    expected = expected_ffs(lat, lon, night, period)
    out["last_night"] = {"night": night, "n_ff": n_ff, "expected": expected,
                         "verdict": _night_verdict(n_ff, expected)}

    guard = block.get("guard")
    got, want = block.get("ff_window") or 0, block.get("ff_window_expected") or 0
    window = block.get("window_min") or 30
    if newest is not None and (reported - newest).total_seconds() <= RECORDING_MAX_AGE_S:
        if guard in ("degraded", "lossy"):
            out.update(state="degraded", detail=f"stall guard: {guard}; {got} of {want} FF files in {window} min")
        elif want >= MIN_WINDOW_EXPECTED and got < DEGRADED_YIELD * want:
            out.update(state="degraded", detail=f"{got} of {want} FF files in {window} min")
        else:
            out.update(state="recording", detail=f"{got} of {want} FF files in {window} min")
        return out

    rms_state = block.get("rms_state")
    since = f"last FF {newest:%Y-%m-%d %H:%M} UTC" if newest else "no FF file"
    if rms_state in ("night", "capturing"):
        night_now = True
    elif rms_state in ("day", "ended", "waiting"):
        night_now = guard == "stalled"
    else:
        night_now = guard == "stalled" or (sun_alt is not None and sun_alt < NIGHT_SUN_ALT_DEG)
    state_since = _parse_ts(block.get("rms_state_since"))
    if night_now and rms_state in ("night", "capturing") and state_since is not None \
            and (reported - state_since).total_seconds() <= STARTING_GRACE_S:
        out.update(state="idle", detail="night capture is starting")
    elif night_now:
        out.update(state="stalled", detail=f"RMS is in night capture, but {since}")
    else:
        out.update(state="idle", detail="daytime: RMS records at night")
    return out
