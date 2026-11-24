"""`_ensure_sudoers` must write rules for the station user it runs as.

On 2026-09-25 an admin ran the updater on gmnro08 as `sudo -u rmsuser`, which
leaves SUDO_USER set to the admin; the function honoured it and replaced
rmsuser's /etc/sudoers.d/rovimen with rules for the admin account.
"""

from __future__ import annotations

import getpass
import os
import subprocess
from pathlib import Path

import pytest

UPDATER = Path(__file__).resolve().parent.parent / "rovimen-scripts" / "updater.sh"


def _generate(tmp_path: Path, env_extra: dict) -> str:
    out = tmp_path / "candidate"
    script = f'''
set -e
LOCAL_DIR={tmp_path}
log() {{ :; }}
sudo() {{ if [ "$1" = "-n" ] && [ "$2" = "tee" ]; then cat > {out}; return 0; fi; return 1; }}
eval "$(sed -n '/^_ensure_sudoers() {{/,/^}}/p' {UPDATER})"
_ensure_sudoers
'''
    env = {k: v for k, v in os.environ.items() if not k.startswith("SUDO_")}
    env.update(env_extra)
    subprocess.run(["bash", "-c", script], env=env, check=True)
    return out.read_text()


@pytest.mark.skipif(os.geteuid() == 0, reason="the bug is the non-root path")
def test_rules_are_for_the_running_user_not_the_sudo_invoker(tmp_path):
    text = _generate(tmp_path, {"SUDO_USER": "some_admin"})
    me = getpass.getuser()
    assert f"{me} ALL=(root) NOPASSWD:" in text
    assert "some_admin" not in text


@pytest.mark.skipif(os.geteuid() == 0, reason="the bug is the non-root path")
def test_rules_are_for_the_running_user_without_sudo(tmp_path):
    text = _generate(tmp_path, {})
    assert f"{getpass.getuser()} ALL=(root) NOPASSWD:" in text
