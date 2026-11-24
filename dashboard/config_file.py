"""Where ``dashboard_config.yaml`` is, how it is loaded, and what an admin
save writes back.

Shared by the dashboard (``cache_store``) and its standalone services
(``clip_requester``, ``nightly_telegram``, ``fireball_alert``).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from models import CameraConfig, DashboardConfig

# Written for every camera even at the default, as the registry always has
# them (code and cam_ip have no default and are always written anyway).
_CAMERA_KEYS_ALWAYS_WRITTEN = frozenset({"rotate", "label"})


def dashboard_config_path() -> Path:
    """``$ROVIMEN_CONFIG``, else ``$ROVIMEN_DASHBOARD_CONFIG``, else the
    ``dashboard_config.yaml`` next to the dashboard code."""
    for var in ("ROVIMEN_CONFIG", "ROVIMEN_DASHBOARD_CONFIG"):
        val = os.environ.get(var, "").strip()
        if val:
            return Path(val)
    return Path(__file__).resolve().parent / "dashboard_config.yaml"


def load_config(path: Path) -> DashboardConfig:
    """The validated station registry in ``path``."""
    return DashboardConfig.model_validate(yaml.safe_load(path.read_text()))


def config_to_dict(config: DashboardConfig) -> dict[str, Any]:
    """The registry as an admin save writes it to ``dashboard_config.yaml``.

    The models' own serialization, so a field added to ``DashboardConfig``,
    ``StationConfig`` or ``CameraConfig`` is written without editing this
    function. Every top-level and station key is written, defaults included,
    except ``ssh_host_key_fingerprints`` while it is empty (most stations are
    unpinned). A camera carries code, cam_ip, rotate and label, and its other
    keys only when they differ from the default, which keeps unpointed,
    single-host cameras short.
    """
    data = config.model_dump(mode="json")
    for host_key, station in config.stations.items():
        entry = data["stations"][host_key]
        if not station.ssh_host_key_fingerprints:
            del entry["ssh_host_key_fingerprints"]
        entry["cameras"] = [_camera_to_dict(cam) for cam in station.cameras]
    return data


def _camera_to_dict(cam: CameraConfig) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, value in cam.model_dump(mode="json").items():
        field = type(cam).model_fields[name]
        if field.is_required():
            out[name] = value
            continue
        default = field.get_default(call_default_factory=True)
        current = getattr(cam, name)
        if current is None and default is not None:
            # Defence in depth: the admin PATCH routes validate their bodies
            # (admin_config.validated_edit), but code that sets a model
            # attribute directly skips validation, so a None can still sit in
            # a field that does not accept it. Written as is, the registry
            # would no longer load; written as the default, it loads as the
            # old serializer's output did.
            current = value = default
        if name in _CAMERA_KEYS_ALWAYS_WRITTEN or current != default:
            out[name] = value
    return out
