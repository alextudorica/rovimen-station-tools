"""Tests for routes.station_ops.station_json_payload.

A station that answers is not necessarily a station that answers in JSON.
The case that matters: for the hours between a dashboard deploy and a
station's next updater window, the station runs a bundle that predates the
endpoint being called and replies with Flask's HTML 404 page. Calling
``.json()`` on that raised out of the view and the dashboard returned its own
HTML 500 — so the mask editor, which reads ``error`` out of a JSON body, could
only say "unknown error" for the single failure an operator is most likely to
hit. Caught by probing the live fleet on 2026-09-21 while gmnro02 was still on
bundle 277.
"""

from __future__ import annotations

import pytest

from routes.station_ops import station_json_payload


class _Resp:
    """requests.Response stand-in: JSON when *json_body* is set, otherwise
    text that raises on .json() the way requests does."""

    def __init__(self, status_code: int, json_body=None, text: str = ""):
        self.status_code = status_code
        self._json = json_body
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._json


def test_json_object_passes_through_with_its_status():
    payload, status = station_json_payload(_Resp(200, {"ok": True, "masked_pct": 3.1}))
    assert payload == {"ok": True, "masked_pct": 3.1}
    assert status == 200


def test_json_error_body_keeps_the_stations_status():
    payload, status = station_json_payload(_Resp(409, {"error": "already in progress"}))
    assert payload["error"] == "already in progress"
    assert status == 409


def test_html_404_becomes_a_named_json_error():
    resp = _Resp(404, text="<!doctype html>\n<title>404 Not Found</title>\n")
    payload, status = station_json_payload(resp)
    assert status == 404
    assert "404" in payload["error"]
    assert "not JSON" in payload["error"]
    assert "404 Not Found" in payload["detail"]


def test_empty_body_still_yields_a_usable_message():
    payload, status = station_json_payload(_Resp(502, text="   "))
    assert status == 502
    assert payload["detail"] == "empty response body"


def test_long_html_body_is_truncated():
    payload, _ = station_json_payload(_Resp(500, text="x" * 5000))
    assert len(payload["detail"]) == 200


def test_a_bare_json_scalar_is_wrapped_so_the_caller_always_gets_an_object():
    """jsonify of a bare list/scalar is awkward for callers that do
    ``result.error``; wrap it rather than letting the shape vary."""
    payload, status = station_json_payload(_Resp(200, json_body=[1, 2, 3]))
    assert payload == {"value": [1, 2, 3]}
    assert status == 200


@pytest.mark.parametrize("status", [200, 400, 404, 409, 500, 502])
def test_the_stations_status_is_never_replaced(status):
    _, got = station_json_payload(_Resp(status, text="not json"))
    assert got == status
