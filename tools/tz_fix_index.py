#!/usr/bin/env python3
"""Repoint or clear chunk_file for detections indexed against shifted chunk names.

Companion to tools/tz_fix_chunk_names.py.  For nights whose chunks are already
off local disk, the names on the storage box cannot be corrected, so the index is
what gets corrected instead.  A detection at UTC T was matched to the chunk named
T, which holds the sky from T-offset; the chunk that actually contains the meteor
is named T+offset.

Two modes, because only the VPS can see the storage box while the authoritative
copy of a row lives on both the VPS and the station that produced it:

  plan   read a DB plus the storage box, decide per row, write a JSON plan
  apply  replay that plan into any detections DB

Running plan once and applying the same plan everywhere keeps the VPS index and
the station indexes byte-identical, so the push plane cannot re-send a stale
chunk_file and undo the repair.

Where the correctly named stack survives on the box the row is repointed, so the
thumbnail shows the meteor that was really recorded (the clip itself was never
locked, so the map shows the pending marker).  Where it does not, chunk_file is
cleared: no video is a true statement, a video of a different patch of sky three
hours earlier is not.

Dry-run unless --apply.

    # on the VPS
    python3 tz_fix_index.py plan --db /opt/rovimen/detections.db \
        --media-root /srv/rovimen/archive --cams RO0015,RO0016,RO0018,RO0019 \
        --offset 3 --until 20260726 --out /tmp/tz_index_plan.json
    python3 tz_fix_index.py apply --db /opt/rovimen/detections.db \
        --plan /tmp/tz_index_plan.json --apply
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

STAMP = '%Y%m%d_%H%M%S'
SUFFIX = '_color.mkv'


def _stem_stamp(chunk: str, cam: str) -> str | None:
    """Extract the YYYYMMDD_HHMMSS stamp from <cam>_<stamp>_color.mkv."""
    if not chunk or not chunk.startswith(cam + '_') or not chunk.endswith(SUFFIX):
        return None
    stamp = chunk[len(cam) + 1:-len(SUFFIX)]
    try:
        datetime.strptime(stamp, STAMP)
    except ValueError:
        return None
    return stamp


def _shift(stamp: str, hours: float) -> str:
    return (datetime.strptime(stamp, STAMP)
            + timedelta(hours=hours)).strftime(STAMP)


def _listing(root: Path, cam: str, night: str) -> set[str]:
    """Stamps of every stack the box holds for this cam-night."""
    d = root / cam / night / 'stacks'
    out = set()
    try:
        names = [p.name for p in d.iterdir()]
    except OSError:
        return out
    for n in names:
        if n.startswith(cam + '_') and n.endswith('_stack.webp'):
            out.add(n[len(cam) + 1:-len('_stack.webp')])
    return out


def cmd_plan(args: argparse.Namespace) -> int:
    cams = [c.strip() for c in args.cams.split(',') if c.strip()]
    root = Path(args.media_root)
    con = sqlite3.connect('file:%s?mode=ro' % args.db, uri=True)
    con.row_factory = sqlite3.Row

    rows = list(con.execute(
        'SELECT cam,date,ff_file,meteor_no,time_utc,chunk_file FROM detections '
        'WHERE cam IN (%s) AND date < ? ORDER BY cam,date'
        % ','.join('?' * len(cams)), (*cams, args.until)))

    groups: dict[tuple[str, str], set[str]] = {}
    plan: list[dict] = []
    counts = {'repoint': 0, 'clear': 0, 'already_null_kept': 0, 'skip': 0}
    for r in rows:
        key = (r['cam'], r['date'])
        if key not in groups:
            groups[key] = _listing(root, *key)
        stacks = groups[key]

        old = r['chunk_file']
        stamp = _stem_stamp(old, r['cam']) if old else None
        if stamp is not None:
            # The matched chunk's name is a real segment boundary, so the chunk
            # holding this meteor is exactly one offset later.
            true_stamp = _shift(stamp, args.offset)
        else:
            # No match at index time (the detection preceded the night's first
            # shifted name), so the boundary has to come from the box listing.
            try:
                t = datetime.strptime(r['time_utc'][:19], '%Y-%m-%dT%H:%M:%S')
            except (TypeError, ValueError):
                counts['skip'] += 1
                continue
            want = t + timedelta(hours=args.offset)
            true_stamp = None
            for back in range(0, args.segment + 1):
                cand = (want - timedelta(seconds=back)).strftime(STAMP)
                if cand in stacks:
                    true_stamp = cand
                    break

        new = None
        if true_stamp is not None and true_stamp in stacks:
            new = '%s_%s%s' % (r['cam'], true_stamp, SUFFIX)
            counts['repoint'] += 1
        elif old is None:
            counts['already_null_kept'] += 1
            continue
        else:
            counts['clear'] += 1

        plan.append({'cam': r['cam'], 'date': r['date'], 'ff_file': r['ff_file'],
                     'meteor_no': r['meteor_no'], 'old': old, 'new': new})

    payload = {'offset_hours': args.offset, 'until': args.until, 'cams': cams,
               'rows_examined': len(rows), 'counts': counts, 'updates': plan}
    Path(args.out).write_text(json.dumps(payload, indent=1))
    print('examined %d rows across %d cam-nights' % (len(rows), len(groups)))
    for k in ('repoint', 'clear', 'already_null_kept', 'skip'):
        print('  %-18s %d' % (k, counts[k]))
    print('plan written to %s (%d updates)' % (args.out, len(plan)))
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    payload = json.loads(Path(args.plan).read_text())
    updates = payload['updates']
    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row

    matched = agree = differ = missing = 0
    todo = []
    for u in updates:
        key = (u['cam'], u['date'], u['ff_file'], u['meteor_no'])
        cur = con.execute(
            'SELECT chunk_file FROM detections '
            'WHERE cam=? AND date=? AND ff_file=? AND meteor_no=?', key).fetchone()
        if cur is None:
            missing += 1
            continue
        matched += 1
        if cur['chunk_file'] == u['new']:
            agree += 1
            continue
        if cur['chunk_file'] != u['old']:
            differ += 1
        todo.append((u['new'], *key))

    print('%s: %d of %d plan rows present, %d already correct, '
          '%d disagree with the plan\'s old value, %d absent, %d to write'
          % (args.db, matched, len(updates), agree, differ, missing, len(todo)))
    if not args.apply:
        for new, *key in todo[:5]:
            print('  %s -> %s' % ('/'.join(str(k) for k in key), new))
        print('dry run: nothing written (pass --apply)')
        return 0

    con.executemany(
        'UPDATE detections SET chunk_file=? '
        'WHERE cam=? AND date=? AND ff_file=? AND meteor_no=?', todo)
    con.commit()
    print('wrote %d rows' % len(todo))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('plan')
    p.add_argument('--db', required=True)
    p.add_argument('--media-root', required=True)
    p.add_argument('--cams', required=True, help='comma-separated camera codes')
    p.add_argument('--offset', type=float, default=3.0,
                   help='hours the chunk names ran ahead of UTC (default 3)')
    p.add_argument('--until', required=True,
                   help='only nights strictly before this YYYYMMDD')
    p.add_argument('--segment', type=int, default=20, help='segment seconds')
    p.add_argument('--out', required=True)
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser('apply')
    p.add_argument('--db', required=True)
    p.add_argument('--plan', required=True)
    p.add_argument('--apply', action='store_true')
    p.set_defaults(func=cmd_apply)

    args = ap.parse_args()
    return args.func(args)


if __name__ == '__main__':
    raise SystemExit(main())
