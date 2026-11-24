#!/usr/bin/env python3
"""Print the station event history (boots, uplink changes, offline/online).

Usage (on the VPS, as a user that can read the database):
    python3 /opt/rovimen/station_event_history.py                 # every station
    python3 /opt/rovimen/station_event_history.py gmnro09         # one station
    python3 /opt/rovimen/station_event_history.py gmnro09 --since 2026-11-01 --limit 500
    python3 /opt/rovimen/station_event_history.py --json

Newest first. Reads ``ROVIMEN_STATION_EVENTS_DB`` (default
``/opt/rovimen/pushed_state/station_events.db``) read-only; ``--db`` points it
elsewhere, e.g. at a copy fetched to a workstation.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Repo checkout: dashboard/ is a sibling of tools/. VPS deploy: the deploy
# workflow copies this script next to the dashboard modules.
_SCRIPT_DIR = Path(__file__).resolve().parent
for _candidate in (_SCRIPT_DIR, _SCRIPT_DIR.parent / "dashboard"):
    if _candidate.is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

import station_events  # noqa: E402  (sys.path injection above)


def _detail_text(detail: object) -> str:
    if isinstance(detail, dict):
        return str(detail.get("text") or json.dumps(detail, sort_keys=True))
    return "" if detail is None else str(detail)


def format_rows(events: list[dict]) -> list[str]:
    lines = []
    for ev in events:
        when = str(ev.get("ts") or "").replace("T", " ").rstrip("Z")
        lines.append(f"{when}  {ev.get('station', ''):<8}  {ev.get('source', ''):<7}  "
                     f"{ev.get('kind', ''):<12}  {_detail_text(ev.get('detail'))}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print the station event history.")
    parser.add_argument("station", nargs="?", help="host key, e.g. gmnro09; all stations if omitted")
    parser.add_argument("--since", help="ISO instant or date (YYYY-MM-DD), inclusive")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--db", type=Path, help="database path (default: the VPS store)")
    parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    args = parser.parse_args(argv)

    since = station_events.parse_since(args.since)
    if args.since and since is None:
        parser.error(f"--since: not a date or ISO instant: {args.since!r}")
    db = args.db or station_events.db_path()
    if not db.exists():
        print(f"no event history at {db}", file=sys.stderr)
        return 1
    hosts = [args.station] if args.station else None
    events = station_events.list_events(hosts, limit=args.limit, since=since, path=db)
    if args.json:
        print(json.dumps(events, indent=2))
    else:
        for line in format_rows(events):
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
