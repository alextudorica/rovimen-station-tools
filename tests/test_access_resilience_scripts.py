"""setup_revtunnel.sh and setup_rescue_wifi.sh (#1432), run under fake system
binaries on PATH that log their argv, so the tests can check what would be
installed and that the rescue password never reaches a command line."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "rovimen-scripts"
PSK = "rescue-pass-1234"


def _fake(bin_dir: Path, name: str, body: str) -> None:
    p = bin_dir / name
    p.write_text("#!/usr/bin/env bash\n" + f'echo "{name} $*" >> "$FAKE_LOG"\n' + body + "\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def env(tmp_path: Path) -> dict:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "argv.log"
    log.touch()
    _fake(bin_dir, "sudo", '[ "$1" = -n ] && shift\n[ -n "${FAKE_NO_SUDO:-}" ] && exit 1\nexec "$@"')
    _fake(bin_dir, "systemctl", 'case "$*" in *is-active*NetworkManager*) [ -n "${FAKE_NM:-}" ]; exit $? ;; *is-active*) echo active ;; esac')
    _fake(bin_dir, "loginctl", 'echo "${FAKE_LINGER:-yes}"')
    _fake(bin_dir, "nmcli", 'case "$*" in *"device status"*) '
          'printf "%s\\n" "eno1:connected" "wlp0s20f3:${FAKE_NM_WIFI_STATE:-disconnected}" ;; esac')
    _fake(bin_dir, "netplan", 'exit "${FAKE_NETPLAN_RC:-0}"')
    _fake(bin_dir, "wpa_supplicant", "exit 0")
    _fake(bin_dir, "visudo", 'exit "${FAKE_VISUDO_RC:-0}"')
    # uutils install (Ubuntu 26.04) cannot overwrite an existing file from
    # /dev/stdin, which a re-run of setup_rescue_wifi.sh does; GNU install
    # (the stations) can. Replace the target first so re-runs test the script.
    real_install = shutil.which("install") or "/usr/bin/install"
    _fake(bin_dir, "install", 'for a; do last="$a"; done\n'
          'case " $* " in *" /dev/stdin "*) rm -f "$last" ;; esac\n'
          f'exec {real_install} "$@"')
    _fake(bin_dir, "ip", 'echo "default via 192.168.1.1 dev ${FAKE_DEFAULT_DEV:-eno1} proto dhcp metric 100"')
    net = tmp_path / "net"
    (net / "eno1").mkdir(parents=True)
    (net / "wlp0s20f3" / "wireless").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    return {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(home),
        "FAKE_LOG": str(log),
        "ROVIMEN_SYSTEMD_DIR": str(tmp_path / "systemd"),
        "ROVIMEN_USER_SYSTEMD_DIR": str(home / ".config/systemd/user"),
        "ROVIMEN_ETC": str(tmp_path / "etc"),
        "ROVIMEN_SYS_CLASS_NET": str(net),
        "ROVIMEN_SBIN": str(tmp_path / "sbin"),
        "_tmp": str(tmp_path),
    }


def _run(script: str, env: dict, args: list[str], stdin: str = "", **extra: str):
    e = {k: v for k, v in env.items() if not k.startswith("_")}
    e.update(extra)
    (Path(env["_tmp"]) / "systemd").mkdir(exist_ok=True)
    return subprocess.run(["bash", str(SCRIPTS / script), *args], input=stdin,
                          capture_output=True, text=True, env=e, timeout=30)


# ── reverse tunnel ──────────────────────────────────────────────────────


def test_tunnel_installs_a_system_unit_and_a_port_pinned_vps_line(env):
    r = _run("setup_revtunnel.sh", env, ["--port", "22204", "--vps", "203.0.113.7",
                                         "--host-key", "ssh-ed25519 AAAAvpskey"])
    assert "REVTUNNEL: PASS mode=system port=22204" in r.stdout, r.stdout + r.stderr
    unit = (Path(env["ROVIMEN_SYSTEMD_DIR"]) / "rovimen-revtunnel.service").read_text()
    assert "-R 127.0.0.1:22204:localhost:22 revssh@203.0.113.7" in unit
    assert "StrictHostKeyChecking=yes" in unit and "StartLimitIntervalSec=0" in unit
    assert "Restart=always" in unit and "User=" in unit
    known = Path(env["HOME"]) / ".ssh/revtunnel_known_hosts"
    assert known.read_text() == "203.0.113.7 ssh-ed25519 AAAAvpskey\n"
    assert ('restrict,port-forwarding,permitlisten="127.0.0.1:22204",'
            'command="/bin/false" ssh-ed25519 ') in r.stdout


# Options of sshd(8) AUTHORIZED_KEYS FILE FORMAT, with the value shape each
# takes (None: a flag without a value).
_HOSTPORT = re.compile(r"^(?:[^:\s]+:)?(?:\d{1,5}|\*)$")
_KEY_OPTIONS: dict[str, re.Pattern[str] | None] = {
    "agent-forwarding": None, "cert-authority": None, "no-agent-forwarding": None,
    "no-port-forwarding": None, "no-pty": None, "no-user-rc": None,
    "no-X11-forwarding": None, "port-forwarding": None, "pty": None,
    "no-touch-required": None, "verify-required": None, "restrict": None,
    "user-rc": None, "X11-forwarding": None,
    "command": re.compile(r".+"), "environment": re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$"),
    "expiry-time": re.compile(r"^\d{8}(?:\d{4}(?:\d{2})?)?Z?$"),
    "from": re.compile(r".+"), "principals": re.compile(r".+"), "tunnel": re.compile(r"^\d+$"),
    "permitlisten": _HOSTPORT, "permitopen": _HOSTPORT,
}


def _split_options(opts: str) -> list[str]:
    out, cur, quoted = [], "", False
    for ch in opts:
        if ch == '"':
            quoted = not quoted
        if ch == "," and not quoted:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return out


def test_tunnel_vps_line_is_a_valid_authorized_keys_entry(env, tmp_path):
    """permitopen="none" made sshd drop all 18 tunnel keys on 2026-11-22."""
    r = _run("setup_revtunnel.sh", env, ["--port", "22211", "--vps", "203.0.113.7", "--dry-run"])
    line = next(l for l in r.stdout.splitlines() if l.startswith("restrict,"))
    assert "permitopen" not in line
    opts, keytype, rest = line.split(" ", 2)
    blob = rest.split(" ", 1)[0]
    # the key itself must parse
    pub = tmp_path / "k.pub"
    pub.write_text(f"{keytype} {blob}\n")
    assert subprocess.run(["ssh-keygen", "-lf", str(pub)], capture_output=True).returncode == 0
    for opt in _split_options(opts):
        name, _, value = opt.partition("=")
        assert name in _KEY_OPTIONS, f"unknown authorized_keys option {name}"
        shape = _KEY_OPTIONS[name]
        if shape is None:
            assert not value, f"{name} takes no value"
            continue
        assert value.startswith('"') and value.endswith('"'), f"{name} value must be quoted"
        assert shape.match(value[1:-1]), f"{name}={value} has the wrong shape"
    assert 'permitlisten="127.0.0.1:22211"' in line and 'command="/bin/false"' in line


def test_tunnel_rerun_keeps_the_key_and_does_not_restart(env):
    args = ["--port", "22204", "--vps", "203.0.113.7"]
    _run("setup_revtunnel.sh", env, args)
    key = (Path(env["HOME"]) / ".ssh/revtunnel_ed25519.pub").read_text()
    r = _run("setup_revtunnel.sh", env, args)
    assert "changed=0" in r.stdout
    assert (Path(env["HOME"]) / ".ssh/revtunnel_ed25519.pub").read_text() == key
    restarts = [l for l in Path(env["FAKE_LOG"]).read_text().splitlines()
                if l.startswith("systemctl restart rovimen-revtunnel")]
    assert len(restarts) == 1


def test_tunnel_without_sudo_is_a_user_unit_and_needs_linger(env):
    args = ["--port", "22204", "--vps", "203.0.113.7"]
    r = _run("setup_revtunnel.sh", env, args, FAKE_NO_SUDO="1", FAKE_LINGER="no")
    assert "needs linger" in r.stdout and r.returncode == 1
    r = _run("setup_revtunnel.sh", env, args, FAKE_NO_SUDO="1")
    assert "REVTUNNEL: PASS mode=user" in r.stdout, r.stdout
    unit = (Path(env["ROVIMEN_USER_SYSTEMD_DIR"]) / "rovimen-revtunnel.service").read_text()
    assert "User=" not in unit and "WantedBy=default.target" in unit


@pytest.mark.parametrize("args", [["--vps", "h"], ["--port", "80", "--vps", "h"],
                                  ["--port", "22204"], ["--port", "x", "--vps", "h"]])
def test_tunnel_rejects_bad_arguments(env, args):
    r = _run("setup_revtunnel.sh", env, args)
    assert r.returncode == 2 and "REVTUNNEL: FAIL" in r.stdout


# ── rescue Wi-Fi ────────────────────────────────────────────────────────


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def _etc(env: dict) -> Path:
    return Path(env["ROVIMEN_ETC"])


def test_netplan_profile_is_staged_off_and_root_only(env):
    r = _run("setup_rescue_wifi.sh", env, ["--user", "gmn"], stdin=PSK + "\n")
    assert "RESCUE-WIFI: PASS backend=netplan iface=wlp0s20f3 ssid=ROVIMEN-RESCUE user=gmn off until the cable fails" in r.stdout, r.stdout + r.stderr
    staged = _etc(env) / "rovimen/rescue-wifi/90-rovimen-rescue-wifi.yaml"
    assert _mode(staged) == 0o600
    text = staged.read_text()
    assert "route-metric: 600" in text and "regulatory-domain: RO" in text and f'password: "{PSK}"' in text
    assert not (_etc(env) / "netplan/90-rovimen-rescue-wifi.yaml").exists(), "must not be live until the cable fails"
    log = Path(env["FAKE_LOG"]).read_text()
    assert "netplan generate --root-dir" in log and "netplan apply" not in log
    assert PSK not in log, "the password reached a command line"


def test_install_adds_the_helper_and_an_exact_sudoers_rule(env):
    _run("setup_rescue_wifi.sh", env, ["--user", "rmsuser"], stdin=PSK + "\n")
    helper = Path(env["ROVIMEN_SBIN"]) / "rovimen-rescue-wifi"
    assert _mode(helper) == 0o755
    rule = (_etc(env) / "sudoers.d/rovimen-rescue-wifi").read_text().strip()
    assert rule == f"rmsuser ALL=(root) NOPASSWD: {helper} up, {helper} down, {helper} status"
    assert _mode(_etc(env) / "sudoers.d/rovimen-rescue-wifi") == 0o440
    assert (_etc(env) / "rovimen/rescue-wifi/conf").read_text() == "backend=netplan\niface=wlp0s20f3\nssid=ROVIMEN-RESCUE\n"


def test_sudoers_user_defaults_to_the_watchdog_units_user(env):
    unit = _etc(env) / "systemd/system/rovimen-watchdog.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("[Service]\nUser=rmsuser\n")
    _run("setup_rescue_wifi.sh", env, [], stdin=PSK + "\n")
    assert (_etc(env) / "sudoers.d/rovimen-rescue-wifi").read_text().startswith("rmsuser ALL=(root)")


def test_a_rejected_netplan_profile_is_removed(env):
    r = _run("setup_rescue_wifi.sh", env, [], stdin=PSK + "\n", FAKE_NETPLAN_RC="1")
    assert r.returncode == 1 and "removed it" in r.stdout
    assert not (_etc(env) / "rovimen/rescue-wifi/90-rovimen-rescue-wifi.yaml").exists()


def test_a_rejected_sudoers_rule_is_removed(env):
    r = _run("setup_rescue_wifi.sh", env, [], stdin=PSK + "\n", FAKE_VISUDO_RC="1")
    assert r.returncode == 1 and not (_etc(env) / "sudoers.d/rovimen-rescue-wifi").exists()


def test_networkmanager_keyfile_is_off(env):
    r = _run("setup_rescue_wifi.sh", env, [], stdin=PSK + "\n", FAKE_NM="1")
    assert "RESCUE-WIFI: PASS backend=nm" in r.stdout, r.stdout + r.stderr
    kf = _etc(env) / "NetworkManager/system-connections/ROVIMEN-RESCUE.nmconnection"
    text = kf.read_text()
    assert _mode(kf) == 0o600
    for line in ("autoconnect=false", "route-metric=600", f"psk={PSK}", "interface-name=wlp0s20f3"):
        assert line in text
    log = Path(env["FAKE_LOG"]).read_text()
    assert "nmcli connection reload" in log and "connection up" not in log and PSK not in log


def test_nm_active_but_wifi_unmanaged_uses_netplan(env):
    """gmnro10 (Roit): NetworkManager runs, netplan/networkd owns every device."""
    r = _run("setup_rescue_wifi.sh", env, [], stdin=PSK + "\n", FAKE_NM="1", FAKE_NM_WIFI_STATE="unmanaged")
    assert "RESCUE-WIFI: PASS backend=netplan" in r.stdout, r.stdout + r.stderr
    assert (_etc(env) / "rovimen/rescue-wifi/90-rovimen-rescue-wifi.yaml").exists()
    assert not (_etc(env) / "NetworkManager/system-connections/ROVIMEN-RESCUE.nmconnection").exists()


def test_nm_inactive_uses_netplan_without_asking_nmcli(env):
    r = _run("setup_rescue_wifi.sh", env, [], stdin=PSK + "\n")
    assert "RESCUE-WIFI: PASS backend=netplan" in r.stdout
    assert "nmcli" not in Path(env["FAKE_LOG"]).read_text()


def test_switch_to_netplan_removes_the_old_nm_keyfile(env):
    _run("setup_rescue_wifi.sh", env, [], stdin=PSK + "\n", FAKE_NM="1")
    kf = _etc(env) / "NetworkManager/system-connections/ROVIMEN-RESCUE.nmconnection"
    assert kf.exists()
    Path(env["FAKE_LOG"]).write_text("")
    r = _run("setup_rescue_wifi.sh", env, [], stdin=PSK + "\n", FAKE_NM="1", FAKE_NM_WIFI_STATE="unmanaged")
    assert "RESCUE-WIFI: PASS backend=netplan" in r.stdout, r.stdout + r.stderr
    assert not kf.exists()
    assert "nmcli connection reload" in Path(env["FAKE_LOG"]).read_text()
    assert "backend=netplan" in (_etc(env) / "rovimen/rescue-wifi/conf").read_text()


def test_switch_to_nm_removes_the_old_staged_netplan_yaml(env):
    _run("setup_rescue_wifi.sh", env, [], stdin=PSK + "\n")
    staged = _etc(env) / "rovimen/rescue-wifi/90-rovimen-rescue-wifi.yaml"
    assert staged.exists()
    r = _run("setup_rescue_wifi.sh", env, [], stdin=PSK + "\n", FAKE_NM="1")
    assert "RESCUE-WIFI: PASS backend=nm" in r.stdout, r.stdout + r.stderr
    assert not staged.exists()
    assert "netplan apply" not in Path(env["FAKE_LOG"]).read_text()


def test_same_backend_rerun_removes_nothing(env):
    _run("setup_rescue_wifi.sh", env, [], stdin=PSK + "\n")
    r = _run("setup_rescue_wifi.sh", env, [], stdin=PSK + "\n")
    assert "removed" not in r.stdout
    assert (_etc(env) / "rovimen/rescue-wifi/90-rovimen-rescue-wifi.yaml").exists()


def test_generated_password_is_printed_once_and_used(env):
    r = _run("setup_rescue_wifi.sh", env, ["--generate-password"])
    line = next(l for l in r.stdout.splitlines() if l.startswith("RESCUE-WIFI password for"))
    pw = line.split(": ")[1].split()[0]
    assert len(pw) == 14 and pw.count("-") == 2
    assert f'password: "{pw}"' in (_etc(env) / "rovimen/rescue-wifi/90-rovimen-rescue-wifi.yaml").read_text()
    assert pw not in Path(env["FAKE_LOG"]).read_text()
    other = _run("setup_rescue_wifi.sh", env, ["--generate-password"])
    assert pw not in other.stdout


def test_wifi_primary_station_is_skipped(env):
    r = _run("setup_rescue_wifi.sh", env, [], stdin=PSK + "\n", FAKE_DEFAULT_DEV="wlp0s20f3")
    assert r.returncode == 0 and "RESCUE-WIFI: SKIP wlp0s20f3 already carries the default route" in r.stdout


def test_regulatory_domain_is_persistent(env):
    _run("setup_rescue_wifi.sh", env, ["--country", "RO"], stdin=PSK + "\n")
    assert (_etc(env) / "modprobe.d/rovimen-wifi-regdom.conf").read_text() == "options cfg80211 ieee80211_regdom=RO\n"


def test_no_wifi_adapter_is_a_skip(env):
    import shutil
    shutil.rmtree(Path(env["ROVIMEN_SYS_CLASS_NET"]) / "wlp0s20f3")
    r = _run("setup_rescue_wifi.sh", env, [], stdin=PSK + "\n")
    assert r.returncode == 0 and "RESCUE-WIFI: SKIP no Wi-Fi adapter" in r.stdout


def test_dry_run_writes_nothing_and_hides_the_password(env):
    r = _run("setup_rescue_wifi.sh", env, ["--dry-run"], stdin=PSK + "\n")
    assert "dry-run" in r.stdout and PSK not in r.stdout
    assert not (_etc(env) / "rovimen").exists()
    gen = _run("setup_rescue_wifi.sh", env, ["--dry-run", "--generate-password"])
    assert "RESCUE-WIFI password for" not in gen.stdout


@pytest.mark.parametrize("stdin,args", [("short\n", []), (PSK + "\n", ["--ssid", "bad ssid"]),
                                        ('has"quote1\n', []), (PSK + "\n", ["--country", "ro"])])
def test_wifi_rejects_bad_input(env, stdin, args):
    r = _run("setup_rescue_wifi.sh", env, args, stdin=stdin)
    assert r.returncode == 2 and "RESCUE-WIFI: FAIL" in r.stdout


# ── the helper the watchdog calls ───────────────────────────────────────


def _helper(env: dict, action: str, **extra: str):
    e = {k: v for k, v in env.items() if not k.startswith("_")}
    e["ROVIMEN_RESCUE_ASSOC_WAIT"] = "0"  # no real wait for the radio
    e.update(extra)
    return subprocess.run(["bash", str(Path(env["ROVIMEN_SBIN"]) / "rovimen-rescue-wifi"), action],
                          capture_output=True, text=True, env=e, timeout=30)


def _associated(env: dict, yes: bool) -> None:
    """Wi-Fi carrier: 1 only while associated with an access point."""
    (Path(env["ROVIMEN_SYS_CLASS_NET"]) / "wlp0s20f3" / "carrier").write_text("1\n" if yes else "0\n")


def test_netplan_helper_brings_the_profile_up_and_down(env):
    _run("setup_rescue_wifi.sh", env, [], stdin=PSK + "\n")
    live = _etc(env) / "netplan/90-rovimen-rescue-wifi.yaml"
    (_etc(env) / "netplan").mkdir(exist_ok=True)
    _associated(env, True)
    assert _helper(env, "status").stdout.strip() == "down"
    assert _helper(env, "up").returncode == 0 and _mode(live) == 0o600
    assert _helper(env, "status").stdout.strip() == "up"
    assert _helper(env, "down").returncode == 0 and not live.exists()
    assert Path(env["FAKE_LOG"]).read_text().count("netplan apply") == 2


def test_netplan_status_is_waiting_while_no_hotspot_is_associated(env):
    """A live profile with nothing in range is not "up": it carries no traffic."""
    _run("setup_rescue_wifi.sh", env, [], stdin=PSK + "\n")
    live = _etc(env) / "netplan/90-rovimen-rescue-wifi.yaml"
    (_etc(env) / "netplan").mkdir(exist_ok=True)
    _associated(env, False)
    r = _helper(env, "up")
    assert r.returncode == 1 and r.stdout.strip().endswith("waiting") and live.exists()
    assert _helper(env, "status").stdout.strip() == "waiting"
    assert _helper(env, "up").returncode == 1
    assert Path(env["FAKE_LOG"]).read_text().count("netplan apply") == 1, "not re-applied"
    _associated(env, True)
    assert _helper(env, "status").stdout.strip() == "up"
    _associated(env, False)
    assert _helper(env, "down").returncode == 0 and not live.exists()
    assert _helper(env, "status").stdout.strip() == "down"


def test_nm_helper_uses_nmcli(env):
    _run("setup_rescue_wifi.sh", env, [], stdin=PSK + "\n", FAKE_NM="1")
    _helper(env, "up")
    assert "nmcli -w 30 connection up ROVIMEN-RESCUE" in Path(env["FAKE_LOG"]).read_text()


def test_netplan_rescue_owns_the_default_route_while_up(env):
    """Link up behind a dead router: the wired route (metric 100) must not win."""
    _run("setup_rescue_wifi.sh", env, [], stdin=PSK + "\n")
    staged = _etc(env) / "rovimen/rescue-wifi/90-rovimen-rescue-wifi.yaml"
    live = _etc(env) / "netplan/90-rovimen-rescue-wifi.yaml"
    (_etc(env) / "netplan").mkdir(exist_ok=True)
    _associated(env, True)
    assert _helper(env, "up").returncode == 0
    assert live.read_text().count("route-metric: 5\n") == 2
    assert "route-metric: 600" in staged.read_text(), "the staged profile keeps its standby metric"
    assert 'password: "rescue-pass-1234"' in live.read_text()


def test_nm_rescue_lowers_the_metric_on_the_device_only(env):
    _run("setup_rescue_wifi.sh", env, [], stdin=PSK + "\n", FAKE_NM="1")
    _helper(env, "up", FAKE_NM="1")
    log = Path(env["FAKE_LOG"]).read_text()
    assert "nmcli device modify wlp0s20f3 ipv4.route-metric 5 ipv6.route-metric 5" in log
    assert "connection modify" not in log, "the profile keeps metric 600 for next time"
