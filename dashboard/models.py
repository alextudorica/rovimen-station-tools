"""Shared Pydantic configuration / user dataclasses for the dashboard.

These models are deliberately kept dependency-free (only ``pydantic``) so that
they can be imported by ``rovimen_dashboard.py``, ``public_api.py`` and any
future extracted sub-modules without circular-import grief.

Moving forward, any new dataclass that is consumed by more than one of the
dashboard sub-modules should live here too.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator


_IPV4_RE = re.compile(r"^[0-9]{1,3}(\.[0-9]{1,3}){3}$")
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.\-]*[A-Za-z0-9])?$")


class CameraConfig(BaseModel):
    code: str
    cam_ip: str
    rotate: bool = False
    # Does this camera deliver colour clips to the archive at all?
    #
    # Set false for a camera whose detections will never be backed by a video
    # file: gmnro17 (Câmpenița) runs plots-only Raspberry Pis with no
    # colour-capture pipeline, and a station with `archive.enabled: false`
    # ships no meteor bytes while that hold lasts. Without the flag every one
    # of their detections renders as "clip pending" forever, which says a clip
    # is on its way when none ever is. With it the marker reads "no video"
    # instead — the detection, its RMS metadata and its FF thumbnail still
    # show. Flip it back to true the moment the camera starts uploading.
    clips: bool = True
    label: str = ""
    az: float | None = None   # pointing azimuth degrees (0=N, 90=E, 180=S, 270=W)
    alt: float | None = None  # pointing elevation degrees above horizon
    rtsp_url: str | None = None
    # Spectroscopy camera (a grating in front of the lens, colour capture
    # only, no RMS). Gates the Final Data Products spectroscopy section and
    # the /api/spectro listing; the station-side pipeline is configured
    # separately in the station's own config (stations.<code>.spectro).
    spectro: bool = False
    # Optional per-camera override of the STATION HOST that serves this
    # camera's on-box API (:7779). Empty (the default) means "this camera
    # lives on the station's own `ip`" — the case for every single-box
    # station. Set it when one logical station is physically split across
    # two boxes: gmnro17 (Câmpenița) is two Raspberry Pis, each with its own
    # camera and its own Tailscale address, but the operator, the site and
    # the map marker are one station. Camera-scoped requests (nights, FF /
    # maxpixel, platepar, chunks, thumbnails) are then routed to this host
    # instead of `StationConfig.ip`; station-scoped requests (overview
    # online flag, disk, services) still go to `StationConfig.ip`.
    #
    # Accepts an IPv4 address or a hostname (e.g. a MagicDNS name). NOT
    # honoured for stations reached through `jump_hosts`: the SSH tunnel
    # forwards to `StationConfig.ip` only, so a split station must be
    # directly routable (see TunnelManager.get_api_base).
    host: str = ""

    @field_validator("cam_ip")
    @classmethod
    def _validate_cam_ip(cls, v: str) -> str:
        v = v.strip()
        if v and not _IPV4_RE.match(v):
            raise ValueError(f"cam_ip must be a dotted-quad IPv4 address, got {v!r}")
        return v

    @field_validator("host")
    @classmethod
    def _validate_host(cls, v: str) -> str:
        v = v.strip()
        if v and not (_IPV4_RE.match(v) or _HOSTNAME_RE.match(v)):
            raise ValueError(
                f"host must be an IPv4 address or a hostname, got {v!r}"
            )
        return v


class StationConfig(BaseModel):
    ip: str
    label: str
    ssh_user: str = "gmn"
    cameras: list[CameraConfig] = Field(default_factory=list)
    jump_hosts: list[str] = Field(default_factory=list)
    # Pinned SSH host-key fingerprint(s) for this station's IP (H5). When set,
    # `_ensure_known_hosts` refuses to trust an ssh-keyscan result unless its
    # fingerprint matches one of these values, closing the first-contact TOFU
    # MITM window. Fingerprints are the base64 SHA256 form ssh-keygen emits
    # (e.g. "SHA256:abc123..."), one per host key type, and MUST be verified
    # out-of-band before pinning (see tools/pin_host_keys.py). Left empty for
    # already-deployed stations, which stay on accept-new (a WARNING is logged
    # so unpinned hosts remain visible) — this keeps the fleet backward
    # compatible while the pins are rolled out station by station.
    ssh_host_key_fingerprints: list[str] = Field(default_factory=list)
    proxy_media: bool = False  # proxy all media through dashboard (for cross-tailnet stations)
    lat: float | None = None
    lon: float | None = None
    show_on_map: bool = True
    public_tabs: list[str] = Field(default_factory=list)  # 'videodb', 'archive' visible to guests
    # Controls visibility on the public /api/public/v1/* + /media/v1/*
    # read-only surface. Default true so every new station is visible
    # automatically. Set to false for stations still in commissioning or
    # explicitly opted out. Non-public stations still participate internally
    # (overview, events, GMN cross-join) but are never returned by public
    # endpoints and their media is not served via /media/v1/.
    public: bool = True
    # Human-readable location surfaced on the public API. Falls back to
    # `label` when empty. Free-form (e.g. "Bistrița, Bistrița-Năsăud").
    location_name: str = ""
    # Operational status: "active" (default) or "commissioning". Stations
    # in commissioning state show orange markers on the overview map instead
    # of the normal green/red online/offline indicators.
    status: str = "active"
    # Migration primitive for the reversed-HTTP push rollout. When True, the
    # dashboard stops server-side POLLING this station (status/vitals in
    # station_client + the detection-index poll in index_poller) and relies
    # entirely on data the station PUSHES into the shared StationCache /
    # detections.db via the ingest API. Default False keeps every station on
    # the existing poll path — flip a single station to True to cut it over to
    # push individually. Fail-safe: a push_enabled station that stops pushing
    # simply shows its last-known cache entry (the ingest owns freshness); the
    # dashboard never fabricates status for it.
    push_enabled: bool = False

    @property
    def is_split_host(self) -> bool:
        """True when at least one camera is served by a different box than
        ``ip`` — i.e. this logical station spans two or more hosts."""
        return any(c.host for c in self.cameras)

    def camera_host(self, code: str) -> str:
        """Host that serves ``code``'s on-box API: the camera's own ``host``
        override when set, else the station ``ip``. Unknown codes fall back
        to the station ``ip`` so a stale code can never break routing."""
        for cam in self.cameras:
            if cam.code == code:
                return cam.host or self.ip
        return self.ip

    def host_for_path(self, path: str) -> str:
        """Resolve which box a station-API request for ``path`` must go to.

        A request is camera-scoped exactly when a camera code appears in its
        path or query string (``/api/nights/RO0002``,
        ``/api/chunks/RO0002/20260824``, ``/api/platepar?cam=RO0002``), and
        camera codes are distinctive enough that their presence is a reliable
        marker — a station-scoped path never contains one. Such a request is
        routed to that camera's ``host`` override when it has one. Everything
        else — and every camera without an override — stays on ``ip``.

        Keeping the decision here (rather than threading an explicit ``cam=``
        argument through ~60 call sites) means every existing caller routes
        correctly without being touched, and a new camera-scoped endpoint is
        covered the moment it puts the code in its URL.
        """
        if not path:
            return self.ip
        for cam in self.cameras:
            if not cam.host or not cam.code:
                continue
            if re.search(
                rf"(?<![0-9A-Za-z]){re.escape(cam.code)}(?![0-9A-Za-z])", path
            ):
                return cam.host
        return self.ip

    @property
    def should_contact(self) -> bool:
        """Whether the dashboard may reach out to this station's on-box API
        (:7779) or SSH at all.

        False for stations in ``commissioning`` state: they're being set up,
        usually have no cameras wired yet, and their IP is often a dead or
        absent Tailscale peer (a box that's powered off between site visits).
        Repeatedly background-polling such a peer wedges tailscaled's magicsock
        with DERP route-churn — an offline commissioning station has taken the
        whole tailnet path to the VPS down. There is nothing to fetch from a
        commissioning station anyway, so every background poll/prefetch/index
        loop MUST gate on this and never call it. Active stations (even
        temporarily offline ones) stay contactable so we notice when they
        recover.
        """
        return self.status != "commissioning"


# Stable page keys that CAN be exposed to anonymous visitors. Each maps to
# one or more ``@public_route(page=...)`` views. This is the *eligible* set
# — the closed vocabulary the runtime ``public_pages`` toggle draws from.
# A key here does NOT make a page public on its own; it only means the page
# is allowed to appear in ``public_pages``. Sensitive pages (admin, config,
# social, network, station-write) are deliberately absent and have no
# ``page=`` tag, so config can never expose them. "highlights" is eligible:
# its page + data route serve curated, GMN-confirmed top-10 clips carrying
# no admin fields, so an operator may opt it into ``public_pages``. "wiki" is
# eligible: the build guide is static reference content (parts, prices, camera
# settings) carrying no station data at all, and is the page a prospective
# operator needs to read *before* they have an account.
#
# Every ``page=`` string passed to ``@public_route`` MUST appear here, or
# ``_drop_unknown_public_pages`` silently discards the config entry and the
# page stays login-gated forever. ``test_every_page_tag_is_eligible`` pins
# this both ways.
PUBLIC_PAGE_KEYS: frozenset[str] = frozenset(
    {"overview", "station", "events", "live", "showers", "highlights", "wiki"}
)


class DashboardConfig(BaseModel):
    station_api_port: int = 7779
    correlation_window_s: int = 1
    stations: dict[str, StationConfig] = Field(default_factory=dict)
    # Runtime on/off switch for which public-capable pages anonymous visitors
    # may reach. Each entry is a stable page key (see PUBLIC_PAGE_KEYS) that
    # maps to one or more ``@public_route(page=...)`` views. The auth gate
    # exposes a page to anon ONLY IF (a) its route is page-tagged eligible AND
    # (b) its key appears here. Removing a key re-gates that page on the next
    # restart — no code deploy. FAIL-CLOSED: unknown keys are dropped (they
    # can never widen access), and a page-tagged route whose key is absent
    # stays login-gated. Default = all eligible pages on (preserves the
    # existing public surface from PR #628 out of the box).
    public_pages: list[str] = Field(default_factory=lambda: sorted(PUBLIC_PAGE_KEYS))

    @field_validator("public_pages", mode="before")
    @classmethod
    def _coerce_null_public_pages(cls, v: object) -> object:
        # A bare ``public_pages:`` YAML key (null) means "none enabled" —
        # fail-closed, not "fall back to default".
        return v if v is not None else []

    @field_validator("public_pages")
    @classmethod
    def _drop_unknown_public_pages(cls, v: list[str]) -> list[str]:
        # Silently drop keys outside the eligible vocabulary. An operator
        # typo or a stale/renamed key can therefore never expose anything —
        # it just resolves to "not enabled". De-duplicate while preserving
        # a stable order for readable round-trips.
        seen: set[str] = set()
        out: list[str] = []
        for key in v:
            if key in PUBLIC_PAGE_KEYS and key not in seen:
                seen.add(key)
                out.append(key)
        return out


class UserConfig(BaseModel):
    display_name: str = ""
    password_hash: str = ""  # empty when the user only signs in via Cloudflare Access
    role: str  # 'admin' | 'host' | 'visitor' | 'press'
    stations: list[str] = Field(default_factory=list)

    @field_validator("stations", mode="before")
    @classmethod
    def _coerce_null_stations(cls, v: object) -> list[str]:
        return v if v is not None else []
    # Werkzeug password hash of the one-time reset token. The plaintext
    # token is shown to the admin once at issuance and never stored —
    # leaking users.yaml no longer leaks an active account-takeover code.
    reset_token: str | None = None
    reset_token_expiry: str | None = None  # ISO8601 UTC
    # Optional Cloudflare Access identity. When set, a verified JWT whose
    # `email` claim matches this value auto-logs the user in (no password).
    # Lookup is case-insensitive — emails are normalised to lowercase before
    # comparison.
    email: str | None = None
    # TOTP MFA. `totp_secret` holds the base32 shared secret once the user
    # has enrolled. `require_totp` defaults to True — every new account
    # is forced to enrol an authenticator on first login. Existing rows
    # without the field pick up the default through Pydantic validation,
    # so legacy users are automatically opted in next time they sign in.
    totp_secret: str | None = None
    require_totp: bool = True
    # Optional one-click magic-login token. Stores a Werkzeug hash of a
    # high-entropy token (plaintext shown to the issuing admin once and
    # delivered out-of-band as a /l/<token> link). A hit on that route
    # establishes a normal 7-day session WITHOUT a password or TOTP — the
    # link itself is the bearer credential. Gated by `expires_at` (the
    # account-level expiry), audit-logged, and scoped by `role`/`stations`.
    # Set to None to revoke. Only minted for `host`-role accounts so a
    # leaked link can never confer admin.
    magic_token: str | None = None
    # Optional account-level expiry (ISO 8601 UTC, e.g. "2026-05-25T18:30:00+00:00").
    # When set and past, _check_credentials() refuses authentication for the
    # account entirely — distinct from reset_token_expiry, which only gates
    # the initial first-login token. Used for tester accounts.
    expires_at: str | None = None
    # Monotonic session-invalidation counter. Stamped into the session at
    # login and re-checked on every authenticated request: an admin bumps it
    # whenever they change this account's role, stations, password, or expiry,
    # so the long-lived (90-day) session cookie can't keep granting the *old*
    # privilege level after a demotion/de-scope. A mismatch forces the live
    # role/stations to be re-read from users.yaml (or the session invalidated).
    # Starts at 0 for legacy rows via the default.
    session_epoch: int = 0
    # Where this user wants fireball alerts by email (set on /alerts). Separate
    # from ``email``, which is a Cloudflare Access sign-in identity: setting
    # that would let anyone with the address's Access login into the account.
    alert_email: str | None = None
