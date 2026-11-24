"""Segment-health alarm policy for the colour-capture cadence signal.

Stations report a per-camera ``segment_health`` block on ``/api/status``
(``rovimen-scripts/station_api.py``): how many colour segments in the trailing
hour were cut back-to-back ("short") and how many holes opened up ("stalls").
The raw counters are deliberately dumb — deciding what counts as *broken* is a
fleet-wide policy call, so it lives here, once, instead of being re-derived in
the overview route, the SSE diff and the frontend.

Motivating incident: a corrupt camera stream makes ffmpeg's segment muxer stop
honouring ``-segment_time`` and cut on nearly every packet. RO000Y wrote 1543
one-second clips in a single hour and RO0010 1330 over three, while services,
disk and heartbeat all stayed green — so nobody saw it until the morning, with
the night already unusable.

This module is pure: no Flask, no I/O, no station access. It is imported both
by ``routes/overview.py`` (to emit the alarm) and by ``station_client.py`` (to
decide when the SSE fan-out should fire).
"""

from __future__ import annotations

from typing import Any

# A healthy camera cuts one segment every ``segment_duration`` seconds (20 s
# fleet-wide), so a clean hour has ~180 segments and exactly zero short ones.
# 50/hour is therefore already far outside normal — it takes a real muxer
# breakdown to reach — while leaving room for the handful of back-to-back cuts
# a capture restart or a brief stream hiccup can produce legitimately.
SEGMENT_SHORT_ALARM_PER_HOUR = 50

# Stalls are rarer and each one costs coverage outright, so the bar is much
# lower. A couple of gaps an hour is a flaky uplink; six is a stream that keeps
# dropping out (RO001E logged 123 in one night before anyone noticed).
SEGMENT_STALL_ALARM_PER_HOUR = 5

# Alarm kinds, in the order the UI should prefer when describing a camera.
ALARM_STORM = "storm"
ALARM_STALLS = "stalls"
ALARM_BOTH = "both"


def _per_hour(count: Any, scale: float) -> float | None:
    """Normalise a windowed count to a per-hour rate, or None if unusable."""
    if count is None or isinstance(count, bool):
        return None
    try:
        return float(count) * scale
    except (TypeError, ValueError):
        return None


def segment_alarm(health: dict[str, Any] | None) -> str | None:
    """Alarm kind for one camera's segment-health block, or None when healthy.

    ``health`` is the block the station reports for a single camera, or None on
    an old station bundle that predates the field. An absent or malformed block
    reads as "nothing to say", never as a fault — the dashboard must not turn a
    stale bundle into a red camera.

    Counters are scaled to a per-hour rate from the block's own
    ``window_min``, so the thresholds stay meaningful if the station's window
    is ever retuned.
    """
    if not isinstance(health, dict):
        return None
    window_min = health.get("window_min") or 60
    try:
        scale = 60.0 / float(window_min)
    except (TypeError, ValueError, ZeroDivisionError):
        scale = 1.0

    short = _per_hour(health.get("short"), scale)
    stalls = _per_hour(health.get("stalls"), scale)
    storming = short is not None and short > SEGMENT_SHORT_ALARM_PER_HOUR
    stalling = stalls is not None and stalls > SEGMENT_STALL_ALARM_PER_HOUR

    if storming and stalling:
        return ALARM_BOTH
    if storming:
        return ALARM_STORM
    if stalling:
        return ALARM_STALLS
    return None


def camera_segment_health(
    segment_health: dict[str, Any] | None, code: str
) -> dict[str, Any] | None:
    """One camera's block, looked up case-insensitively and tagged with ``alarm``.

    Returns None when the station does not report the camera — an old bundle,
    or a camera present in ``dashboard_config.yaml`` but not in the station's
    own ``config.json``.
    """
    if not isinstance(segment_health, dict):
        return None
    block = segment_health.get(code)
    if block is None:
        wanted = str(code).upper()
        for cam, val in segment_health.items():
            if str(cam).upper() == wanted:
                block = val
                break
    if not isinstance(block, dict):
        return None
    tagged = dict(block)
    tagged["alarm"] = segment_alarm(block)
    return tagged


def tag_segment_alarms(status: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return ``status`` with every camera's segment-health block tagged ``alarm``.

    Applied once on the status-cache write path, so the SSE stream,
    ``/api/status/<host>`` and ``/api/status/all`` all carry the same verdict
    and the frontend never has to re-implement a threshold. Shallow copy — the
    caller's dict is left untouched — and a status without the field (an older
    station bundle) comes back unchanged, so this is safe to apply blindly.
    """
    if not isinstance(status, dict):
        return status
    blocks = status.get("segment_health")
    if not isinstance(blocks, dict) or not blocks:
        return status
    tagged: dict[str, Any] = {}
    for cam, block in blocks.items():
        if not isinstance(block, dict):
            tagged[cam] = block
            continue
        enriched = dict(block)
        enriched["alarm"] = segment_alarm(block)
        tagged[cam] = enriched
    out = dict(status)
    out["segment_health"] = tagged
    return out


def station_segment_alarms(
    segment_health: dict[str, Any] | None
) -> dict[str, str]:
    """``{camera code: alarm kind}`` for every camera currently in alarm.

    Empty for a healthy station and for one that does not report the field at
    all, which is what makes it safe as an SSE change-detection projection:
    the raw block churns every poll (the newest-segment timestamp moves, the
    segment count grows), but this reduction only moves when the alarm does.
    """
    if not isinstance(segment_health, dict):
        return {}
    alarms: dict[str, str] = {}
    for cam, block in segment_health.items():
        kind = segment_alarm(block)
        if kind is not None:
            alarms[str(cam)] = kind
    return alarms
