# /// script
# requires-python = ">=3.9"
# dependencies = ["tinytuya"]
# ///
"""Switch a Tuya / Smart Life plug on the LAN: on, off or status.

The device file is one tinytuya-style entry (``id``, ``key``, ``ip``,
``version``), e.g. ``~/.config/tuya/fireball_plug.json`` on gmnde01 (Berlin),
written from the full device list kept on the admin workstation. The plug's IP is DHCP, so
when the stored one does not answer the plug is found again by its device id
from its LAN broadcasts, and the file is updated.

  uv run tools/tuya_plug.py ~/.config/tuya/fireball_plug.json on

Used by the fireball signal receiver (``tools/fireball_signal.py --cmd``).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import tinytuya

logger = logging.getLogger("tuya_plug")


def _device(entry: dict, ip: str) -> tinytuya.OutletDevice:
    dev = tinytuya.OutletDevice(entry["id"], ip, entry["key"], version=float(entry.get("version") or 3.3))
    dev.set_socketTimeout(3)
    dev.set_socketRetryLimit(1)
    return dev


def _rediscover(entry: dict) -> str | None:
    found = tinytuya.find_device(dev_id=entry["id"])
    ip = (found or {}).get("ip")
    if ip and found.get("version"):
        entry["version"] = str(found["version"])
    return ip


def run(path: Path, action: str) -> int:
    entry = json.loads(path.read_text())
    for attempt in range(2):
        ip = entry.get("ip") if attempt == 0 else _rediscover(entry)
        if not ip:
            continue
        dev = _device(entry, ip)
        if action == "on":
            result = dev.turn_on()
        elif action == "off":
            result = dev.turn_off()
        else:
            result = dev.status()
        if isinstance(result, dict) and "Error" not in result:
            if ip != entry.get("ip"):
                entry["ip"] = ip
                path.write_text(json.dumps(entry))
                logger.info("plug %s moved to %s", entry["id"], ip)
            print(json.dumps(result.get("dps", result)))
            return 0
        logger.warning("plug %s at %s: %s", entry["id"], ip, result)
    logger.error("plug %s not reachable", entry["id"])
    return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("device_file", type=Path)
    ap.add_argument("action", choices=("on", "off", "status"))
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return run(args.device_file.expanduser(), args.action)


if __name__ == "__main__":
    sys.exit(main())
