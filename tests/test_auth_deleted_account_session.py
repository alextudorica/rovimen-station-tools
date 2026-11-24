"""Regression test: deleting an account must end its live session.

Session cookies are client-side and live 90 days. ``revalidate_session()``
used to return early whenever the username had no users.yaml row, so a deleted
account kept browsing on its login-time role/stations snapshot until the cookie
expired on its own — observed in production, where an account removed in August
was still serving requests in September and only its magic link 401'd.

The exemption that early return was protecting is the legacy env-var admin,
which legitimately has no file row. These tests pin both halves, plus the
fail-open guard that stops an unreadable users.yaml logging out the fleet.
"""

from __future__ import annotations

import pytest
from flask import Flask, session

import auth
from models import UserConfig


@pytest.fixture
def app(monkeypatch):
    app = Flask(__name__)
    app.secret_key = "test-secret"

    @app.before_request
    def _revalidate():
        auth.revalidate_session()

    @app.route("/probe")
    def probe():
        return {"user": session.get("user"), "role": session.get("role")}

    # No env-var admins unless a test opts in.
    for i in (1, 2):
        monkeypatch.delenv(f"ROVIMEN_ADMIN_USER_{i}", raising=False)
        monkeypatch.delenv(f"ROVIMEN_ADMIN_PASS_{i}", raising=False)
    return app


def _seed_users(monkeypatch, **users: UserConfig) -> None:
    monkeypatch.setattr(auth, "_load_users", lambda: dict(users))


def _login(client, username: str, **extra) -> None:
    with client.session_transaction() as sess:
        sess["user"] = username
        sess["role"] = extra.get("role", "host")
        sess["stations"] = extra.get("stations", [])
        sess["session_epoch"] = extra.get("session_epoch", 0)
        if "pw_fp" in extra:
            sess["pw_fp"] = extra["pw_fp"]


def test_deleted_account_session_is_cleared(app, monkeypatch):
    """The row is gone from users.yaml — the cookie must stop authenticating."""
    _seed_users(monkeypatch, someone_else=UserConfig(role="host"))
    client = app.test_client()
    _login(client, "host_braila", role="host", stations=["gmnro20"])

    assert client.get("/probe").get_json() == {"user": None, "role": None}


def test_live_account_session_survives(app, monkeypatch):
    """The ordinary case must not regress: a real account stays signed in."""
    _seed_users(monkeypatch,
                host_braila=UserConfig(role="host", stations=["gmnro20"]))
    client = app.test_client()
    _login(client, "host_braila", role="host", stations=["gmnro20"])

    body = client.get("/probe").get_json()
    assert body == {"user": "host_braila", "role": "host"}


def test_env_var_admin_is_exempt(app, monkeypatch):
    """Env-var admins authenticate from the environment and own no file row."""
    monkeypatch.setenv("ROVIMEN_ADMIN_USER_1", "operator")
    monkeypatch.setenv("ROVIMEN_ADMIN_PASS_1", "irrelevant-here")
    _seed_users(monkeypatch, someone_else=UserConfig(role="host"))
    client = app.test_client()
    _login(client, "operator", role="admin")

    body = client.get("/probe").get_json()
    assert body == {"user": "operator", "role": "admin"}


def test_half_configured_env_admin_slot_is_not_an_account(app, monkeypatch):
    """A username set with no password is a deployment mistake, not a login."""
    monkeypatch.setenv("ROVIMEN_ADMIN_USER_1", "operator")
    monkeypatch.delenv("ROVIMEN_ADMIN_PASS_1", raising=False)
    _seed_users(monkeypatch, someone_else=UserConfig(role="host"))
    client = app.test_client()
    _login(client, "operator", role="admin")

    assert client.get("/probe").get_json() == {"user": None, "role": None}


def test_unreadable_users_file_does_not_log_everyone_out(app, monkeypatch):
    """_load_users() returns {} when users.yaml is missing — fail open."""
    _seed_users(monkeypatch)  # empty map
    client = app.test_client()
    _login(client, "host_braila", role="host", stations=["gmnro20"])

    body = client.get("/probe").get_json()
    assert body == {"user": "host_braila", "role": "host"}


def test_epoch_refresh_still_applies_to_live_accounts(app, monkeypatch):
    """The pre-existing privilege-refresh path must keep working."""
    _seed_users(monkeypatch,
                host_braila=UserConfig(role="host", stations=["gmnro20"],
                                       session_epoch=3))
    client = app.test_client()
    _login(client, "host_braila", role="host", stations=[], session_epoch=1,
           pw_fp=auth.password_fingerprint(""))

    client.get("/probe")
    with client.session_transaction() as sess:
        assert sess["stations"] == ["gmnro20"]
        assert sess["session_epoch"] == 3


def _login_magic(client, username: str, session_epoch: int) -> None:
    with client.session_transaction() as sess:
        sess["user"] = username
        sess["role"] = auth.MAGIC_SESSION_ROLE
        sess["stations"] = ["gmnro22"]
        sess["session_epoch"] = session_epoch
        sess["pw_fp"] = auth.password_fingerprint("")  # tagged session
        sess["magic"] = True


def test_epoch_refresh_keeps_magic_session_read_only(app, monkeypatch):
    """An admin edit to a host account must not lift its link session to host."""
    _seed_users(monkeypatch,
                hostuser2=UserConfig(role="host", stations=["gmnro22", "gmnro20"],
                                  session_epoch=2))
    client = app.test_client()
    _login_magic(client, "hostuser2", session_epoch=1)

    body = client.get("/probe").get_json()
    assert body == {"user": "hostuser2", "role": auth.MAGIC_SESSION_ROLE}
    with client.session_transaction() as sess:
        assert sess["stations"] == ["gmnro22", "gmnro20"]
        assert sess["admin"] is None
        assert sess["session_epoch"] == 2


def test_epoch_refresh_ends_magic_session_of_promoted_admin(app, monkeypatch):
    """magic_login refuses admin accounts, so a promotion ends the link session."""
    _seed_users(monkeypatch,
                hostuser2=UserConfig(role="admin", session_epoch=2))
    client = app.test_client()
    _login_magic(client, "hostuser2", session_epoch=1)

    assert client.get("/probe").get_json() == {"user": None, "role": None}
