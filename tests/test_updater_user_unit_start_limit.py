"""updater.sh moves the StartLimit keys of a --user color-capture unit into [Unit].

The bundle's `color-capture.service` had `StartLimitBurst`/`StartLimitIntervalSec`
in `[Service]`, where systemd ignores `StartLimitIntervalSec`. The bundle copy is
fixed, and the updater refreshes system units from it, but stations that run
colour as a hand-fitted --user unit (gmnro06 Cluj) are never reconciled: copying
the system template over them would bring `User=` and
`WantedBy=multi-user.target` into a user unit. So the updater edits just those
keys in place. These tests run the real `_fix_user_start_limit` from updater.sh.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UPDATER = REPO / "rovimen-scripts" / "updater.sh"

# gmnro06 (Cluj)'s user unit shape: no User=, default.target, old keys in [Service].
CLUJ_UNIT = """[Unit]
Description=Color video capture for meteor clips
After=network-online.target

[Service]
Type=simple
ExecStart=/home/rmsuser/vRMS/bin/python /home/rmsuser/rovimen_scripts/color_capture.py -c /home/rmsuser/rovimen_scripts/config.json
Nice=5
Restart=on-failure
RestartSec=60
StartLimitBurst=3
StartLimitIntervalSec=300
MemoryMax=2G

[Install]
WantedBy=default.target
"""


def _run(unit: Path) -> int:
    script = (f"eval \"$(sed -n '/^_fix_user_start_limit() {{/,/^}}/p' {UPDATER})\"\n"
              f"_fix_user_start_limit \"{unit}\"")
    return subprocess.run(["bash", "-c", script], timeout=30).returncode


def _section_of(text: str, key: str) -> list[str]:
    section, out = "", []
    for line in text.splitlines():
        if line.startswith("["):
            section = line
        elif line.startswith(key + "="):
            out.append(section)
    return out


def test_cluj_user_unit_gets_its_keys_moved(tmp_path: Path) -> None:
    unit = tmp_path / "color-capture.service"
    unit.write_text(CLUJ_UNIT)
    assert _run(unit) == 0
    text = unit.read_text()
    assert _section_of(text, "StartLimitIntervalSec") == ["[Unit]"]
    assert "StartLimitIntervalSec=0" in text
    assert _section_of(text, "StartLimitBurst") == []
    # Everything else is the operator's file, untouched.
    rest = [line for line in text.splitlines() if not line.startswith("StartLimit")]
    expected = [line for line in CLUJ_UNIT.splitlines() if not line.startswith("StartLimit")]
    assert rest == expected


def test_fixed_unit_is_left_alone(tmp_path: Path) -> None:
    unit = tmp_path / "color-capture.service"
    unit.write_text(CLUJ_UNIT)
    assert _run(unit) == 0
    once = unit.read_text()
    assert _run(unit) == 1          # nothing to do: no reload requested
    assert unit.read_text() == once


def test_no_duplicate_when_unit_section_already_has_the_key(tmp_path: Path) -> None:
    unit = tmp_path / "color-capture.service"
    unit.write_text(CLUJ_UNIT.replace("[Unit]\n", "[Unit]\nStartLimitIntervalSec=0\n", 1))
    assert _run(unit) == 0
    assert _section_of(unit.read_text(), "StartLimitIntervalSec") == ["[Unit]"]


def test_missing_unit_is_not_an_error(tmp_path: Path) -> None:
    assert _run(tmp_path / "color-capture.service") == 1
