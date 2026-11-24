"""Work out which meteor a colour clip holds, for archive-served nights.

The station records the answer in ``state.json`` as
``chunks[<clip>].lock.meteor_time``. The dashboard reads it back for two
things: keying a clip to its RMS detection (``_lookupDetection`` in
dashboard-rms.js matches ``chunk.meteor_time`` against ``detection.time_utc``
at second precision) and seeking straight to the meteor
(``detection_offset_s``).

That field is not durable. ``dawn_process._sweep_stale_locks`` clears every
``lock`` older than ``stale_lock_days`` (default 7) and archive_upload then
resyncs ``state.json`` to the Storage Box, so the archived copy of any night
older than about a week carries ``lock: null``. Nothing about the clip
changed — ``lock`` only ever recorded whether the clip was pinned on the
station's local disk — but the dashboard lost the pointer to its meteor, and
with it the whole info panel under the clip.

Provenance is therefore resolved here instead, from three sources, most
authoritative first:

1. the archived lock, when it survived;
2. ``detections.db``'s ``chunk_file`` column, written by the station's
   detection push and never touched by the sweep;
3. a time-window match against the night's ``*_radiants.txt``, mirroring the
   station-side matcher in ``overlay_details.match_detections_to_clip`` so the
   dashboard agrees with what the overlay bar burned into the clip.

Tier 3 is what keeps the archive self-describing: a night needs neither a
database row nor a station round-trip to say which meteor is in which clip.

Resolved times are passed through as the source wrote them, never reformatted
— the browser joins on the string, so re-rendering it risks breaking a match
that the source had exactly right.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

# Colour chunk length in seconds (``segment_duration`` in the station config).
# Every station in the fleet runs the 20 s default; a clip that ran long is
# covered by the stitched form, which carries its own end stamp.
DEFAULT_SEGMENT_SECS = 20

# RO0014_20260525_215813_color.mkv, and the boundary-stitched
# RO0014_20260525_221623_221643_color.mkv that detection_lock.py writes when a
# meteor lands near a chunk edge. Both name the clip's own start time first.
# ``.mp4`` matches too: the janitor reaps the heavy ``.mkv`` on aged nights and
# leaves the faststart sibling behind as the only survivor.
_CHUNK_RE = re.compile(
    r"^[A-Z0-9]+_(?P<date>\d{8})_(?P<start>\d{6})(?:_(?P<end>\d{6}))?_color\.(?:mkv|mp4)$"
)


def _parse_iso(value: Any) -> datetime | None:
    """Parse an ISO detection timestamp as UTC, or None if unusable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def chunk_span(
    name: str, segment_secs: int = DEFAULT_SEGMENT_SECS
) -> tuple[datetime, datetime] | None:
    """UTC ``[start, end]`` a clip covers, or None if the name is unparseable.

    A regular clip spans ``[start, start + segment_secs]``; a stitched clip
    spans ``[start, end]`` from its two-timestamp name. The filename's date
    component is the clip's own calendar date, so a post-midnight clip needs no
    rollover guess — only a stitched clip whose end stamp crossed midnight does.

    Deliberately identical to ``overlay_details._clip_span`` on the station,
    including treating a stitched clip's second stamp as the end of the span
    rather than the start of a further segment: that matcher decides what gets
    burned into the clip's overlay bar, and the panel under the clip must name
    the same meteor.
    """
    m = _CHUNK_RE.match(name)
    if not m:
        return None
    try:
        start = datetime.strptime(
            f"{m.group('date')}_{m.group('start')}", "%Y%m%d_%H%M%S"
        ).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    end_stamp = m.group("end")
    if end_stamp:
        try:
            end = datetime.strptime(
                f"{m.group('date')}_{end_stamp}", "%Y%m%d_%H%M%S"
            ).replace(tzinfo=timezone.utc)
            if end <= start:  # end crossed midnight relative to start
                end += timedelta(days=1)
            return start, end
        except ValueError:
            pass
    return start, start + timedelta(seconds=segment_secs)


def match_by_window(
    chunk_names: Iterable[str],
    detection_times: Iterable[str],
    segment_secs: int = DEFAULT_SEGMENT_SECS,
) -> dict[str, str]:
    """Map each clip to the earliest detection time falling inside its span.

    Clips that hold no detection are left out rather than mapped to None, so a
    caller can tell "no meteor here" from "not looked up yet". A clip can hold
    more than one meteor; the earliest is the one the lock would have recorded.
    """
    parsed: list[tuple[datetime, str]] = []
    for raw in detection_times:
        dt = _parse_iso(raw)
        if dt is not None:
            parsed.append((dt, raw))
    if not parsed:
        return {}
    parsed.sort(key=lambda pair: pair[0])

    matched: dict[str, str] = {}
    for name in chunk_names:
        span = chunk_span(name, segment_secs)
        if span is None:
            continue
        start, end = span
        for dt, raw in parsed:
            if dt > end:
                break
            if dt >= start:
                matched[name] = raw
                break
    return matched


def _resolve_source(
    source: Any,
) -> Any:
    """Call a lazily-supplied source, or pass a plain value straight through."""
    return source() if callable(source) else source


def resolve_meteor_times(
    chunk_names: Iterable[str],
    *,
    lock_times: Mapping[str, str | None] | None = None,
    db_times: Mapping[str, str | None] | Callable[[], Mapping[str, str | None]] | None = None,
    detection_times: Iterable[str] | Callable[[], Iterable[str]] | None = None,
    segment_secs: int = DEFAULT_SEGMENT_SECS,
) -> dict[str, str | None]:
    """Resolve a meteor time for every clip, falling through the three tiers.

    ``db_times`` and ``detection_times`` may each be a zero-argument callable;
    it is invoked only if the cheaper tiers left a gap, so a night whose locks
    all survived costs neither a database query nor an SSHFS read.

    Returns an entry for every name in ``chunk_names``, None where no tier
    could place a meteor in that clip.
    """
    names = list(chunk_names)
    resolved: dict[str, str | None] = {
        name: (lock_times or {}).get(name) or None for name in names
    }

    missing = [name for name in names if not resolved[name]]
    if missing and db_times is not None:
        by_chunk = _resolve_source(db_times) or {}
        for name in missing:
            resolved[name] = by_chunk.get(name) or None
        missing = [name for name in missing if not resolved[name]]

    if missing and detection_times is not None:
        windowed = match_by_window(
            missing, _resolve_source(detection_times) or (), segment_secs
        )
        for name in missing:
            resolved[name] = windowed.get(name) or None

    return resolved
