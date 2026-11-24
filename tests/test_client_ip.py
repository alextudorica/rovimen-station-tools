"""``security._client_ip`` finds the real client behind the VPS's local proxies.

It keys every per-IP rate limit (login, magic link, TOTP, the keyless public
routes, /media/v1). On the VPS ProxyFix leaves ``remote_addr`` at 127.0.0.1 for
all traffic that came through cloudflared or Tailscale Serve and then nginx, so
returning ``remote_addr`` made every per-IP limit one global bucket: 60 bad
logins an hour from anyone locked every operator out.
"""

from __future__ import annotations

import pytest
from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

import security

# Bound at collection, before any app fixture swaps it for a disabled limiter.
_init_limiter = security.init_limiter


@pytest.fixture
def app() -> Flask:
    return Flask(__name__)


def _ip(app: Flask, remote: str | None, **headers: str) -> str:
    environ = {"REMOTE_ADDR": remote} if remote is not None else {}
    hdrs = {k.replace("_", "-"): v for k, v in headers.items()}
    with app.test_request_context("/", environ_base=environ, headers=hdrs):
        return security._client_ip()


class TestBehindLocalProxy:
    """remote_addr is loopback: cloudflared or Tailscale Serve, then nginx."""

    def test_cloudflare_visitor(self, app):
        assert _ip(app, "127.0.0.1", CF_Connecting_IP="93.184.216.34",
                   X_Forwarded_For="93.184.216.34, 127.0.0.1") == "93.184.216.34"

    def test_cloudflare_visitor_cannot_forge_by_sending_its_own_xff(self, app):
        # Cloudflare appends the address it saw after whatever the client sent.
        assert _ip(app, "127.0.0.1", CF_Connecting_IP="93.184.216.34",
                   X_Forwarded_For="203.0.113.50, 93.184.216.34, 127.0.0.1") == "93.184.216.34"

    def test_cf_connecting_ip_when_no_forwarded_hop_names_the_client(self, app):
        assert _ip(app, "127.0.0.1", CF_Connecting_IP="93.184.216.34",
                   X_Forwarded_For="127.0.0.1") == "93.184.216.34"
        assert _ip(app, "127.0.0.1", CF_Connecting_IP="93.184.216.34") == "93.184.216.34"

    def test_tailscale_serve_peer(self, app):
        assert _ip(app, "127.0.0.1",
                   X_Forwarded_For="100.64.0.4, 127.0.0.1") == "100.64.0.4"

    def test_tailscale_serve_peer_cannot_dodge_limits_with_a_cf_header(self, app):
        assert _ip(app, "127.0.0.1", CF_Connecting_IP="198.51.100.9",
                   X_Forwarded_For="100.64.0.4, 127.0.0.1") == "100.64.0.4"

    def test_tailscale_serve_peer_over_ipv6(self, app):
        assert _ip(app, "127.0.0.1", CF_Connecting_IP="198.51.100.9",
                   X_Forwarded_For="fd7a:115c:a1e0::1234, 127.0.0.1") == "fd7a:115c:a1e0::1234"

    def test_garbage_and_private_hops_are_skipped(self, app):
        assert _ip(app, "127.0.0.1",
                   X_Forwarded_For='93.184.216.34, "><script>, 10.0.0.5, 127.0.0.1') == "93.184.216.34"

    def test_local_process_without_headers(self, app):
        assert _ip(app, "127.0.0.1") == "127.0.0.1"
        assert _ip(app, "::1", CF_Connecting_IP="93.184.216.34") == "93.184.216.34"


class TestDirectClient:
    """remote_addr is not loopback: the client reached nginx itself."""

    def test_tailnet_peer_direct_ignores_every_header(self, app):
        assert _ip(app, "100.64.0.4", CF_Connecting_IP="198.51.100.9",
                   X_Forwarded_For="203.0.113.50") == "100.64.0.4"

    def test_no_remote_addr(self, app):
        assert _ip(app, None) == "-"


def test_limiter_buckets_two_clients_behind_nginx_separately(monkeypatch):
    """The production wiring: ProxyFix(x_for=1) then the limiter's default key."""
    monkeypatch.delenv("ROVIMEN_REDIS_URL", raising=False)
    monkeypatch.delenv("ROVIMEN_CF_AUD", raising=False)
    app = Flask(__name__)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1, x_for=1)  # type: ignore[method-assign]
    limiter = _init_limiter(app)

    @app.route("/login", methods=["POST"])
    @limiter.limit("2 per minute")
    def login():
        return "ok"

    client = app.test_client()

    def post(client_ip: str) -> int:
        # nginx appends the address it saw (cloudflared / Serve on loopback).
        return client.post("/login", environ_base={"REMOTE_ADDR": "127.0.0.1"},
                           headers={"X-Forwarded-For": f"{client_ip}, 127.0.0.1"}).status_code

    assert [post("93.184.216.34") for _ in range(3)] == [200, 200, 429]
    assert post("100.64.0.4") == 200
    assert post("2606:2800:220:1::7") == 200
