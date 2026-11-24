#!/usr/bin/env python3
"""update_config_coords_from_platepar.py — Update config.json with platepar data.

Reads az_centre / alt_centre per camera and station lat/lon/elev from each
camera's newest platepar (the one RMS loads, rovimen_rms.find_latest_platepar)
and writes them into config.json.

Run after completing platepar calibration for one or more cameras.

Usage:
    python3 update_config_coords_from_platepar.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import rovimen_config
import rovimen_rms

SCRIPTS_DIR = Path(__file__).parent
CONFIG_PATH = SCRIPTS_DIR / "config.json"


def _fmt(value: object) -> str:
    return f"{value:.1f}" if isinstance(value, (int, float)) else "?"


def load_platepars(config: dict) -> dict[str, dict]:
    """Return {camera_code: platepar_dict} for every configured camera with a
    readable platepar."""
    result = {}
    stations = config.get("stations") if isinstance(config.get("stations"), dict) else {}
    for code in sorted(stations):
        platepar_path = rovimen_rms.find_latest_platepar(code, config)
        if platepar_path is None:
            continue
        try:
            data = json.loads(platepar_path.read_text())
        except Exception as e:
            print(f"  WARN: could not read {platepar_path}: {e}")
            continue
        if not isinstance(data, dict):
            print(f"  WARN: {platepar_path} is not a platepar — skipping")
            continue
        result[code] = data
        print(f"  Found platepar: {platepar_path} → {code}"
              f"  (az={_fmt(data.get('az_centre'))}  alt={_fmt(data.get('alt_centre'))})")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change without writing")
    args = parser.parse_args()

    if not CONFIG_PATH.exists():
        print(f"ERROR: {CONFIG_PATH} not found", file=sys.stderr)
        sys.exit(1)

    # Read, change and write under the lock every config.json writer takes
    # (station_api's PATCH, config_migrate), so no update is lost.
    with rovimen_config.config_lock(CONFIG_PATH):
        _update_locked(args.dry_run)


def _update_locked(dry_run: bool) -> None:
    config = json.loads(CONFIG_PATH.read_text())

    print("Scanning for platepars...")
    platepars = load_platepars(config)

    if not platepars:
        print("No platepars found — nothing to update.")
        sys.exit(0)

    changes: list[str] = []

    # Update station-level lat/lon/elev from the first platepar found
    first = next(iter(platepars.values()))
    for field, key in [("latitude", "lat"), ("longitude", "lon"), ("elevation", "elev")]:
        val = first.get(key)
        if val is not None:
            old = config.get(field)
            if old != val:
                changes.append(f"  {field}: {old} → {val}")
                config[field] = val

    # Update az/alt per camera in the stations block
    stations = config.get("stations", {})
    for code, platepar in platepars.items():
        for field, key in [("az", "az_centre"), ("alt", "alt_centre")]:
            val = platepar.get(key)
            if val is not None:
                val = round(val, 1)
                old = stations[code].get(field)
                if old != val:
                    changes.append(f"  stations.{code}.{field}: {old} → {val}")
                    stations[code][field] = val

    if not changes:
        print("config.json already up to date — nothing to change.")
        sys.exit(0)

    print("\nChanges:")
    for c in changes:
        print(c)

    if dry_run:
        print("\n[dry-run] no changes written.")
        sys.exit(0)

    rovimen_config.atomic_write_json(CONFIG_PATH, config, indent=4)
    print(f"\nUpdated {CONFIG_PATH}")


if __name__ == "__main__":
    main()
