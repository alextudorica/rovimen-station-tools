"""Tests for the station-side calibration-autofit endpoints' wiring into the
readonly gate (station_api.py's _enforce_network_readonly).

Both api_calibration_autofit and api_calibration_autofit_accept are writes
that must be reachable from the network even when the station is in
read-only mode (issue #880's push-first doctrine explicitly sanctions
operator-initiated on-demand actions as a live-call path, distinct from
routine page rendering) -- they need the same explicit-allow carve-out
api_lock's locked:true case gets. api_calibration_autofit_status is a
read and must NOT need any carve-out at all.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / 'rovimen-scripts'))
import station_api as sa  # noqa: E402


# ---------------------------------------------------------------------------
# Endpoint set membership
# ---------------------------------------------------------------------------

def _methods(endpoint: str) -> set[str]:
    return {m for r in sa.app.url_map.iter_rules() if r.endpoint == endpoint
            for m in r.methods} - sa._SAFE_METHODS


class TestEndpointSetMembership:

    def test_autofit_trigger_and_accept_are_mutating(self):
        assert _methods('api_calibration_autofit') == {'POST'}
        assert _methods('api_calibration_autofit_accept') == {'POST'}

    def test_status_endpoint_is_not_mutating(self):
        """A read -- must never require the readonly carve-out."""
        assert _methods('api_calibration_autofit_status') == set()

    def test_autofit_trigger_and_accept_are_network_allowed(self):
        assert 'api_calibration_autofit' in sa._NETWORK_ALLOWED_ENDPOINTS
        assert 'api_calibration_autofit_accept' in sa._NETWORK_ALLOWED_ENDPOINTS

    def test_status_endpoint_not_in_allowed_set_either(self):
        """It doesn't need to be: GETs pass the guard before the allowlist
        is consulted."""
        assert 'api_calibration_autofit_status' not in sa._NETWORK_ALLOWED_ENDPOINTS


# ---------------------------------------------------------------------------
# _enforce_network_readonly -- functional behavior through a real Flask
# request context, mirroring the actual per-request check.
# ---------------------------------------------------------------------------

class TestEnforceNetworkReadonly:
    """Drives real Flask routing via the test client (request.endpoint is a
    read-only property Flask populates from URL matching, not something a
    test can set directly) so _enforce_network_readonly sees exactly what
    it would for a real request. A non-loopback remote address is forced
    via environ_overrides -- the test client defaults to 127.0.0.1, which
    would trivially pass every case regardless of the carve-out."""

    @pytest.fixture
    def client(self):
        # testing=False so an unrelated view-level error (e.g. this dev box
        # has no ~/rovimen_scripts/config.json) becomes a 500 response like
        # it would in production, rather than propagating as a raised
        # exception -- these tests only care whether the readonly gate
        # itself blocked the request (403) or let it through to the view.
        sa.app.testing = False
        return sa.app.test_client()

    def test_autofit_trigger_not_blocked_by_readonly_from_network(self, client):
        # 404 (script not found on this dev box) proves the readonly gate
        # let the request through to the real view -- a 403 would mean the
        # gate blocked it before the view ever ran.
        resp = client.post('/api/calibration/autofit/RO000H',
                            environ_overrides={'REMOTE_ADDR': '203.0.113.5'})
        assert resp.status_code != 403

    def test_autofit_accept_not_blocked_by_readonly_from_network(self, client):
        resp = client.post('/api/calibration/autofit_accept/RO000H', json={'candidate_id': 'x'},
                            environ_overrides={'REMOTE_ADDR': '203.0.113.5'})
        assert resp.status_code != 403

    def test_status_never_gated_read_endpoint(self, client):
        resp = client.get('/api/calibration/autofit_status/RO000H',
                           environ_overrides={'REMOTE_ADDR': '203.0.113.5'})
        assert resp.status_code != 403

    def test_other_mutating_endpoint_still_blocked_from_network(self, client):
        """Regression guard: confirms the new carve-out is scoped to just
        the two autofit endpoints, not accidentally loosened fleet-wide."""
        resp = client.patch('/api/settings', json={'x': 1},
                            environ_overrides={'REMOTE_ADDR': '203.0.113.5'})
        assert resp.status_code == 403

    def test_autofit_trigger_allowed_from_loopback_regardless(self, client):
        resp = client.post('/api/calibration/autofit/RO000H',
                            environ_overrides={'REMOTE_ADDR': '127.0.0.1'})
        assert resp.status_code != 403


class TestRebasePointingEndpointGating:
    """Re-base stages a candidate exactly as the autofit trigger does, so it
    needs the same network carve-out. It deliberately does NOT take the
    capture-window guard: it runs no fit and burns no sustained CPU, so there
    is nothing for it to starve capture with."""

    def test_is_declared_mutating(self):
        assert _methods('api_calibration_rebase_pointing') == {'POST'}

    def test_is_network_allowed(self):
        assert 'api_calibration_rebase_pointing' in sa._NETWORK_ALLOWED_ENDPOINTS

    def test_shares_the_autofit_single_flight_guard(self):
        """Both write into the same candidate directory, so one must not run
        while the other is in flight."""
        import inspect
        src = inspect.getsource(sa.api_calibration_rebase_pointing)
        assert '_autofit_run_in_flight' in src

    def test_does_not_take_the_capture_window_guard(self):
        import inspect
        src = inspect.getsource(sa.api_calibration_rebase_pointing)
        assert '_camera_is_capturing' not in src
