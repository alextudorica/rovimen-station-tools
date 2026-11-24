#!/usr/bin/env python3
"""config_patch.py — the locked writer for ~/rovimen_scripts/config.json.

Several writers touch a live station's config.json: the loopback
``PATCH /api/settings`` in station_api, ``config_migrate.py`` after every
bundle update and ``update_config_coords_from_platepar.py``. Each used to
rewrite the whole file from its own copy through a fixed ``config.json.tmp``,
so two writers could lose each other's change or install a half-written temp
file, and station_api merged into its 5-second read cache, which kept serving
values that were never saved when the write failed.

Every write here takes an exclusive ``fcntl.flock`` on ``.config.lock`` in the
config's directory, re-reads config.json under that lock, deep-merges the
change into the fresh copy and replaces the file atomically (``mkstemp`` in the
same directory, fsync, ``os.replace``, keeping the old file mode). Other
writers take the same lock file, so the convention is the interlock.

It is also the operator's write channel for a live station's config, run over
SSH (stdlib only, so the system python3 is enough; station_api picks the
change up within its 5 s config cache):

    ssh <station> "python3 ~/rovimen_scripts/config_patch.py '{\\"stacker_max_concurrent\\": 2}'"
    echo '<json>' | ssh <station> python3 ~/rovimen_scripts/config_patch.py -

It prints each key path the patch set. ``--dry-run`` prints them without
writing.
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import stat
import sys
from pathlib import Path
from typing import ContextManager

import rovimen_config

logger = logging.getLogger(__name__)

CONFIG_PATH = Path.home() / 'rovimen_scripts' / 'config.json'
# The lock, merge and atomic write are rovimen_config's, shared with
# config_migrate and update_config_coords, so every writer takes one lock.
LOCK_NAME = rovimen_config.CONFIG_LOCK_NAME
deep_merge = rovimen_config.deep_merge


def config_lock(config_path: Path = CONFIG_PATH) -> ContextManager[None]:
    """Hold the exclusive config lock (``.config.lock`` beside config.json)."""
    return rovimen_config.config_lock(config_path)


def _fsync_dir(directory: Path) -> None:
    """Flush the directory entry so the rename survives a power cut."""
    try:
        dir_fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def write_config_atomic(cfg: dict, config_path: Path = CONFIG_PATH) -> None:
    """Replace config.json with ``cfg`` atomically. Call with the lock held.

    Keeps the old file mode (0644 for a new file)."""
    try:
        mode = stat.S_IMODE(config_path.stat().st_mode)
    except FileNotFoundError:
        mode = 0o644
    rovimen_config.atomic_write_json(config_path, cfg, mode=mode)
    _fsync_dir(config_path.parent)


def patch_config(update: dict, config_path: Path = CONFIG_PATH) -> dict:
    """Deep-merge ``update`` into config.json under the lock.

    Returns the config as written. Raises on a missing or unparseable
    config.json rather than creating one from the patch alone."""
    if not isinstance(update, dict):
        raise TypeError('a config patch must be a JSON object')
    if not config_path.parent.is_dir():
        # rovimen_config.config_lock creates the lock's directory; a mistyped
        # -c path must fail instead of leaving an empty directory behind.
        raise FileNotFoundError(f'no such directory: {config_path.parent}')
    with config_lock(config_path):
        current = json.loads(config_path.read_text())
        if not isinstance(current, dict):
            raise ValueError(f'{config_path} does not hold a JSON object')
        merged = deep_merge(current, copy.deepcopy(update))
        write_config_atomic(merged, config_path)
    return merged


def key_paths(update: dict, prefix: str = '') -> list[str]:
    """Dotted paths of the values a patch sets (nested objects descended)."""
    paths: list[str] = []
    for key, value in update.items():
        path = f'{prefix}{key}'
        if isinstance(value, dict) and value:
            paths.extend(key_paths(value, path + '.'))
        else:
            paths.append(path)
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description='Deep-merge a JSON object into the station config.json under the config lock.')
    parser.add_argument('patch', nargs='?', default='-',
                        help="JSON object to merge; '-' or omitted reads stdin")
    parser.add_argument('-c', '--config', type=Path, default=CONFIG_PATH,
                        help=f'config file (default {CONFIG_PATH})')
    parser.add_argument('--dry-run', action='store_true',
                        help='print the key paths without writing')
    args = parser.parse_args(argv)

    raw = sys.stdin.read() if args.patch == '-' else args.patch
    try:
        update = json.loads(raw)
    except ValueError as exc:
        print(f'config_patch: not valid JSON: {exc}', file=sys.stderr)
        return 2
    if not isinstance(update, dict):
        print('config_patch: the patch must be a JSON object', file=sys.stderr)
        return 2
    if not args.dry_run:
        try:
            patch_config(update, args.config)
        except (OSError, ValueError) as exc:
            print(f'config_patch: {args.config}: {exc}', file=sys.stderr)
            return 1
    for path in key_paths(update):
        print(path)
    return 0


if __name__ == '__main__':
    sys.exit(main())
