"""detection-indexer must run under the station's RMS venv interpreter.

gmnro08 (Brașov) has never had an FF thumbnail: the indexer ran the system
Python 3.8.10 while its RMS venv is 3.12.9, and the venv's numpy cannot load
in 3.8. The bundle unit now pins the venv interpreter, which the updater
localises the way dawn resolves it (RMS's own unit first, then the standard
venv locations), and falls back to system python3 where no venv exists, so
the indexer, which the pusher depends on, still installs everywhere.

These tests run the updater's real `_detect_venv_python` and `_localise_unit`
against a fake home.
"""

from __future__ import annotations

import glob
import os
import re
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "rovimen-scripts"
UPDATER = SCRIPTS / "updater.sh"

pytestmark = pytest.mark.skipif(
    bool(glob.glob("/etc/systemd/system/rms-cam*.service")),
    reason="host RMS units would take part in venv resolution",
)


def _fake_python(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def _localise(home: Path, unit: str, template: str | None = None) -> str:
    """Localise a bundle unit in a copy under the fake home; return ExecStart."""
    src = home / "rovimen_scripts" / unit
    src.parent.mkdir(parents=True, exist_ok=True)
    if template is not None:
        src.write_text(template)
    elif not src.exists():
        src.write_text((SCRIPTS / unit).read_text())
    fallback = re.search(r"^SYSTEM_PYTHON_FALLBACK_UNITS=.*$", UPDATER.read_text(), re.M).group(0)
    script = f'''
set -e
log() {{ :; }}
svc_user=station
{fallback}
eval "$(sed -n '/^_is_in_list() {{/p' {UPDATER})"
eval "$(sed -n '/^_detect_venv_python() {{/,/^}}/p' {UPDATER})"
eval "$(sed -n '/^_localise_unit() {{/,/^}}/p' {UPDATER})"
_localise_unit "{src}" "{home}/no-installed-unit"
'''
    subprocess.run(["bash", "-c", script], env={**os.environ, "HOME": str(home)}, check=True)
    return re.search(r"^ExecStart=(.*)$", src.read_text(), re.M).group(1)


def test_indexer_runs_under_the_venv(tmp_path: Path) -> None:
    py = _fake_python(tmp_path / "vRMS" / "bin" / "python")
    assert _localise(tmp_path, "detection-indexer.service") == \
        f"{py} {tmp_path}/rovimen_scripts/detection_indexer.py"


def test_rms_unit_interpreter_wins_over_the_probe_order(tmp_path: Path) -> None:
    """gmnro09 (Sibiciu): RMS runs ~/RMS/venv while ~/vRMS also exists and is probed first."""
    _fake_python(tmp_path / "vRMS" / "bin" / "python")
    rms_py = _fake_python(tmp_path / "RMS" / "venv" / "bin" / "python")
    unit = tmp_path / ".config" / "systemd" / "user" / "rms-cam1.service"
    unit.parent.mkdir(parents=True)
    unit.write_text(f"[Service]\nExecStart={rms_py} -m RMS.StartCapture\n")
    assert _localise(tmp_path, "detection-indexer.service").startswith(f"{rms_py} ")


def test_no_venv_falls_back_to_system_python(tmp_path: Path) -> None:
    assert _localise(tmp_path, "detection-indexer.service") == \
        f"/usr/bin/env python3 {tmp_path}/rovimen_scripts/detection_indexer.py"


def test_fallback_is_repinned_once_the_venv_exists(tmp_path: Path) -> None:
    """Units are localised in place, so a run without a venv must not freeze system python."""
    _localise(tmp_path, "detection-indexer.service")
    py = _fake_python(tmp_path / "vRMS" / "bin" / "python")
    assert _localise(tmp_path, "detection-indexer.service").startswith(f"{py} ")


def test_pusher_stays_on_system_python(tmp_path: Path) -> None:
    _fake_python(tmp_path / "vRMS" / "bin" / "python")
    assert _localise(tmp_path, "rovimen-pusher.service").startswith("/usr/bin/env python3 ")
