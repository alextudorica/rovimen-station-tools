"""Shipped systemd units must put each key in the section systemd reads it from.

`rovimen-scripts/color-capture.service` carried `StartLimitIntervalSec` in
`[Service]` until 2026-10-05. systemd ignores it there ("Unknown key ... in
section [Service], ignoring") on every station, so the unit never had the
start limit it declared. This pins the `[Unit]`-only keys to `[Unit]` across
every unit file we ship.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
UNIT_FILES = sorted(
    p for d in ("rovimen-scripts", "deployment/vps")
    for p in (REPO / d).glob("*.service")
)

# Keys systemd only reads in [Unit] (systemd.unit(5)).
UNIT_ONLY_KEYS = {
    "Description", "Documentation", "After", "Before", "Wants", "Requires",
    "Requisite", "BindsTo", "PartOf", "Conflicts", "OnFailure",
    "StartLimitIntervalSec", "StartLimitBurst", "StartLimitAction",
    "ConditionPathExists", "AssertPathExists",
}


def _keys_by_section(path: Path) -> list[tuple[str, str]]:
    section, out = "", []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line
        elif "=" in line:
            out.append((section, line.split("=", 1)[0].strip()))
    return out


def test_there_are_unit_files_to_check() -> None:
    assert any(p.name == "color-capture.service" for p in UNIT_FILES)


@pytest.mark.parametrize("unit", UNIT_FILES, ids=lambda p: p.name)
def test_unit_only_keys_live_in_the_unit_section(unit: Path) -> None:
    misplaced = [f"{key} in {section}" for section, key in _keys_by_section(unit)
                 if key in UNIT_ONLY_KEYS and section != "[Unit]"]
    assert misplaced == [], f"{unit.name}: {misplaced}"
