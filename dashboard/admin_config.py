"""Admin routes — users CRUD, activity-log health, network-config edits.

Extracted from rovimen_dashboard.py as part of the A-1 refactor
(the 2026-05 security audit). All routes preserved verbatim — same URLs,
same behaviour, same decorators. Wired in from ``create_app()`` via the
two ``register_*_routes`` entry points below.

The user-CRUD slice only needs module-level helpers from
``rovimen_dashboard`` (``require_admin``, ``_load_users``,
``_update_users``), so its register function takes ``app`` only.

The network-config slice needs the per-app ``config`` + ``tunnels``
instances and three closure-bound helpers from ``create_app`` (atomic
``_save_config``, the YAML serialiser, and the rotate-push), so its
register function takes them as parameters rather than reaching back
into the parent module.
"""

from __future__ import annotations

import ipaddress
import re
from contextlib import contextmanager
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Collection, TypeVar

from flask import Flask, abort, jsonify, request, session
from pydantic import BaseModel, ValidationError
from werkzeug.security import generate_password_hash

import security
from auth import password_fingerprint

from models import CameraConfig, DashboardConfig, StationConfig, UserConfig

_Model = TypeVar("_Model", bound=BaseModel)

# What the user PATCH route sets; ``password`` is hashed into password_hash.
_USER_EDITABLE = frozenset({"display_name", "role", "stations", "expires_at", "password"})


class RefusedEdit(Exception):
    """An admin edit the route refuses. Raised inside the save block, so the
    block's write is skipped and the file and the in-memory model stay as
    they were; the route answers ``status`` with the message."""

    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


def validated_edit(model: _Model, updates: Any, editable: Collection[str]) -> _Model:
    """``model`` with ``updates`` applied, validated as a whole new model.

    ``model_copy(update=)`` and attribute assignment do not validate, so a
    JSON null or a string in a number field used to reach
    ``dashboard_config.yaml`` or ``users.yaml``, which then no longer loaded
    (or skipped the user). Raises ``RefusedEdit`` (422) when ``updates`` is
    not an object, names a key outside ``editable``, or holds a value the
    model rejects. Lax coercion still applies, so ``"30"`` is stored as 30.0.
    """
    if not isinstance(updates, dict):
        raise RefusedEdit("Validation failed: the body must be a JSON object")
    unknown = sorted(set(updates) - set(editable))
    if unknown:
        raise RefusedEdit(f"Validation failed: not editable here: {', '.join(unknown)}")
    try:
        return type(model).model_validate({**model.model_dump(), **updates})
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}"
            for err in exc.errors(include_url=False)
        )
        raise RefusedEdit(f"Validation failed: {problems}") from exc


# ── SSRF deny-list for /api/admin/autodetect ──────────────────────────────
#
# ``/api/admin/autodetect`` fetches ``http://{ip}:{port}/api/settings`` with an
# admin-supplied IP. Even behind ``@require_admin`` this is an SSRF primitive: a
# hijacked admin session (or CSRF) could pivot the VPS into probing hosts the
# operator never intended — cloud metadata (169.254.169.254), loopback-only
# admin services, or arbitrary RFC1918 hosts on the VPS's own network segment.
#
# The previous guard only blocked 127/0.0.0.0/169.254 via string-octet math and
# left every other range (10/172.16/192.168, multicast, reserved) reachable.
# This validator classifies the IP with stdlib ``ipaddress`` and denies the full
# set of special-use ranges, and constrains ``port`` to the unprivileged range
# with 22/80/443 denied so the endpoint can't be turned into a scanner for SSH /
# web admin panels.
#
# TRADEOFF — the Tailscale CGNAT range (100.64.0.0/10) is *deliberately allowed*.
# The Add-Station UI's IP field is labelled "Enter a Tailscale IP first" and the
# whole point of autodetect is to read a brand-new station's ``/api/settings``
# over the operator's own tailnet before it's committed to ``dashboard_config``.
# Those 100.x hosts are the operator's own machines, reachable only by
# authenticated tailnet members — fetching them is the feature, not the attack.
# Blanket-blocking CGNAT (as a naive "deny all RFC6598" rule would) breaks the
# documented workflow while closing nothing that matters; the dangerous targets
# (metadata, loopback, RFC1918, link-local) are the ones denied above. The
# allowance is an explicit, named flag (``allow_tailscale``) so it can be flipped
# off and unit-tested in isolation.
_TAILSCALE_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_DENIED_PORTS = frozenset({22, 80, 443})
_MIN_PORT = 1024
_MAX_PORT = 65535


def validate_autodetect_target(
    ip: str, port: Any, *, allow_tailscale: bool = True
) -> str | None:
    """Validate an autodetect ``(ip, port)`` against the SSRF deny-list.

    Returns ``None`` when the target is allowed, or a human-readable error
    string when it must be rejected. Kept side-effect free and importable so
    the deny-list can be unit-tested without standing up the Flask app.

    ``allow_tailscale`` keeps the intended station-tailnet use working: when
    ``True`` (the default for the autodetect route) an address in the Tailscale
    CGNAT range is permitted. When ``False`` the CGNAT range is rejected too and
    only ordinary public addresses pass.

    H6 — allowing the Tailscale CGNAT range (100.64.0.0/10) is a DELIBERATE,
    owner-accepted tradeoff, not an oversight: autodetect's entire purpose is
    to read a brand-new station's ``/api/settings`` over the operator's own
    tailnet before it's committed to ``dashboard_config``, and those 100.x
    hosts are the operator's own authenticated tailnet peers. The residual
    risk (an admin session could probe arbitrary tailnet hosts/ports in the
    unprivileged range) is accepted; the compensating control is that every
    autodetect attempt is audit-logged in the route handler (see
    ``api_admin_autodetect``). The dangerous ranges — metadata, loopback,
    RFC1918, link-local, multicast, reserved — remain denied above.
    """
    try:
        addr = ipaddress.ip_address(str(ip).strip())
    except ValueError:
        return "Invalid IP address format"
    if isinstance(addr, ipaddress.IPv6Address):
        # The station fleet is IPv4-only; refuse v6 rather than reason about
        # its (larger) set of special-use ranges.
        return "IP address not allowed"

    in_tailnet = addr in _TAILSCALE_CGNAT
    if in_tailnet:
        # The CGNAT range is the intended target. Permit it when allowed,
        # reject it explicitly otherwise. We check membership directly rather
        # than relying on ``is_private`` because the stdlib classification of
        # 100.64.0.0/10 differs across Python versions (private on 3.12-,
        # un-classified on 3.13+); an explicit check is version-stable.
        if not allow_tailscale:
            return "IP address not allowed"
    elif (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    ):
        return "IP address not allowed"

    try:
        port_i = int(port)
    except (TypeError, ValueError):
        return "Invalid port"
    if port_i < _MIN_PORT or port_i > _MAX_PORT or port_i in _DENIED_PORTS:
        return "Port not allowed"
    return None


def register_admin_user_routes(app: Flask) -> None:
    """Attach user-CRUD + activity-log health endpoints to ``app``.

    The helpers ``require_admin`` / ``_load_users`` / ``_update_users``
    are imported lazily from ``rovimen_dashboard`` to keep this module
    free of any cyclic import at parse time.
    """
    # Lazy import: this module is loaded *from* rovimen_dashboard, so a
    # top-level ``import rovimen_dashboard`` here would deadlock the
    # interpreter. By the time register_*_routes() runs, the parent
    # module's globals are fully populated.
    from rovimen_dashboard import (
        _load_users,
        _update_users,
        require_admin,
    )

    @app.route("/api/admin/users")
    @require_admin
    def api_admin_users_list():
        users = _load_users()
        result = []
        for username, u in users.items():
            result.append({
                "username": username,
                "display_name": u.display_name,
                "role": u.role,
                "stations": u.stations,
                "expires_at": u.expires_at,
                "has_magic_link": bool(u.magic_token),
            })
        return jsonify(result)

    @app.route("/api/admin/users/activity")
    @require_admin
    def api_admin_users_activity():
        """Per-user last-login + active-time summary for the Users table.

        Derived from the audit + activity logs (read-only). 90-day window
        is plenty to surface a "last login" for an account that signs in
        rarely without scanning the whole log history.
        """
        import usage_stats
        return jsonify(usage_stats.compute_user_activity(days=90))

    @app.route("/api/admin/usage-stats")
    @require_admin
    def api_admin_usage_stats():
        """Site-wide feature-usage report over a trailing day window."""
        import usage_stats
        try:
            days = int(request.args.get("days", "7"))
        except (TypeError, ValueError):
            days = 7
        return jsonify(usage_stats.compute_usage(days=days))

    @app.route("/api/admin/traffic")
    @require_admin
    def api_admin_traffic():
        """Visitor-traffic report (anonymous + authenticated page views)."""
        import usage_stats
        try:
            days = int(request.args.get("days", "7"))
        except (TypeError, ValueError):
            days = 7
        return jsonify(usage_stats.compute_traffic_report(days=days))

    @app.route("/api/admin/users", methods=["POST"])
    @require_admin
    def api_admin_users_create():
        body = request.get_json(force=True) or {}
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        role     = body.get("role") or "host"
        if not username or not password:
            return jsonify({"error": "username and password required"}), 400
        # Match the floor enforced by /set-password — otherwise admin-side
        # user creation would silently bypass the policy that operators
        # going through self-serve reset are held to.
        if len(password) < 14:
            return jsonify({"error":
                            "password must be at least 14 characters"}), 400
        if role not in ("admin", "host", "visitor", "press"):
            return jsonify({"error": "invalid role"}), 400

        expires_at = body.get("expires_at") or None

        class _Conflict(Exception):
            pass

        def _mutate(users: dict[str, UserConfig]) -> None:
            if username in users:
                raise _Conflict()
            users[username] = UserConfig(
                display_name=body.get("display_name") or "",
                password_hash=generate_password_hash(password),
                role=role,
                stations=body.get("stations") or [],
                expires_at=expires_at,
            )

        try:
            _update_users(_mutate)
        except _Conflict:
            return jsonify({"error": "user already exists"}), 409
        return jsonify({"ok": True})

    @app.route("/api/admin/users/<username>", methods=["PATCH"])
    @require_admin
    def api_admin_users_update(username: str):
        body = request.get_json(force=True) or {}

        # Validate the body once up front — anything we'd otherwise
        # discover deep inside the mutator (and then have to bubble out
        # via exceptions) becomes a plain 400 here.
        if not isinstance(body, dict):
            return jsonify({"error": "the body must be a JSON object"}), 400
        unknown = sorted(set(body) - _USER_EDITABLE)
        if unknown:
            return jsonify({"error": f"not editable here: {', '.join(unknown)}"}), 400
        if "role" in body and body["role"] not in ("admin", "host", "visitor", "press"):
            return jsonify({"error": "invalid role"}), 400
        new_pw = body.get("password") or ""
        if not isinstance(new_pw, str):
            return jsonify({"error": "password must be a string"}), 400
        if new_pw and len(new_pw) < 14:
            # Same floor as /set-password and user-create; a PATCH must
            # not be a silent escape hatch around the policy.
            return jsonify({"error":
                            "password must be at least 14 characters"}), 400

        class _NotFound(Exception):
            pass

        new_hash = generate_password_hash(new_pw) if new_pw else ""

        def _mutate(users: dict[str, UserConfig]) -> None:
            if username not in users:
                raise _NotFound()
            u = users[username]
            # Validate the edited account as a whole before touching it:
            # attribute assignment does not validate, and users.yaml skips an
            # entry that fails validation, so a stored null display_name
            # left that account unable to sign in.
            updates = {k: body[k] for k in ("display_name", "role", "stations") if k in body}
            if "expires_at" in body:
                updates["expires_at"] = body["expires_at"] or None
            checked = validated_edit(u, updates, _USER_EDITABLE)
            # Track whether any privilege-relevant field actually changed so we
            # only bump the session epoch (which force-revalidates live
            # sessions) when role/stations/password/expiry move. A pure
            # display-name edit must not log the user out.
            privilege_changed = False
            if "display_name" in body:
                u.display_name = checked.display_name
            if "role" in body:
                # A role change invalidates any outstanding magic-login link:
                # the link is a bearer credential issued against the old
                # privilege level, so a host→admin promotion (or any change)
                # must not let a stale token redeem at the new level.
                if checked.role != u.role:
                    u.magic_token = None
                    privilege_changed = True
                u.role = checked.role
            if "stations" in body:
                if checked.stations != u.stations:
                    privilege_changed = True
                u.stations = checked.stations
            if "expires_at" in body:
                if checked.expires_at != u.expires_at:
                    privilege_changed = True
                u.expires_at = checked.expires_at
            if new_hash:
                u.password_hash = new_hash
                privilege_changed = True
            if privilege_changed:
                # Bump the session epoch so the before_request revalidation
                # gate re-reads this account's live role/stations into any
                # outstanding session on its next request — a demotion/de-scope
                # takes effect immediately, not at the user's next
                # /api/auth/status poll (audit H3).
                u.session_epoch = (u.session_epoch or 0) + 1

        try:
            _update_users(_mutate)
        except _NotFound:
            abort(404)
        except RefusedEdit as exc:
            # 400 like this route's other input errors; the users tab shows
            # ``error`` whatever the status.
            return jsonify({"error": str(exc)}), 400
        if new_hash and session.get("user") == username:
            # A new password ends every session of the account whose tag no
            # longer matches (auth.revalidate_session); an admin changing
            # their own password keeps this one by taking the new tag.
            session["pw_fp"] = password_fingerprint(new_hash)
        return jsonify({"ok": True})

    @app.route("/api/admin/users/<username>/reset-password", methods=["POST"])
    @require_admin
    def api_admin_users_reset_password(username: str):
        # Generate a high-entropy one-time token. The plaintext is shown to
        # the issuing admin once (it has to be delivered out-of-band to the
        # user). Only the hash is persisted, so a leak of users.yaml no
        # longer leaks an active account-takeover code — matching how we
        # treat password hashes elsewhere. Verification at /login goes
        # through check_password_hash (constant-time inside).
        token  = secrets.token_urlsafe(32)
        expiry = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()

        class _NotFound(Exception):
            pass

        def _mutate(users: dict[str, UserConfig]) -> None:
            if username not in users:
                raise _NotFound()
            u = users[username]
            u.reset_token        = generate_password_hash(token)
            u.reset_token_expiry = expiry
            # A password reset is a privilege-relevant change: bump the session
            # epoch so any existing session for this account is force-
            # revalidated against users.yaml on its next authenticated request
            # (audit H3) rather than running on a stale role/station snapshot.
            u.session_epoch = (u.session_epoch or 0) + 1

        try:
            _update_users(_mutate)
        except _NotFound:
            abort(404)
        return jsonify({"ok": True, "token": token})

    @app.route("/api/admin/users/<username>/magic-link", methods=["POST"])
    @require_admin
    def api_admin_users_magic_link(username: str):
        """Mint (or rotate) a one-click magic-login link for a host account.

        Returns the full ``<base>/l/<token>`` URL once — only the hash is
        persisted, mirroring how ``reset-password`` treats its token. Each
        call rotates the token, so re-issuing instantly invalidates the
        previous link. The link is a bearer credential, so we refuse to
        mint one for an ``admin`` account: a leaked link must never be able
        to confer admin. Pair this with ``expires_at`` (set via the user
        PATCH) to bound the link's lifetime.

        Optional JSON body: ``{"days": <int>}`` (account is active for that
        many days from now) or ``{"expires_at": "<ISO8601 UTC>"}``. If both
        are present, ``expires_at`` wins. Omit both to leave the existing
        expiry untouched.
        """
        from public_api import _public_base_url

        body = request.get_json(silent=True) or {}
        new_expiry = body.get("expires_at")
        if not new_expiry and body.get("days") is not None:
            try:
                days = int(body["days"])
            except (TypeError, ValueError):
                return jsonify({"error": "days must be an integer"}), 400
            if days < 1 or days > 3650:
                return jsonify({"error": "days must be between 1 and 3650"}), 400
            new_expiry = (
                datetime.now(timezone.utc) + timedelta(days=days)
            ).isoformat()

        token = secrets.token_urlsafe(32)

        class _NotFound(Exception):
            pass

        class _AdminRefused(Exception):
            pass

        def _mutate(users: dict[str, UserConfig]) -> None:
            if username not in users:
                raise _NotFound()
            u = users[username]
            if u.role == "admin":
                raise _AdminRefused()
            u.magic_token = generate_password_hash(token)
            if new_expiry:
                u.expires_at = new_expiry

        try:
            _update_users(_mutate)
        except _NotFound:
            abort(404)
        except _AdminRefused:
            return jsonify(
                {"error": "magic links are not allowed for admin accounts"}), 400

        url = f"{_public_base_url()}/l/{token}"
        return jsonify({"ok": True, "url": url, "token": token})

    @app.route("/api/admin/users/<username>/magic-link", methods=["DELETE"])
    @require_admin
    def api_admin_users_magic_link_revoke(username: str):
        """Revoke a magic-login link by clearing the stored token hash."""
        class _NotFound(Exception):
            pass

        def _mutate(users: dict[str, UserConfig]) -> None:
            if username not in users:
                raise _NotFound()
            users[username].magic_token = None

        try:
            _update_users(_mutate)
        except _NotFound:
            abort(404)
        return jsonify({"ok": True})

    @app.route("/api/admin/users/<username>", methods=["DELETE"])
    @require_admin
    def api_admin_users_delete(username: str):
        class _NotFound(Exception):
            pass

        def _mutate(users: dict[str, UserConfig]) -> None:
            if username not in users:
                raise _NotFound()
            users.pop(username)

        try:
            _update_users(_mutate)
        except _NotFound:
            abort(404)
        return jsonify({"ok": True})

    @app.route("/api/admin/health/activity_log")
    @require_admin
    def api_admin_activity_log_health():
        """Operator-visibility for the bounded activity-log queue (P1-15).

        Returns the running drop counter and the current queue depth.
        When ``dropped_since_boot`` is non-zero some authenticated
        traffic has been logged with gaps and we should investigate
        either the writer-thread health or the load that's outrunning
        the disk.
        """
        return jsonify(security.activity_log_stats())

    @app.route("/api/admin/health/key_stores")
    @require_admin
    def api_admin_key_store_health():
        """Whether the station and public-API key files load.

        Each entry under ``failures`` reads, for example, "station_keys.yaml
        unreadable since 21:29 UTC, serving last good keys until 21:44 UTC"
        (``serving_stale_keys`` true) or failing closed. State is per gunicorn
        worker; a reload is attempted first so the answer is current.
        """
        import api_keys
        import station_keys

        out: dict[str, Any] = {}
        for name, mod in (("station_keys", station_keys), ("api_keys", api_keys)):
            mod._get_indexes(mod.DEFAULT_KEYS_PATH)
            out[name] = {"path": str(mod.DEFAULT_KEYS_PATH), "failures": mod._last_good.status()}
        return jsonify(out)


def _optional_float(value: Any) -> float | None:
    """A form number that may be blank: None for a missing or empty value,
    else the float. ``value or None`` turned az 0 (north) into None."""
    if value is None or value == "":
        return None
    return float(value)


def register_admin_network_routes(
    app: Flask,
    config: DashboardConfig,
    *,
    save_config: Callable[[], None],
    config_to_dict: Callable[[], dict[str, Any]],
    require_station: Callable[[str], StationConfig],
    session_for_url,
    config_path: "Path | None" = None,
    config_store=None,
) -> None:
    """Attach the /api/admin/network-config + autodetect routes.

    The closure-bound helpers (``save_config`` / ``config_to_dict`` /
    ``require_station``) are passed in from
    ``create_app()`` rather than re-defined here — keeping the atomic
    write lock and tunnel reference shared with the rest of the dashboard.
    """
    from rovimen_dashboard import require_admin
    import config_sync

    _original_save = save_config

    def save_config():
        _original_save()
        if config_path:
            config_sync.sync_config(config_path)

    @contextmanager
    def mutate_config():
        """Reload-from-disk, mutate, write -- then mirror the snapshot.

        Every registry mutator runs inside this so a save starts from the
        file, not from this worker's memory (see cache_store.ConfigStore):
        two workers editing different cameras used to overwrite each other.
        Falls back to the legacy whole-object save when no store is wired
        (tests that build the routes without one).
        """
        if config_store is None:
            yield config
            save_config()
            return
        with config_store.mutation() as cfg:
            yield cfg
        if config_path:
            config_sync.sync_config(config_path)

    @app.route("/api/admin/network-config")
    @require_admin
    def api_admin_network_config_get():
        return jsonify(config_to_dict())

    @app.route("/api/admin/network-config/station/<host_key>", methods=["PATCH"])
    @require_admin
    def api_admin_network_config_station(host_key: str):
        body = request.get_json(force=True) or {}
        allowed = {"ip", "label", "ssh_user", "proxy_media", "jump_hosts", "lat", "lon", "show_on_map", "public_tabs", "public", "location_name", "status", "push_enabled"}
        try:
            with mutate_config():
                require_station(host_key)
                st = config.stations[host_key]
                config.stations[host_key] = validated_edit(st, body, allowed)
        except RefusedEdit as exc:
            return jsonify({"ok": False, "error": str(exc)}), exc.status
        return jsonify({"ok": True})

    @app.route(
        "/api/admin/network-config/station/<host_key>/camera",
        methods=["POST"],
    )
    @require_admin
    def api_admin_network_config_camera_add(host_key: str):
        body = request.get_json(force=True) or {}
        code = (body.get("code") or "").strip()
        if not code:
            return jsonify({"ok": False, "error": "code is required"}), 400
        with mutate_config():
            require_station(host_key)
            st = config.stations[host_key]
            if any(c.code == code for c in st.cameras):
                return jsonify({"ok": False, "error": f"Camera {code} already exists"}), 409
            st.cameras.append(CameraConfig(
                code=code,
                cam_ip=body.get("cam_ip", ""),
                rotate=bool(body.get("rotate", False)),
                az=_optional_float(body.get("az")),
                alt=_optional_float(body.get("alt")),
                host=(body.get("host") or "").strip(),
                clips=bool(body.get("clips", True)),
            ))
        return jsonify({"ok": True})

    @app.route(
        "/api/admin/network-config/station/<host_key>/camera/<cam_code>",
        methods=["PATCH"],
    )
    @require_admin
    def api_admin_network_config_camera(host_key: str, cam_code: str):
        body = request.get_json(force=True) or {}
        cam_allowed = {"cam_ip", "rotate", "az", "alt", "label", "code", "host",
                       "clips", "spectro"}
        try:
            with mutate_config():
                require_station(host_key)
                st = config.stations[host_key]
                idx = next((i for i, c in enumerate(st.cameras) if c.code == cam_code), None)
                if idx is None:
                    abort(404)
                updated_cam = validated_edit(st.cameras[idx], body, cam_allowed)
                if not updated_cam.code.strip():
                    raise RefusedEdit("Validation failed: code: must not be empty")
                if updated_cam.code != cam_code and any(
                        c.code == updated_cam.code for c in st.cameras):
                    # Every per-camera lookup takes the first match, so a
                    # second camera under one code would shadow the first.
                    raise RefusedEdit(f"Camera {updated_cam.code} already exists", 409)
                st.cameras[idx] = updated_cam
        except RefusedEdit as exc:
            return jsonify({"ok": False, "error": str(exc)}), exc.status

        # dashboard_config.yaml stays the source of truth; the rotate flag is
        # no longer pushed to the station (read-only fleet, 2026-07) — the
        # station picks it up on its next signed-bundle deploy.
        return jsonify({"ok": True, "station_push": None})

    @app.route(
        "/api/admin/network-config/station/<host_key>/camera/<cam_code>",
        methods=["DELETE"],
    )
    @require_admin
    def api_admin_network_config_camera_delete(host_key: str, cam_code: str):
        with mutate_config():
            require_station(host_key)
            st = config.stations[host_key]
            st.cameras = [c for c in st.cameras if c.code != cam_code]
        return jsonify({"ok": True})

    # /api/admin/sync-rotate-all was removed 2026-07 (read-only fleet): rotate
    # lives in dashboard_config.yaml and reaches stations via signed bundles.

    @app.route("/api/admin/autodetect", methods=["POST"])
    @require_admin
    def api_admin_autodetect():
        body = request.get_json(force=True) or {}
        ip = body.get("ip", "")
        port = body.get("port", 7779)
        deny_reason = validate_autodetect_target(ip, port)
        # H6: allowing the tailnet CGNAT range is an accepted tradeoff — make
        # every attempt auditable. Log the requested target + outcome (allowed
        # or the deny reason) with the admin's IP/UA so tailnet-scanning via a
        # hijacked admin session is at least attributable after the fact.
        security.audit_request(
            "admin.autodetect",
            target_ip=str(ip),
            target_port=port,
            allowed=deny_reason is None,
            deny_reason=deny_reason,
        )
        if deny_reason is not None:
            return jsonify({"ok": False, "error": deny_reason}), 400
        port = int(port)
        try:
            autodetect_url = f"http://{ip}:{port}/api/settings"
            resp = session_for_url(autodetect_url).get(autodetect_url, timeout=8)
            resp.raise_for_status()
            settings = resp.json()
        except Exception:
            return jsonify({"ok": False, "error": f"Could not reach station at {ip}:{port}"})

        cameras = []
        ssh_user = "gmn"
        stations_dict = settings.get("stations", {})
        first = True
        for code, sinfo in stations_dict.items():
            rtsp = sinfo.get("camera_rtsp", "")
            m = re.search(r"rtsp://admin:@([\d.]+):", rtsp)
            cam_ip = m.group(1) if m else ""
            rotate = sinfo.get("rotate", False)
            cameras.append({"code": code, "cam_ip": cam_ip, "rotate": rotate})
            if first:
                rms_path = sinfo.get("rms_data_path", "")
                um = re.search(r"/home/([^/]+)/", rms_path)
                if um:
                    ssh_user = um.group(1)
                first = False

        return jsonify({"ok": True, "cameras": cameras, "ssh_user": ssh_user})

    @app.route("/api/admin/network-config/station", methods=["POST"])
    @require_admin
    def api_admin_network_config_station_add():
        body = request.get_json(force=True) or {}
        host = body.get("host", "")
        if not re.match(r"^[a-z0-9_-]+$", host):
            return jsonify({"ok": False, "error": "Invalid host key"}), 400
        jump_raw = body.get("jump_hosts", [])
        jump_hosts = [j.strip() for j in jump_raw if j.strip()] if isinstance(jump_raw, list) else []

        cameras = [
            CameraConfig(
                code=c.get("code", ""),
                cam_ip=c.get("cam_ip", ""),
                rotate=bool(c.get("rotate", False)),
                host=(c.get("host") or "").strip(),
            )
            for c in (body.get("cameras") or [])
        ]

        with mutate_config():
            if host in config.stations:
                return jsonify({"ok": False, "error": f"Station '{host}' already exists"}), 409
            config.stations[host] = StationConfig(
                ip=body.get("ip", ""),
                label=body.get("label", host),
                ssh_user=body.get("ssh_user", "gmn"),
                proxy_media=bool(body.get("proxy_media", False)),
                jump_hosts=jump_hosts,
                cameras=cameras,
                lat=body.get("lat") or None,
                lon=body.get("lon") or None,
                show_on_map=bool(body.get("show_on_map", True)),
            )
        return jsonify({"ok": True})

    @app.route("/api/admin/network-config/station/<host_key>", methods=["DELETE"])
    @require_admin
    def api_admin_network_config_station_delete(host_key: str):
        with mutate_config():
            require_station(host_key)
            config.stations.pop(host_key)
        return jsonify({"ok": True})
