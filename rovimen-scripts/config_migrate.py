#!/usr/bin/env python3
"""config_migrate.py — Merge new default fields into config.json.

Called automatically by updater.sh after each script update.
Adds missing keys from config_defaults.json without touching existing values.
Writes atomically (private temp file → rename) under the config.json writer lock.

Usage:
    python3 config_migrate.py [--dry-run]
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Iterator

# The shared station library, guarded: the updater runs this first after every
# bundle sync, and a config migration must never abort over a missing or
# half-synced sibling. Without it the write stays atomic, only unlocked.
try:
    from rovimen_config import atomic_write_json as _shared_atomic_write
    from rovimen_config import config_lock as _shared_config_lock
except Exception:  # missing, mid-update or unimportable rovimen_config.py
    _shared_atomic_write = None
    _shared_config_lock = None

SCRIPTS_DIR = Path(__file__).parent
CONFIG_PATH = SCRIPTS_DIR / "config.json"
DEFAULTS_PATH = SCRIPTS_DIR / "config_defaults.json"

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

# Clip-overlay geometry is owned by overlay.py's constants. Any copy of these
# keys sitting in a station's config.json shadows the constant and silently voids
# every future change to it -- bar_logo_height_frac = 0.62, merged in from
# config_defaults.json on 2026-08-05, absorbed four fleet-wide logo shrinks that
# way. Purge them on every update so code stays the only source of truth.
#
# The list is imported from overlay.py rather than duplicated here, so adding a
# geometry constant there purges it fleet-wide with no second edit. The literal
# fallback covers a station whose overlay.py is missing or mid-update: a config
# migration must never abort over branding metadata. A test pins the two together.
_CODE_OWNED_OVERLAY_FALLBACK = (
    "bar_logo_height_frac",
    "bar_logo_width_frac",
    "bar_logo_max_height_frac",
    "bar_logo_max_width_frac",
    "text_scale",
    "network_text_scale",
    "gmn_logo_frac",
)

# Overlay keys that reach no render path at all (overlay.INERT_OVERLAY_KEYS).
# Purged for the same reason as the geometry keys, one step further along: these
# were not shadowing a constant, they were pure decoration that read as working
# controls. logo_size sat in every station config and in the dashboard's overlay
# editor as "px (0 = auto)"; the audit on 2026-08-25 found it set to 0, 16 and 25
# across the fleet with no rendered difference between them.
_INERT_OVERLAY_FALLBACK = (
    "logo_size",
    "logo_opacity",
    "watermark_width_frac",
    "watermark_opacity",
)


def _code_owned_overlay_keys() -> list[str]:
    """Overlay keys owned by overlay.py, plus the ones it no longer reads."""
    try:
        if str(SCRIPTS_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPTS_DIR))
        from overlay import (  # noqa: PLC0415
            CODE_OWNED_OVERLAY_KEYS,
            INERT_OVERLAY_KEYS,
        )
        return sorted(set(CODE_OWNED_OVERLAY_KEYS) | set(INERT_OVERLAY_KEYS))
    except Exception:  # missing, mid-update or unimportable overlay.py
        log.warning("overlay.py unreadable — purging the known geometry keys only")
        return sorted(set(_CODE_OWNED_OVERLAY_FALLBACK) | set(_INERT_OVERLAY_FALLBACK))


# Top-level keys that have been removed from the schema and should be purged
# from any config.json that still contains them.
# NOTE: only add keys that are no longer read anywhere in the codebase.
# Keys that still appear as backward-compat fallbacks (e.g. color_capture_path,
# color_retention_days) must NOT be listed here until those fallbacks are removed.
# Nested keys to remove: { parent_key: [child_keys...] }
# Parent block is also removed if it becomes empty after purging.
REMOVED_NESTED_KEYS: dict[str, list[str]] = {
    "capabilities": ["vaapi", "vaapi_driver", "vaapi_device"],
    "overlay": _code_owned_overlay_keys(),
}

REMOVED_KEYS: list[str] = [
    "clip_retention_days",      # superseded by retention.locked_days
    "retention_days",           # old generic retention key, unused
    "clips_output_path",        # old path key, removed
    "output_path",              # old path key, removed
    "color_stack_path",         # old path key, removed
    "color_timelapse_path",     # old path key, removed
    "clips_path",               # old explicit path key, removed
    "stacks_path",              # old explicit path key, removed
    "thumbnails_path",          # old explicit path key, removed
    "timelapse_path",           # old explicit path key, removed
    "fireball_candidate_path",  # old path key, removed
    # gmn0004 old schema (pre-rewrite)
    "buffer_duration_seconds",
    "detection_timeout_seconds",
    "min_stars_for_detection",
    "ml_filtering_enabled",
    "ml_model_path",
    "ml_threshold",
]


def deep_merge(base: dict, defaults: dict, _path: str = "") -> tuple[dict, list[str]]:
    """Return (merged_dict, list_of_added_key_paths).

    Existing values in base always win.  Missing keys are filled from defaults.
    Recurses into nested dicts.  The 'stations' key is intentionally left
    untouched — station entries are station-specific and have no defaults.
    """
    result = base.copy()
    added: list[str] = []

    for key, default_val in defaults.items():
        full_key = f"{_path}.{key}" if _path else key

        if key not in result:
            result[key] = default_val
            added.append(full_key)
        elif isinstance(default_val, dict) and not isinstance(result[key], dict):
            log.warning("Type mismatch at %s: expected dict, got %s — skipping merge",
                        full_key, type(result[key]).__name__)
        elif isinstance(default_val, dict) and isinstance(result[key], dict):
            # Never recurse into the stations block — it is entirely
            # station-specific and defaults contain no station entries.
            if key == "stations":
                continue
            result[key], sub_added = deep_merge(result[key], default_val, full_key)
            added.extend(sub_added)

    return result, added


@contextlib.contextmanager
def _config_lock() -> Iterator[None]:
    """The shared config.json writer lock, or no lock without rovimen_config."""
    if _shared_config_lock is None:
        yield
        return
    with _shared_config_lock(CONFIG_PATH):
        yield


def _write_config(merged: dict) -> None:
    """Replace config.json atomically through a private temp file.

    The old fixed ``config.json.tmp`` was shared with station_api's PATCH and
    update_config_coords, so two writers could truncate each other's temp
    file and install a half-written config.
    """
    if _shared_atomic_write is not None:
        _shared_atomic_write(CONFIG_PATH, merged, indent=4)
        return
    fd, tmp = tempfile.mkstemp(prefix=".config.json.", suffix=".tmp", dir=str(CONFIG_PATH.parent))
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(merged, fh, indent=4)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, CONFIG_PATH.stat().st_mode & 0o7777)
        os.replace(tmp, CONFIG_PATH)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def migrate(dry_run: bool = False) -> int:
    """Merge defaults into config.json.  Returns number of fields added."""
    if not CONFIG_PATH.exists():
        log.info("No config.json found — skipping migration")
        return 0
    if not DEFAULTS_PATH.exists():
        log.info("No config_defaults.json found — skipping migration")
        return 0

    # Read, merge and write under the lock every config.json writer takes, so
    # a PATCH landing mid-migration is neither lost nor overwritten.
    with _config_lock():
        return _migrate_locked(dry_run)


def _migrate_locked(dry_run: bool) -> int:
    config = json.loads(CONFIG_PATH.read_text())
    defaults = json.loads(DEFAULTS_PATH.read_text())

    merged, added = deep_merge(config, defaults)

    purged = [k for k in REMOVED_KEYS if k in merged]
    for k in purged:
        del merged[k]

    purged_nested: list[str] = []
    for parent, children in REMOVED_NESTED_KEYS.items():
        if not isinstance(merged.get(parent), dict):
            continue
        for child in children:
            if child in merged[parent]:
                del merged[parent][child]
                purged_nested.append(f"{parent}.{child}")
        if not merged[parent]:
            del merged[parent]
            purged_nested.append(f"{parent} (empty, removed)")

    if not added and not purged and not purged_nested:
        log.info("config.json is up to date")
        return 0

    if added:
        log.info("config.json: adding %d field(s): %s", len(added), ", ".join(added))
    if purged:
        log.info("config.json: removing %d obsolete field(s): %s", len(purged), ", ".join(purged))
    if purged_nested:
        log.info("config.json: removing %d nested obsolete field(s): %s", len(purged_nested), ", ".join(purged_nested))

    if dry_run:
        log.info("[dry-run] no changes written")
        return len(added)

    _write_config(merged)
    log.info("config.json updated successfully")
    return len(added)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be added without writing",
    )
    args = parser.parse_args()
    sys.exit(0 if migrate(dry_run=args.dry_run) >= 0 else 1)


if __name__ == "__main__":
    main()
