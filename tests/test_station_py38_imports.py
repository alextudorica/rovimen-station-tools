"""Every station script must import on Python 3.8, the RMS venvs on gmnro05/08.

#1214 added ``_Sig = tuple[int, int, int]`` to flags_manager. On Python 3.8 that
raises TypeError at import, so on gmnro05 the station API crash-looped and every
pipeline script importing flags_manager died. CI's 3.13 never noticed. This runs
tests/station_py38_import_check.py under a real 3.8 interpreter fetched by uv.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

CHECK = Path(__file__).with_name('station_py38_import_check.py')


def test_station_scripts_import_on_python38() -> None:
    in_ci = bool(os.environ.get('CI'))
    uv = shutil.which('uv')
    if uv is None:
        if in_ci:
            pytest.fail('uv is needed in CI to run the Python 3.8 import check')
        pytest.skip('uv is not installed')
    proc = subprocess.run(
        [uv, 'run', '--python', '3.8', '--no-project', 'python', str(CHECK)],
        capture_output=True, text=True, timeout=600)
    ran = 'station modules import on Python 3.8' in proc.stdout
    if not ran and not in_ci:
        pytest.skip(f'no Python 3.8 interpreter available: {proc.stderr.strip()[-300:]}')
    assert proc.returncode == 0, proc.stdout + proc.stderr
