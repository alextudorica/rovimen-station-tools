#!/usr/bin/env python3
"""Station-side diagnosis for detections whose chunk_file never got resolved.

Runs ON a station. Answers, per cam/night since a cutoff:
  - how many rows the station's own index has with chunk_file NULL/empty
  - whether the capture dir for that night still exists locally, and how many
    raw chunks / boundary splices it holds
  - for each missing row, whether a chunk covering time_utc is present NOW
    (so we can separate "chunk was never written" from "chunk aged out")
  - the local chunk coverage gaps around the missing detections

Read-only. Prints TSV-ish blocks.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

SINCE = sys.argv[1] if len(sys.argv) > 1 else "20260720"
HOME = Path.home()
sys.path.insert(0, str(HOME / "rovimen_scripts"))
import rovimen_config  # noqa: E402
DB = HOME / "rovimen_scripts" / "detections_index.db"
CFG = HOME / "rovimen_scripts" / "config.json"

CHUNK_RE = re.compile(r"^([A-Z0-9]+)_(\d{8})_(\d{6})_color\.mkv$")
STITCH_RE = re.compile(r"^([A-Z0-9]+)_(\d{8})_(\d{6})_(\d{6})_color\.mkv$")
CHUNK_S = 20
SPAN_MAX = 600


def cfg_load():
    try:
        with open(str(CFG)) as fh:
            return json.load(fh)
    except Exception as exc:
        print("CONFIG READ FAILED: %s" % exc)
        return {}


def capture_base(cfg):
    return rovimen_config.configured_capture_root(cfg)


def chunk_dt(ds, ts):
    try:
        return datetime(
            int(ds[:4]), int(ds[4:6]), int(ds[6:8]),
            int(ts[:2]), int(ts[2:4]), int(ts[4:6]),
        )
    except ValueError:
        return None


def build_index(base, cam, night):
    d = base / cam / night
    if not d.exists():
        return None, 0, 0
    idx = []
    raw = spl = 0
    for f in d.glob("*_color.mkv"):
        sm = STITCH_RE.match(f.name)
        if sm:
            s = chunk_dt(sm.group(2), sm.group(3))
            e = chunk_dt(sm.group(2), sm.group(4))
            if s is None or e is None:
                continue
            if e < s:
                e += timedelta(days=1)
            if not 0 < (e - s).total_seconds() <= SPAN_MAX:
                continue
            spl += 1
        else:
            m = CHUNK_RE.match(f.name)
            if not m:
                continue
            s = chunk_dt(m.group(2), m.group(3))
            if s is None:
                continue
            e = s + timedelta(seconds=CHUNK_S)
            raw += 1
        idx.append((s, e, f.name))
    idx.sort(key=lambda x: x[0])
    return idx, raw, spl


def covers(idx, t):
    for s, e, n in idx:
        if s <= t < e:
            return n
    return None


def main():
    cfg = cfg_load()
    base = capture_base(cfg)
    print("HOST=%s" % os.uname()[1])
    print("capture_base=%s exists=%s" % (base, base.exists()))
    for k in rovimen_config.CAPTURE_ROOT_KEYS:
        if cfg.get(k):
            print("  cfg.%s=%s" % (k, cfg[k]))
    ret = cfg.get("retention") or {}
    print("retention=%s" % json.dumps(ret, sort_keys=True))
    print("archive.enabled=%s" % ((cfg.get("archive") or {}).get("enabled")))
    print("db=%s exists=%s" % (DB, DB.exists()))
    if not DB.exists():
        return

    con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    con.row_factory = sqlite3.Row
    rows = list(con.execute(
        "SELECT cam, date, time_utc, ff_file, chunk_file FROM detections "
        "WHERE date >= ? ORDER BY cam, date, time_utc", (SINCE,)))
    print("\n=== per cam/night: index rows, missing chunk_file, local chunks ===")
    print("cam\tnight\trows\tmiss\tdir\traw\tsplice\tmiss_now_covered")
    groups = {}
    for r in rows:
        groups.setdefault((r["cam"], r["date"]), []).append(r)
    for (cam, night) in sorted(groups):
        g = groups[(cam, night)]
        miss = [r for r in g if not r["chunk_file"]]
        idx, raw, spl = build_index(base, cam, night)
        if idx is None:
            print("%s\t%s\t%d\t%d\tABSENT\t-\t-\t-" % (cam, night, len(g), len(miss)))
            continue
        cov = 0
        for r in miss:
            try:
                t = datetime.strptime(r["time_utc"][:19], "%Y-%m-%dT%H:%M:%S")
            except Exception:
                continue
            if covers(idx, t):
                cov += 1
        print("%s\t%s\t%d\t%d\tok\t%d\t%d\t%d"
              % (cam, night, len(g), len(miss), raw, spl, cov))

    print("\n=== sample missing rows with nearest local chunks ===")
    shown = 0
    for (cam, night) in sorted(groups):
        idx, _, _ = build_index(base, cam, night)
        if not idx:
            continue
        for r in groups[(cam, night)]:
            if r["chunk_file"] or shown >= 25:
                continue
            try:
                t = datetime.strptime(r["time_utc"][:19], "%Y-%m-%dT%H:%M:%S")
            except Exception:
                continue
            before = [n for s, e, n in idx if s <= t]
            after = [n for s, e, n in idx if s > t]
            print("%s %s %s  prev=%s  next=%s  covered=%s"
                  % (cam, night, r["time_utc"],
                     before[-1] if before else "-",
                     after[0] if after else "-",
                     covers(idx, t)))
            shown += 1

    print("\n=== indexed_sessions (when each night was last indexed) ===")
    try:
        for r in con.execute(
                "SELECT session_path, rows, indexed_at FROM indexed_sessions "
                "ORDER BY indexed_at DESC LIMIT 25"):
            print("%s\trows=%s\tat=%s" % (
                r["session_path"], r["rows"],
                datetime.utcfromtimestamp(r["indexed_at"]).isoformat()
                if r["indexed_at"] else "-"))
    except Exception as exc:
        print("indexed_sessions query failed: %s" % exc)


main()
