"""Auth hardening: TOTP enrolment, magic-link role, password-change logout.

Three gaps, each driven through the real Flask handlers over an in-memory
users store:

* ``/totp/setup`` is reachable with only ``pending_login`` (a correct
  password, or a Cloudflare identity). It used to render the account's
  *stored* TOTP secret, so one factor could read and then satisfy the second.
  It now only enrols an account with no secret, and never for a magic-link
  session.
* ``/api/auth/status`` re-reads role/stations into the session on every page
  load. It used to copy the account's own role, undoing the read-only
  downgrade a magic-link session gets at redemption.
* A password change used to leave every other session (90-day cookies)
  signed in. Sessions now carry a fingerprint of the password hash and
  ``revalidate_session`` clears one whose fingerprint no longer matches.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pyotp
import pytest
from flask import Flask, jsonify, session
from werkzeug.security import generate_password_hash

import auth as auth_mod
import routes.auth_routes as auth_routes
import security
from models import UserConfig
from routes.auth_routes import _MAGIC_SESSION_ROLE, register_auth_routes

_ENROLLED_SECRET = "JBSWY3DPEHPK3PXP"


class _NoopLimiter:
    def limit(self, *_args, **_kwargs):
        def _decorator(fn):
            return fn
        return _decorator


@pytest.fixture
def store(monkeypatch) -> dict[str, UserConfig]:
    """One in-memory users.yaml shared by every module that reads or writes it."""
    users: dict[str, UserConfig] = {}

    def _update(mutator) -> None:
        mutator(users)

    for mod in (auth_mod, auth_routes):
        monkeypatch.setattr(mod, "_load_users", lambda: users)
    monkeypatch.setattr(auth_routes, "_update_users", _update)
    monkeypatch.setattr(security, "audit_request", lambda *a, **k: None)
    monkeypatch.setattr(security, "is_user_locked", lambda u: False)
    monkeypatch.setattr(security, "record_login_failure", lambda u: False)
    monkeypatch.setattr(security, "clear_login_failures", lambda u: None)
    return users


@pytest.fixture
def app(store) -> Flask:
    """The auth routes behind the real revalidation hook and auth gate, in
    create_app's order, plus two protected stand-in routes."""
    templates = os.path.join(os.path.dirname(auth_mod.__file__), "templates")
    app = Flask(__name__, template_folder=templates)
    app.secret_key = "test-secret"
    app.jinja_env.globals["csrf_token"] = lambda: "test-csrf"
    app._limiter = _NoopLimiter()  # type: ignore[attr-defined]

    @app.route("/")
    def index():
        return "home"

    @app.route("/api/whoami")
    def whoami():
        return jsonify(user=session.get("user"), role=session.get("role"))

    @app.route("/api/station/<host_key>/mask", methods=["GET", "POST"])
    @auth_mod.require_station
    def station_mask(host_key: str):
        return jsonify(ok=True)

    auth_mod.install_session_revalidation(app)
    security.install_auth_gate(app)
    register_auth_routes(app, config=None, cache=None, tunnels=None)
    return app


def _magic_session(sess, user: str, stations: list[str]) -> None:
    sess["user"] = user
    sess["role"] = _MAGIC_SESSION_ROLE
    sess["stations"] = stations
    sess["admin"] = None
    sess["magic"] = True
    sess["magic_exp"] = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()


# ── 1. /totp/setup never discloses or re-confirms an existing secret ────────


def test_password_only_session_cannot_see_existing_secret(app, store):
    store["bob"] = UserConfig(role="host", totp_secret=_ENROLLED_SECRET, require_totp=True)
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["pending_login"] = "bob"   # what /login sets after a correct password

    resp = c.get("/totp/setup")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/totp/verify")
    assert _ENROLLED_SECRET not in resp.get_data(as_text=True)


def test_password_only_session_cannot_use_existing_secret(app, store):
    store["bob"] = UserConfig(role="host", totp_secret=_ENROLLED_SECRET, require_totp=True)
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["pending_login"] = "bob"

    resp = c.post("/totp/setup", data={"code": pyotp.TOTP(_ENROLLED_SECRET).now()})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/totp/verify")
    with c.session_transaction() as sess:
        assert "user" not in sess          # no session from the setup page
    assert store["bob"].totp_secret == _ENROLLED_SECRET


def test_cloudflare_pending_session_cannot_see_existing_secret(app, store):
    store["bob"] = UserConfig(role="host", totp_secret=_ENROLLED_SECRET, require_totp=True)
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["pending_login"] = "bob"
        sess["cf_pending"] = True

    resp = c.get("/totp/setup")
    assert resp.status_code == 302
    assert _ENROLLED_SECRET not in resp.get_data(as_text=True)


def test_signed_in_enrolled_account_is_sent_home(app, store):
    store["bob"] = UserConfig(role="host", totp_secret=_ENROLLED_SECRET, require_totp=True)
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["user"] = "bob"
        sess["role"] = "host"

    resp = c.get("/totp/setup")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/")
    assert _ENROLLED_SECRET not in resp.get_data(as_text=True)


def test_first_time_enrolment_still_works(app, store):
    store["bob"] = UserConfig(role="host", stations=["gmnro13"], require_totp=True)
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["pending_login"] = "bob"

    resp = c.get("/totp/setup")
    assert resp.status_code == 200
    with c.session_transaction() as sess:
        proposed = sess["totp_setup_secret"]
    assert proposed in resp.get_data(as_text=True)

    resp = c.post("/totp/setup", data={"code": pyotp.TOTP(proposed).now()})
    assert resp.status_code == 302
    assert store["bob"].totp_secret == proposed
    with c.session_transaction() as sess:
        assert sess["user"] == "bob"
        assert sess["mfa"] == "totp"
        assert "pending_login" not in sess


def test_enrolment_post_without_a_proposed_secret_is_refused(app, store):
    store["bob"] = UserConfig(role="host", require_totp=True)
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["pending_login"] = "bob"

    resp = c.post("/totp/setup", data={"code": "123456"})
    assert resp.status_code == 302
    assert store["bob"].totp_secret is None


def test_magic_session_cannot_enrol(app, store):
    store["hostuser"] = UserConfig(role="host", stations=["gmnro13"])
    c = app.test_client()
    with c.session_transaction() as sess:
        _magic_session(sess, "hostuser", ["gmnro13"])
        sess["totp_setup_secret"] = _ENROLLED_SECRET   # even with one planted

    resp = c.get("/totp/setup")
    assert resp.status_code == 302
    assert _ENROLLED_SECRET not in resp.get_data(as_text=True)

    resp = c.post("/totp/setup", data={"code": pyotp.TOTP(_ENROLLED_SECRET).now()})
    assert resp.status_code == 302
    assert store["hostuser"].totp_secret is None


# ── 2. /api/auth/status keeps a magic session read-only ─────────────────────


def test_magic_session_stays_read_only_after_auth_status(app, store):
    store["hostuser"] = UserConfig(role="host", stations=["gmnro13", "gmnro14"])
    c = app.test_client()
    with c.session_transaction() as sess:
        _magic_session(sess, "hostuser", ["gmnro13"])

    body = c.get("/api/auth/status").get_json()
    assert body["role"] == _MAGIC_SESSION_ROLE
    assert body["admin"] is False
    assert body["stations"] == ["gmnro13", "gmnro14"]   # assignments still refresh
    with c.session_transaction() as sess:
        assert sess["role"] == _MAGIC_SESSION_ROLE
        assert sess["admin"] is None

    # The host's write gate stays shut for the link session.
    assert c.post("/api/station/gmnro13/mask", json={}).status_code == 403


def test_normal_host_session_still_refreshes_role_and_can_write(app, store):
    store["hostuser"] = UserConfig(role="host", stations=["gmnro13"])
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["user"] = "hostuser"
        sess["role"] = "visitor"        # stale: promoted to host since login
        sess["stations"] = []

    body = c.get("/api/auth/status").get_json()
    assert body["role"] == "host"
    assert body["stations"] == ["gmnro13"]
    assert c.post("/api/station/gmnro13/mask", json={}).status_code == 200


# ── 3. A password change ends every other session ───────────────────────────


def _login(app: Flask, username: str, password: str):
    c = app.test_client()
    resp = c.post("/login", data={"username": username, "password": password})
    assert resp.status_code == 302, resp.get_data(as_text=True)
    return c


def test_set_password_ends_other_sessions_and_keeps_its_own(app, store):
    store["bob"] = UserConfig(role="host", stations=["gmnro02"], require_totp=False,
                              password_hash=generate_password_hash("old-password-123456"))
    other = _login(app, "bob", "old-password-123456")
    assert other.get("/api/whoami").get_json()["user"] == "bob"

    # Admin issues a reset token; the owner redeems it in a second browser.
    token = "reset-token-value-abcdef"
    store["bob"].reset_token = generate_password_hash(token)
    store["bob"].reset_token_expiry = (
        datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    changer = app.test_client()
    resp = changer.post("/login", data={"username": "bob", "password": token})
    assert resp.headers["Location"].endswith("/set-password")
    resp = changer.post("/set-password", data={"password": "new-password-123456",
                                               "confirm": "new-password-123456"})
    assert resp.status_code == 302

    assert changer.get("/api/whoami").status_code == 200    # changing session stays
    assert other.get("/api/whoami").status_code == 401      # the other one is ended
    with other.session_transaction() as sess:
        assert "user" not in sess


def test_admin_password_change_ends_other_sessions_but_not_the_changer(app, store, monkeypatch):
    import rovimen_dashboard as rd
    from admin_config import register_admin_user_routes

    monkeypatch.setattr(rd, "_load_users", lambda: store)
    monkeypatch.setattr(rd, "_update_users", lambda mutator: mutator(store))
    register_admin_user_routes(app)
    store["operator"] = UserConfig(role="admin", require_totp=False,
                               password_hash=generate_password_hash("admin-pw-1234567890"))
    store["bob"] = UserConfig(role="host", stations=["gmnro02"], require_totp=False,
                              password_hash=generate_password_hash("bob-pw-1234567890"))
    changer = _login(app, "operator", "admin-pw-1234567890")
    other_admin = _login(app, "operator", "admin-pw-1234567890")
    bob = _login(app, "bob", "bob-pw-1234567890")

    # The admin changes their own password: this session stays, the other ends.
    assert changer.patch("/api/admin/users/operator",
                         json={"password": "admin-pw-new-123456"}).status_code == 200
    assert changer.get("/api/whoami").status_code == 200
    assert other_admin.get("/api/whoami").status_code == 401

    # An admin setting someone else's password ends that account's sessions.
    assert changer.patch("/api/admin/users/bob",
                         json={"password": "bob-pw-new-1234567"}).status_code == 200
    assert bob.get("/api/whoami").status_code == 401
    assert changer.get("/api/whoami").status_code == 200


def test_session_without_fingerprint_survives_and_is_stamped(app, store):
    store["bob"] = UserConfig(role="host", stations=["gmnro02"], require_totp=False,
                              password_hash=generate_password_hash("pw-1234567890abc"))
    c = app.test_client()
    with c.session_transaction() as sess:     # minted before the fingerprint existed
        sess["user"] = "bob"
        sess["role"] = "host"
        sess["stations"] = ["gmnro02"]
        sess["session_epoch"] = 0

    assert c.get("/api/whoami").status_code == 200
    with c.session_transaction() as sess:
        assert sess["pw_fp"] == auth_mod.password_fingerprint(store["bob"].password_hash)
    assert c.get("/api/whoami").status_code == 200


def test_untagged_session_is_cleared_when_the_epoch_moved(app, store):
    """An untagged session that sat idle across a password change must not
    adopt the new tag: the change bumped the epoch, so it is cleared."""
    store["bob"] = UserConfig(role="host", stations=["gmnro02"], require_totp=False,
                              password_hash=generate_password_hash("pw-1234567890abc"),
                              session_epoch=3)
    c = app.test_client()
    with c.session_transaction() as sess:     # minted at epoch 2, before the tag
        sess["user"] = "bob"
        sess["role"] = "host"
        sess["stations"] = ["gmnro02"]
        sess["session_epoch"] = 2

    assert c.get("/api/whoami").status_code == 401
    with c.session_transaction() as sess:
        assert "pw_fp" not in sess
        assert "user" not in sess


def test_login_and_magic_link_stamp_the_fingerprint(app, store):
    token = "magic-token-value"
    store["bob"] = UserConfig(role="host", stations=["gmnro02"], require_totp=False,
                              password_hash=generate_password_hash("pw-1234567890abc"),
                              magic_token=generate_password_hash(token))
    fp = auth_mod.password_fingerprint(store["bob"].password_hash)
    c = _login(app, "bob", "pw-1234567890abc")
    with c.session_transaction() as sess:
        assert sess["pw_fp"] == fp
    m = app.test_client()
    assert m.get(f"/l/{token}").status_code == 302
    with m.session_transaction() as sess:
        assert sess["magic"] is True
        assert sess["pw_fp"] == fp

    # A later password change ends the magic session too.
    store["bob"].password_hash = generate_password_hash("another-pw-123456")
    assert m.get("/api/whoami").status_code == 401


def test_cloudflare_full_session_stamps_the_fingerprint(monkeypatch):
    u = UserConfig(role="host", stations=["gmnro02"], require_totp=False,
                   password_hash=generate_password_hash("pw-1234567890abc"))
    monkeypatch.setattr(auth_mod.cf_access, "verify_request_token",
                        lambda headers, cookies: {"email": "u@example.com"})
    monkeypatch.setattr(auth_mod, "_find_user_by_email", lambda e: ("u", u))
    app = Flask(__name__)
    app.secret_key = "test-secret"
    with app.test_request_context("/"):
        auth_mod._cf_access_before_request()
        assert session["user"] == "u"
        assert session["pw_fp"] == auth_mod.password_fingerprint(u.password_hash)


def test_fingerprint_changes_with_every_new_hash():
    a = generate_password_hash("same-password-123")
    b = generate_password_hash("same-password-123")   # new salt
    assert auth_mod.password_fingerprint(a) != auth_mod.password_fingerprint(b)
    assert auth_mod.password_fingerprint(a) == auth_mod.password_fingerprint(a)
    assert a not in auth_mod.password_fingerprint(a)
