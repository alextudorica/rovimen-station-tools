"""Sync dashboard_config.yaml back to the GitHub repo after admin edits.

When an admin changes the config via the dashboard UI, the YAML is
written to disk by save_config(). This module pushes that updated file
to the repo so the next deploy carries the change, and the repo stays
the single source of truth.

Requires GITHUB_TOKEN env var with repo contents:write scope.
Runs in a background thread so the admin API response isn't blocked.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import base64
import urllib.request
import urllib.error
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
REPO = os.environ.get("GITHUB_REPO", "")
CONFIG_PATH_IN_REPO = "dashboard/dashboard_config.yaml"
# GITHUB_CONFIG_BRANCH selects the branch admin-UI config edits are committed
# to. There is one branch since the dev tier was retired (2026-09-26), so the
# default is main; prod also sets it in /etc/rovimen-dashboard.env.
BRANCH = os.environ.get("GITHUB_CONFIG_BRANCH", "main")

_API = "https://api.github.com"

# Structural invariants checked before a snapshot is allowed onto the branch.
# Deliberately a narrow subset of tests/test_dashboard_config_invariants.py --
# that file loads the real committed config and stays the authoritative gate.
# What is mirrored here is only what distinguishes a coherent snapshot from a
# half-written one, so an admin mid-edit is blocked while a legitimately
# unusual (but valid) fleet change still syncs.
_STATION_REQUIRED_KEYS = ("ip", "cameras")
_IPV4_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")
_CAM_CODE_RE = re.compile(r"^[A-Z]{2}[0-9A-Z]{4}$")


def validate_config(data: object) -> list[str]:
    """Structural violations in a dashboard_config snapshot; [] means valid.

    Returns descriptions rather than raising: the caller runs on a background
    thread serving an admin request, and a bad snapshot must be skipped, not
    turned into a 500.
    """
    violations: list[str] = []
    if not isinstance(data, dict):
        return ["config root is not a YAML mapping"]

    stations = data.get("stations")
    if not isinstance(stations, dict) or not stations:
        return ["'stations' is missing, empty, or not a mapping"]

    for station_id, station in stations.items():
        if not isinstance(station, dict):
            violations.append(f"{station_id}: station entry is not a mapping")
            continue

        for key in _STATION_REQUIRED_KEYS:
            if key not in station:
                violations.append(f"{station_id}: missing required key {key!r}")

        ip = station.get("ip", "")
        if not isinstance(ip, str) or not _IPV4_RE.match(ip):
            violations.append(f"{station_id}: ip={ip!r} is not a dotted-quad IPv4")

        cameras = station.get("cameras", [])
        if not isinstance(cameras, list):
            violations.append(f"{station_id}: 'cameras' is not a list")
            continue

        # Scope is the host, not the station: a station split across two boxes
        # has two independent camera LANs that legitimately reuse addresses.
        seen_ips: dict[tuple[str, str], str] = {}
        for cam in cameras:
            if not isinstance(cam, dict):
                violations.append(f"{station_id}: camera entry is not a mapping")
                continue

            code = cam.get("code", "")
            if not isinstance(code, str) or not _CAM_CODE_RE.match(code):
                violations.append(
                    f"{station_id}: camera code {code!r} is not a 6-character "
                    "GMN station code"
                )

            cam_ip = (cam.get("cam_ip") or "").strip()
            if not cam_ip:
                continue  # camera not yet connected -- allowed
            host = (cam.get("host") or "").strip() or ip
            key = (host, cam_ip)
            if key in seen_ips:
                violations.append(
                    f"{station_id}: {code!r} shares cam_ip {cam_ip!r} with "
                    f"{seen_ips[key]!r} on host {host!r}"
                )
            else:
                seen_ips[key] = code

    return violations


def _sync_to_github(local_path: Path) -> None:
    if not GITHUB_TOKEN:
        logger.debug("config_sync: no GITHUB_TOKEN, skipping")
        return

    try:
        content = local_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("config_sync: cannot read %s: %s", local_path, exc)
        return

    # Gate the push on structural validity. The committed config is loaded by
    # tests/test_dashboard_config_invariants.py, so an incoherent snapshot --
    # an admin caught mid-edit -- turns CI red on every open PR until someone
    # reverts it by hand. Skipping the commit costs one un-synced edit, which
    # the next save re-pushes; committing it costs the whole team's CI.
    try:
        violations = validate_config(yaml.safe_load(content))
    except yaml.YAMLError as exc:
        violations = [f"config is not parseable YAML: {exc}"]
    if violations:
        logger.error(
            "config_sync: refusing to push an invalid config to %s/%s "
            "(%d violation(s)):\n%s",
            REPO, BRANCH, len(violations),
            "\n".join(f"  - {v}" for v in violations),
        )
        return

    url = f"{_API}/repos/{REPO}/contents/{CONFIG_PATH_IN_REPO}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "ROVIMEN-Dashboard/1.0",
        "Content-Type": "application/json",
    }

    try:
        req = urllib.request.Request(
            f"{url}?ref={BRANCH}", headers=headers, method="GET"
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            existing = json.loads(resp.read())
        sha = existing.get("sha", "")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            sha = ""
        else:
            logger.warning("config_sync: GET failed: %s", exc)
            return
    except Exception as exc:
        logger.warning("config_sync: GET failed: %s", exc)
        return

    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    body = {
        "message": "chore: sync dashboard_config.yaml from admin UI",
        "content": encoded,
        "branch": BRANCH,
    }
    if sha:
        body["sha"] = sha

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="PUT",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        logger.info("config_sync: pushed to %s/%s", REPO, BRANCH)
    except Exception as exc:
        logger.warning("config_sync: PUT failed: %s", exc)


def sync_config(local_path: Path) -> None:
    """Push config to GitHub in a background thread (non-blocking)."""
    t = threading.Thread(
        target=_sync_to_github, args=(local_path,), daemon=True
    )
    t.start()
