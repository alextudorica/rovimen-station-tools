#!/usr/bin/env python3
"""Shift localtime-stamped colour chunk names back onto UTC.

Station-side repair for the failure mode in docs/known_failure_modes.md entry
23: on a host whose system timezone is not UTC, ffmpeg's segment muxer expanded
the ``-strftime`` pattern through ``localtime()`` while the night directory came
from ``datetime.now(timezone.utc)``.  Every chunk in that night therefore carries
a name ``offset`` hours ahead of the sky it contains, so a detection at UTC T
locked and indexed the chunk holding T-offset and its own video was never locked
at all.

This renames every chunk (plus its stacks, thumbnails and lock sidecars) back by
the offset, rekeys ``state.json``, and clears the per-chunk lock/upload flags so
the normal ``detection_lock`` -> reencode -> upload path re-runs against names
the rest of the pipeline can resolve.

Nothing is deleted.  Boundary splices encode two stamps in one name and were
built from the wrong chunks, so they move to ``.tzfix_stale/`` and let
``detection_lock`` rebuild them.

Run it only while capture for the night is finished: a rename under a live
ffmpeg segment muxer would leave state.json flags pointing at bytes that moved.

Dry-run unless --apply.

    python3 tz_fix_chunk_names.py -c ~/rovimen_scripts/config.json \
        --station RO0019 --date 20260726 --offset 3
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path.home() / 'rovimen_scripts'))
import flags_manager  # noqa: E402
import rovimen_config  # noqa: E402

STAMP = '%Y%m%d_%H%M%S'


def _capture_root(cfg: dict) -> Path:
    """The first non-empty key of ``rovimen_config.CAPTURE_ROOT_KEYS``, else
    ``~/color_capture``: the tree every station script writes."""
    return rovimen_config.configured_capture_root(cfg)


def _shift(stamp: str, hours: float) -> str:
    return (datetime.strptime(stamp, STAMP)
            - timedelta(hours=hours)).strftime(STAMP)


def _verify_offset(chunks: list[tuple[str, Path]], hours: float,
                   segment_s: int) -> tuple[bool, str]:
    """Confirm the names really are ``hours`` ahead of the bytes.

    A chunk's mtime is the moment the segment closed, so for a shifted name the
    filename stamp read as UTC lands ``hours`` after mtime, minus one segment.
    Refusing to run when that does not hold is what stops this tool from
    double-shifting a night, or shifting a station that was always UTC.

    Judged on the median and the agreeing fraction, not the worst case: a night
    can carry a truncated chunk that was rewritten hours later, and one such file
    must not veto a night whose other 1400 names agree to the second.
    """
    step = max(1, len(chunks) // 40)
    sample = chunks[::step][:40]
    deltas = []
    for stamp, path in sample:
        named = datetime.strptime(stamp, STAMP).replace(tzinfo=timezone.utc)
        closed = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        # named - hours == segment start; + segment_s == segment close
        expected = named - timedelta(hours=hours) + timedelta(seconds=segment_s)
        deltas.append(abs((closed - expected).total_seconds()))
    deltas.sort()
    median = deltas[len(deltas) // 2]
    agree = sum(1 for x in deltas if x <= 90)
    ok = median <= 90 and agree >= 0.75 * len(deltas)
    return ok, 'median name-vs-mtime disagreement %.0f s, %d of %d samples ' \
                'within 90 s, worst %.0f s' % (median, agree, len(deltas),
                                               deltas[-1])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('-c', '--config', required=True)
    ap.add_argument('--station', required=True, help='camera / station code, e.g. RO0019')
    ap.add_argument('--date', required=True, help='night directory, YYYYMMDD')
    ap.add_argument('--offset', type=float, default=3.0,
                    help='hours the names run ahead of UTC (default 3)')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--force', action='store_true',
                    help='skip the freshly-written and offset-plausibility guards')
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).expanduser().read_text())
    cam, night = args.station, args.date
    d = _capture_root(cfg) / cam / night
    if not d.is_dir():
        print('no such night directory: %s' % d)
        return 1

    segment_s = int(cfg.get('segment_duration') or 20)
    chunk_re = re.compile(r'^%s_(\d{8}_\d{6})_color\.(mkv|mp4)$' % cam)
    splice_re = re.compile(r'^%s_\d{8}_\d{6}_\d{6}_color\.(mkv|mp4)$' % cam)
    stamped_re = re.compile(r'^%s_(\d{8}_\d{6})(_.*|\..*)$' % cam)

    quarantine = d / '.tzfix_stale'
    renames: list[tuple[Path, Path]] = []
    splices: list[Path] = []
    unmatched: list[Path] = []
    chunks: list[tuple[str, Path]] = []

    for p in sorted(d.rglob('*')):
        if p.is_dir() or quarantine in p.parents or p == quarantine:
            continue
        if p.name == 'state.json' or p.name == 'state.json.tmp':
            continue
        if splice_re.match(p.name):
            splices.append(p)
            continue
        m = stamped_re.match(p.name)
        if not m:
            unmatched.append(p)
            continue
        stamp = m.group(1)
        renames.append((p, p.with_name(cam + '_' + _shift(stamp, args.offset)
                                       + m.group(2))))
        # Only raw .mkv segments date the offset check: a reencoded .mp4 was
        # written long after its segment closed.
        if p.suffix == '.mkv' and chunk_re.match(p.name):
            chunks.append((stamp, p))

    if not chunks:
        print('no chunks matching %s_<stamp>_color.mkv in %s' % (cam, d))
        return 1

    ok, detail = _verify_offset(chunks, args.offset, segment_s)
    print('offset check: %s (%s)' % ('PASS' if ok else 'FAIL', detail))
    if not ok and not args.force:
        print('refusing to run: the names do not look %+.1f h ahead of the bytes '
              '(already fixed? wrong offset? use --force to override)' % args.offset)
        return 2

    newest = max(p.stat().st_mtime for _, p in chunks)
    age = datetime.now(timezone.utc).timestamp() - newest
    print('newest chunk written %.0f s ago' % age)
    if age < 300 and not args.force:
        print('refusing to run: capture looks live in this directory')
        return 2

    # Targets are always earlier than sources, so ascending source order frees
    # each slot before it is needed: the chunk that currently owns S-offset has
    # already moved to S-2*offset by the time S arrives.
    renames.sort(key=lambda t: t[0].name)
    sources = {p for p, _ in renames}
    occupied = set(sources)
    collisions = []
    for src, dst in renames:
        occupied.discard(src)
        if dst in occupied or (dst not in sources and dst.exists()):
            collisions.append((src, dst))
        occupied.add(dst)

    print('%d files to shift %+.1f h, %d stale splices to quarantine, '
          '%d unmatched left alone, %d collisions'
          % (len(renames), -args.offset, len(splices), len(unmatched),
             len(collisions)))
    for src, dst in renames[:3] + renames[-3:]:
        print('  %s -> %s' % (src.relative_to(d), dst.relative_to(d)))
    for p in splices:
        print('  quarantine %s' % p.relative_to(d))
    for p in unmatched[:5]:
        print('  leave %s' % p.relative_to(d))
    if collisions:
        print('ABORT: %d unresolvable collisions, first %s -> %s'
              % (len(collisions), collisions[0][0].name, collisions[0][1].name))
        return 3

    if not args.apply:
        print('dry run: nothing written (pass --apply)')
        return 0

    with flags_manager._lock(cam, night, cfg):
        quarantine.mkdir(exist_ok=True)
        # The rename is reversible from the names alone; the flag reset is not,
        # so keep the pre-repair state next to the quarantined splices.
        sp = d / 'state.json'
        if sp.exists():
            (quarantine / 'state.json.pre-tzfix').write_bytes(sp.read_bytes())
        for p in splices:
            p.rename(quarantine / p.name)
        moved = 0
        for src, dst in renames:
            if dst.exists():
                print('ABORT after %d moves: %s appeared' % (moved, dst.name))
                return 3
            src.rename(dst)
            moved += 1

        state = flags_manager.load(cam, night, cfg)
        new_chunks: dict[str, dict] = {}
        dropped = 0
        for name, entry in (state.get('chunks') or {}).items():
            if splice_re.match(name):
                dropped += 1
                continue
            m = chunk_re.match(name)
            if not m:
                new_chunks[name] = entry
                continue
            shifted = cam + '_' + _shift(m.group(1), args.offset) + '_color.mkv'
            e = dict(entry)
            # The lock, the reencoded copy and both uploads were all keyed to the
            # old name, so they have to be re-derived from the corrected one.
            e['lock'] = None
            e['uploaded'] = False
            e['reencoded'] = False
            e['stack_uploaded'] = False
            e['stacked'] = (d / 'stacks' / (shifted[:-len('_color.mkv')]
                                            + '_stack.webp')).exists()
            new_chunks[shifted] = e
        state['chunks'] = new_chunks
        flags_manager.save(state, cam, night, cfg)

    print('applied: %d files renamed, %d splices quarantined, '
          '%d state entries rekeyed, %d splice entries dropped'
          % (moved, len(splices), len(new_chunks), dropped))
    print('next: python3 ~/rovimen_scripts/detection_lock.py -c %s '
          '--station %s --date %s' % (args.config, cam, night))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
