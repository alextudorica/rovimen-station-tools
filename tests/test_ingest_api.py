"""Tests for dashboard/ingest_api.py — reversed-HTTP push ingest endpoints.

Builds a minimal Flask app that mounts only the ingest routes (mirroring how
create_app wires them) with the DB/cache/key paths pointed at tmp files, so the
handlers exercise the real station_keys / station_state / detection_db code.
"""

from __future__ import annotations

import base64
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from flask import Flask

import detection_db
import ingest_api
import station_keys
import station_state as ss


class _FakeCache:
    """Stand-in for StationCache.set_status/set_vitals — records the writes so
    tests can assert a pushed heartbeat lands in the same cache the pollers use."""

    def __init__(self) -> None:
        self.status: dict[str, dict[str, Any]] = {}
        self.vitals: dict[str, dict[str, Any]] = {}
        self.live_thumbs: dict[tuple[str, str], dict[str, Any]] = {}

    def set_status(self, host_key: str, data: dict[str, Any]) -> None:
        self.status[host_key] = data

    def set_vitals(self, host_key: str, data: dict[str, Any]) -> None:
        self.vitals[host_key] = data

    def set_live_thumb(self, host_key: str, cam: str, data: bytes,
                       *, content_type: str = "image/webp",
                       ff_timestamp: str | None = None) -> None:
        self.live_thumbs[(host_key, cam)] = {
            "data": data, "content_type": content_type,
            "ff_timestamp": ff_timestamp,
        }


@pytest.fixture(autouse=True)
def _require_keys(monkeypatch):
    """Force key enforcement on (production default) for the auth tests."""
    monkeypatch.setenv("ROVIMEN_STATION_KEYS_REQUIRED", "1")
    station_keys._invalidate_cache()
    yield
    station_keys._invalidate_cache()


@pytest.fixture
def env(tmp_path: Path):
    keys_path = tmp_path / "station_keys.yaml"
    det_path = tmp_path / "detections.db"
    state_path = tmp_path / "state.db"
    archive_root = tmp_path / "archive"
    cache = _FakeCache()

    # Mint one key for gmn0002.
    _, secret = station_keys.add_key("gmn0002", path=keys_path)
    station_keys._invalidate_cache()

    app = Flask(__name__)
    app.testing = True
    ingest_api.register_ingest_routes(
        app,
        cache_set_status=cache.set_status,
        cache_set_vitals=cache.set_vitals,
        cache_set_live_thumb=cache.set_live_thumb,
        known_stations=lambda: {"gmn0002", "gmnro10"},
        detections_db_path=det_path,
        station_state_path=state_path,
        station_keys_path=keys_path,
        archive_media_path=archive_root,
        blob_spool_dir=tmp_path / "spool",
        limiter=None,
    )
    client = app.test_client()
    return {
        "client": client,
        "app": app,
        "secret": secret,
        "keys_path": keys_path,
        "det_path": det_path,
        "state_path": state_path,
        "archive_root": archive_root,
        "cache": cache,
    }


@pytest.fixture
def redis_env(tmp_path: Path, monkeypatch):
    """Same wiring as ``env`` but with the seq guard routed through a shared
    fakeredis backend, so the ingest handlers exercise the Redis seq path while
    the media pointers stay in SQLite."""
    fakeredis = pytest.importorskip("fakeredis")
    import cache_backend

    keys_path = tmp_path / "station_keys.yaml"
    det_path = tmp_path / "detections.db"
    state_path = tmp_path / "state.db"
    cache = _FakeCache()

    _, secret = station_keys.add_key("gmn0002", path=keys_path)
    station_keys._invalidate_cache()

    # Force the station_state seq guard onto fakeredis for the duration.
    client = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr(
        cache_backend,
        "make_seq_backend",
        lambda redis_url=None: cache_backend.RedisStationStateBackend(
            client, prefix="ingest", ttl_seconds=0
        ),
    )
    ss.reset_seq_backend_for_tests()

    app = Flask(__name__)
    app.testing = True
    ingest_api.register_ingest_routes(
        app,
        cache_set_status=cache.set_status,
        cache_set_vitals=cache.set_vitals,
        cache_set_live_thumb=cache.set_live_thumb,
        known_stations=lambda: {"gmn0002", "gmnro10"},
        detections_db_path=det_path,
        station_state_path=state_path,
        station_keys_path=keys_path,
        limiter=None,
    )
    client_app = app.test_client()
    yield {
        "client": client_app,
        "secret": secret,
        "state_path": state_path,
        "det_path": det_path,
        "cache": cache,
        "redis": client,
    }
    ss.reset_seq_backend_for_tests()


def _sqlite_last_seq(state_path: Path, host_key: str) -> int:
    """Read the raw SQLite last_seq column directly (0 if the row/DB is absent).

    Used to prove the per-push seq write did NOT land in SQLite on the Redis
    path — the whole point of the fix.
    """
    import sqlite3

    if not Path(state_path).exists():
        return 0
    con = sqlite3.connect(str(state_path))
    try:
        row = con.execute(
            "SELECT last_seq FROM station_state WHERE host_key=?", (host_key,)
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    finally:
        con.close()
    return int(row[0]) if row and row[0] is not None else 0


def _hdr(secret: str) -> dict[str, str]:
    return {"X-Station-Key": secret}


def _envelope(seq: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {"seq": seq, "sent_at": "2026-07-01T22:00:00Z", "payload": payload}


# ── discovery index ─────────────────────────────────────────────────────


def test_index_is_open(env):
    r = env["client"].get("/api/ingest/v1")
    assert r.status_code == 200
    assert r.get_json()["service"] == "rovimen-ingest-api"


# ── auth ─────────────────────────────────────────────────────────────────


class TestAuth:
    def test_missing_key_401(self, env):
        r = env["client"].post(
            "/api/ingest/v1/gmn0002/heartbeat", json=_envelope(1, {"online": True})
        )
        assert r.status_code == 401
        assert r.get_json()["error"] == "missing_or_invalid_station_key"

    def test_invalid_key_401(self, env):
        r = env["client"].post(
            "/api/ingest/v1/gmn0002/heartbeat",
            json=_envelope(1, {"online": True}),
            headers=_hdr("rvmn_gmn0002_deadbeef"),
        )
        assert r.status_code == 401

    def test_wrong_station_403(self, env):
        """gmn0002's key POSTing to gmnro10 → 403 (core §2.2 property)."""
        r = env["client"].post(
            "/api/ingest/v1/gmnro10/heartbeat",
            json=_envelope(1, {"online": True}),
            headers=_hdr(env["secret"]),
        )
        assert r.status_code == 403
        assert r.get_json()["error"] == "station_mismatch"

    def test_bearer_token_accepted(self, env):
        r = env["client"].post(
            "/api/ingest/v1/gmn0002/heartbeat",
            json=_envelope(1, {"online": True}),
            headers={"Authorization": f"Bearer {env['secret']}"},
        )
        assert r.status_code == 200

    def test_query_key_param_rejected(self, env):
        r = env["client"].post(
            f"/api/ingest/v1/gmn0002/heartbeat?key={env['secret']}",
            json=_envelope(1, {"online": True}),
        )
        assert r.status_code == 400
        assert r.get_json()["error"] == "url_key_param_disabled"

    def test_unknown_station_404(self, env):
        r = env["client"].post(
            "/api/ingest/v1/gmn9999/heartbeat",
            json=_envelope(1, {"online": True}),
            headers=_hdr(env["secret"]),
        )
        assert r.status_code == 404

    def test_bad_station_id_404(self, env):
        r = env["client"].post(
            "/api/ingest/v1/BAD-ID/heartbeat",
            json=_envelope(1, {"online": True}),
            headers=_hdr(env["secret"]),
        )
        assert r.status_code == 404


# ── heartbeat ──────────────────────────────────────────────────────────────


class TestHeartbeat:
    def test_writes_to_cache_and_state(self, env):
        r = env["client"].post(
            "/api/ingest/v1/gmn0002/heartbeat",
            json=_envelope(5, {"online": True, "rms_running": True}),
            headers=_hdr(env["secret"]),
        )
        assert r.status_code == 200
        assert r.get_json()["dropped"] is False
        # Same cache the poll thread writes.
        assert env["cache"].status["gmn0002"]["online"] is True
        assert env["cache"].status["gmn0002"]["rms_running"] is True
        # Seq advanced.
        assert ss.get_last_seq("gmn0002", path=env["state_path"]) == 5

    def test_replay_dropped(self, env):
        c, s = env["client"], env["secret"]
        c.post("/api/ingest/v1/gmn0002/heartbeat", json=_envelope(5, {"online": True}), headers=_hdr(s))
        r = c.post("/api/ingest/v1/gmn0002/heartbeat", json=_envelope(5, {"online": False}), headers=_hdr(s))
        assert r.status_code == 200
        assert r.get_json()["dropped"] is True
        # Cache not overwritten by the replay.
        assert env["cache"].status["gmn0002"]["online"] is True

    def test_older_seq_dropped(self, env):
        c, s = env["client"], env["secret"]
        c.post("/api/ingest/v1/gmn0002/heartbeat", json=_envelope(10, {"online": True}), headers=_hdr(s))
        r = c.post("/api/ingest/v1/gmn0002/heartbeat", json=_envelope(4, {"online": False}), headers=_hdr(s))
        assert r.get_json()["dropped"] is True

    def test_malformed_envelope_422(self, env):
        r = env["client"].post(
            "/api/ingest/v1/gmn0002/heartbeat",
            json={"payload": {"online": True}},  # missing seq
            headers=_hdr(env["secret"]),
        )
        assert r.status_code == 422

    def test_heartbeat_string_services_accepted(self, env):
        # Stations report service states as strings ("active"/"inactive"), not
        # bools — the ingest schema must accept them (pilot regression 2026-07-01:
        # dict[str, bool] rejected real /api/status payloads with a 422).
        c, s = env["client"], env["secret"]
        r = c.post(
            "/api/ingest/v1/gmn0002/heartbeat",
            json=_envelope(7, {"online": True,
                               "services": {"rms-cam1": "active",
                                            "color-capture": "active"}}),
            headers=_hdr(s),
        )
        assert r.status_code == 200
        assert r.get_json()["dropped"] is False
        assert env["cache"].status["gmn0002"]["services"]["rms-cam1"] == "active"

    def test_non_object_body_422(self, env):
        r = env["client"].post(
            "/api/ingest/v1/gmn0002/heartbeat",
            data="not json",
            content_type="application/json",
            headers=_hdr(env["secret"]),
        )
        assert r.status_code == 422


# ── vitals ──────────────────────────────────────────────────────────────


class TestVitals:
    def test_writes_vitals_cache(self, env):
        r = env["client"].post(
            "/api/ingest/v1/gmn0002/vitals",
            json=_envelope(2, {"cpu_pct": 33.0, "ram_pct": 40.0}),
            headers=_hdr(env["secret"]),
        )
        assert r.status_code == 200
        assert env["cache"].vitals["gmn0002"]["cpu_pct"] == 33.0

    def test_malformed_payload_422(self, env):
        r = env["client"].post(
            "/api/ingest/v1/gmn0002/vitals",
            json=_envelope(2, {"cpu_pct": "not-a-number"}),
            headers=_hdr(env["secret"]),
        )
        assert r.status_code == 422


# ── detections ─────────────────────────────────────────────────────────────


class TestDetections:
    def _batch(self, seq: int, n: int = 1):
        dets = [
            {
                "cam": "RO000M", "date": "20260630",
                "ff_file": f"FF_RO000M_20260630_{i:03d}.fits", "meteor_no": 1,
                "time_utc": "2026-06-30T22:41:07Z", "shower": "SPO",
                "mag_apparent": -1.2,
            }
            for i in range(n)
        ]
        return _envelope(seq, {"window_since": "20260617", "detections": dets})

    def test_upserts_into_db(self, env):
        r = env["client"].post(
            "/api/ingest/v1/gmn0002/detections",
            json=self._batch(1, n=3),
            headers=_hdr(env["secret"]),
        )
        assert r.status_code == 200
        assert r.get_json()["written"] == 3
        rows = detection_db.query_detections(["20260630"], path=env["det_path"])
        assert len(rows) == 3
        assert rows[0]["source"] == "push"

    def test_replay_dropped_no_write(self, env):
        c, s = env["client"], env["secret"]
        c.post("/api/ingest/v1/gmn0002/detections", json=self._batch(5, n=1), headers=_hdr(s))
        r = c.post("/api/ingest/v1/gmn0002/detections", json=self._batch(5, n=2), headers=_hdr(s))
        assert r.get_json()["dropped"] is True
        assert r.get_json()["written"] == 0

    def test_missing_required_column_422(self, env):
        bad = _envelope(1, {"detections": [{"cam": "RO000M"}]})  # no date/ff_file
        r = env["client"].post(
            "/api/ingest/v1/gmn0002/detections", json=bad, headers=_hdr(env["secret"])
        )
        assert r.status_code == 422

    def test_empty_batch_ok(self, env):
        r = env["client"].post(
            "/api/ingest/v1/gmn0002/detections",
            json=_envelope(1, {"detections": []}),
            headers=_hdr(env["secret"]),
        )
        assert r.status_code == 200
        assert r.get_json()["written"] == 0


class TestDetectionsCap:
    """The per-POST cap and what a station learns when it trips it.

    Regression 2026-08-09: a five-camera station's rolling 14-day window grew
    past the cap, every push 422'd, and the VPS index froze for two weeks while
    the station kept heartbeating as healthy. The 422 body echoed the entire
    rejected batch back on every retry (~4.5 MB per POST, 5-minutely).
    """

    def _rows(self, n: int) -> list[dict]:
        return [
            {
                "cam": "RO000M", "date": "20260630",
                "ff_file": f"FF_RO000M_20260630_{i:05d}.fits", "meteor_no": 1,
            }
            for i in range(n)
        ]

    def test_cap_clears_a_full_hot_window_for_the_largest_station(self):
        # Five cameras x 14 nights of a shower month is ~10k rows; the cap has
        # to clear that on its own for stations not yet shipping batches.
        assert ingest_api._MAX_DETECTIONS >= 10000

    def test_cap_is_not_unbounded(self):
        assert ingest_api._MAX_DETECTIONS <= 50000

    def test_over_cap_422_does_not_echo_the_rejected_batch(self, env):
        n = ingest_api._MAX_DETECTIONS + 1
        r = env["client"].post(
            "/api/ingest/v1/gmn0002/detections",
            json=_envelope(1, {"detections": self._rows(n)}),
            headers=_hdr(env["secret"]),
        )
        assert r.status_code == 422
        body = r.get_json()
        assert body["error"] == "invalid_payload"
        # The whole point: the response is a few hundred bytes, not megabytes.
        assert len(r.data) < 2048
        # And it carries no fragment of the rejected input.
        assert "FF_RO000M" not in body["detail"]

    def test_over_cap_detail_names_the_count_and_the_limit(self, env):
        n = ingest_api._MAX_DETECTIONS + 7
        r = env["client"].post(
            "/api/ingest/v1/gmn0002/detections",
            json=_envelope(1, {"detections": self._rows(n)}),
            headers=_hdr(env["secret"]),
        )
        detail = r.get_json()["detail"]
        assert "too_long" in detail
        assert "detections" in detail
        assert str(n) in detail
        assert str(ingest_api._MAX_DETECTIONS) in detail

    def test_over_cap_logs_a_warning_naming_station_rows_and_limit(self, env, caplog):
        n = ingest_api._MAX_DETECTIONS + 3
        with caplog.at_level("WARNING", logger="ingest_api"):
            env["client"].post(
                "/api/ingest/v1/gmn0002/detections",
                json=_envelope(1, {"detections": self._rows(n)}),
                headers=_hdr(env["secret"]),
            )
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert warnings, "a capped station must be visible in the journal"
        msg = warnings[-1].getMessage()
        assert "gmn0002" in msg
        assert str(n) in msg
        assert str(ingest_api._MAX_DETECTIONS) in msg

    def test_batch_at_the_cap_is_accepted(self, env):
        r = env["client"].post(
            "/api/ingest/v1/gmn0002/detections",
            json=_envelope(1, {"detections": self._rows(ingest_api._MAX_DETECTIONS)}),
            headers=_hdr(env["secret"]),
        )
        assert r.status_code == 200
        assert r.get_json()["written"] == ingest_api._MAX_DETECTIONS

    def test_ordinary_validation_error_is_also_compact(self, env):
        bad = _envelope(1, {"detections": [{"cam": "RO000M", "ff_file": "x" * 4000}]})
        r = env["client"].post(
            "/api/ingest/v1/gmn0002/detections", json=bad, headers=_hdr(env["secret"])
        )
        assert r.status_code == 422
        # The oversized field value must not be reflected back.
        assert "x" * 100 not in r.get_json()["detail"]


class TestBlobRateBudget:
    """The blob bucket is per-station, so its minute component has to fit one
    station's whole dawn flush rather than a fleet average.

    Measured on the VPS access log for 2026-09-01/02: gmnro04 peaked at 669 blob
    POSTs in one minute and gmnde00 at 620 — both four-camera sites flushing
    per-FF stacks — against a 600/minute ceiling, for 121 rejected uploads.
    """

    def _components(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for part in ingest_api._BLOB_RATE.split(";"):
            count, _, period = part.partition("/")
            out[period.strip()] = int(count)
        return out

    def test_minute_budget_clears_the_measured_dawn_peak(self):
        assert self._components()["minute"] >= 700

    def test_hourly_budget_still_bounds_a_runaway_station(self):
        # Peak observed station-hour was ~2 500 POSTs, so the hourly component
        # was never the binding constraint — it stays the abuse ceiling.
        assert self._components()["hour"] <= 20000


class TestCompactValidationDetail:
    def test_truncates_a_long_error_list(self):
        from pydantic import ValidationError

        try:
            ingest_api.DetectionsPayload.model_validate(
                {"detections": [{"cam": "A"} for _ in range(50)]}
            )
        except ValidationError as exc:
            detail = ingest_api._compact_validation_detail(exc)
        else:  # pragma: no cover - the payload is invalid by construction
            pytest.fail("expected a ValidationError")
        assert len(detail) <= 512
        assert "more" in detail


# ── freshness feed (poll_state touch) ────────────────────────────────────


class TestFreshnessTouch:
    """Accepted pushes must stamp poll_state.last_polled — push stations are
    never polled, so ingest activity is the only feed for the
    detection_db.covers_dates() liveness gate (regression 2026-07-10: the gate
    froze fleet-wide as stations converged on push, silently disabling the
    detections-index fast path)."""

    def _batch(self, seq: int):
        det = {
            "cam": "RO000M", "date": "20260630",
            "ff_file": "FF_RO000M_20260630_000.fits", "meteor_no": 1,
            "time_utc": "2026-06-30T22:41:07Z", "shower": "SPO",
            "mag_apparent": -1.2,
        }
        return _envelope(seq, {"detections": [det]})

    def test_accepted_detections_push_opens_fast_path(self, env):
        r = env["client"].post(
            "/api/ingest/v1/gmn0002/detections",
            json=self._batch(1),
            headers=_hdr(env["secret"]),
        )
        assert r.status_code == 200
        state = detection_db.get_poll_state("gmn0002", path=env["det_path"])
        assert state["last_polled"] is not None
        assert state["since_date"] is None  # poller bookkeeping untouched
        # End to end: the fast-path gate is green for the pushed night.
        assert detection_db.covers_dates(
            ["20260630"], path=env["det_path"], poll_interval=300
        ) is True

    def test_heartbeat_touches_poll_state(self, env):
        r = env["client"].post(
            "/api/ingest/v1/gmn0002/heartbeat",
            json=_envelope(1, {"online": True}),
            headers=_hdr(env["secret"]),
        )
        assert r.status_code == 200
        state = detection_db.get_poll_state("gmn0002", path=env["det_path"])
        assert state["last_polled"] is not None

    def test_unauthorized_push_does_not_touch(self, env):
        r = env["client"].post(
            "/api/ingest/v1/gmn0002/heartbeat", json=_envelope(1, {"online": True})
        )
        assert r.status_code == 401
        state = detection_db.get_poll_state("gmn0002", path=env["det_path"])
        assert state["last_polled"] is None


class TestFreshnessTouchDegrades:
    """A contended writer lock on the freshness *stamp* must never cost a push.

    Regression 2026-09-01: writer-lock contention on detections.db surfaced as
    gunicorn 500s and nginx 504s on the ingest surface. The stamp only feeds
    covers_dates(), which tolerates 2x the poll interval of staleness and is
    refreshed by the next accepted push — failing the request over it throws
    away a detection batch that is already durably written.
    """

    def _batch(self, seq: int):
        det = {
            "cam": "RO000M", "date": "20260630",
            "ff_file": "FF_RO000M_20260630_000.fits", "meteor_no": 1,
            "time_utc": "2026-06-30T22:41:07Z", "shower": "SPO",
        }
        return _envelope(seq, {"detections": [det]})

    def test_locked_stamp_does_not_fail_the_push(self, env, monkeypatch, caplog):
        def _locked(*_a, **_k):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(detection_db, "touch_poll_state", _locked)
        with caplog.at_level("WARNING"):
            r = env["client"].post(
                "/api/ingest/v1/gmn0002/detections",
                json=self._batch(1), headers=_hdr(env["secret"]),
            )
        assert r.status_code == 200
        assert r.get_json()["written"] == 1
        # The batch is on disk even though the freshness stamp was skipped.
        assert len(detection_db.query_detections(["20260630"], path=env["det_path"])) == 1
        assert any("freshness stamp" in rec.message for rec in caplog.records)

    def test_locked_stamp_does_not_fail_a_heartbeat(self, env, monkeypatch):
        def _locked(*_a, **_k):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(detection_db, "touch_poll_state", _locked)
        r = env["client"].post(
            "/api/ingest/v1/gmn0002/heartbeat",
            json=_envelope(1, {"online": True}), headers=_hdr(env["secret"]),
        )
        assert r.status_code == 200
        assert env["cache"].status["gmn0002"]["online"] is True

    def test_a_locked_detection_write_still_surfaces(self, env, monkeypatch):
        """Only the freshness stamp degrades. If the batch itself cannot be
        written the station must hear about it and retry — silently 200-ing a
        lost batch would freeze that station's index with nothing in the log."""
        def _locked(*_a, **_k):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(detection_db, "upsert_detections", _locked)
        with pytest.raises(sqlite3.OperationalError):
            env["client"].post(
                "/api/ingest/v1/gmn0002/detections",
                json=self._batch(1), headers=_hdr(env["secret"]),
            )


# ── media pointers ───────────────────────────────────────────────────────


class TestMedia:
    def test_stores_pointers(self, env):
        payload = {
            "timelapses": [{"cam": "RO000M", "date": "20260630",
                            "filename": "t.mp4", "night_stack": "n.webp"}],
            "clips": [{"cam": "RO000M", "date": "20260630", "filename": "c.mkv",
                       "locked": True, "meteor_time": "2026-06-30T22:41:07Z"}],
        }
        r = env["client"].post(
            "/api/ingest/v1/gmn0002/media",
            json=_envelope(1, payload),
            headers=_hdr(env["secret"]),
        )
        assert r.status_code == 200
        assert r.get_json()["written"] == 2
        rows = ss.get_media_pointers("gmn0002", path=env["state_path"])
        kinds = {row["kind"] for row in rows}
        assert kinds == {"timelapse", "clip"}

    def test_replay_dropped(self, env):
        c, s = env["client"], env["secret"]
        p = {"clips": [{"cam": "RO000M", "date": "20260630", "filename": "c.mkv"}]}
        c.post("/api/ingest/v1/gmn0002/media", json=_envelope(5, p), headers=_hdr(s))
        r = c.post("/api/ingest/v1/gmn0002/media", json=_envelope(5, p), headers=_hdr(s))
        assert r.get_json()["dropped"] is True

    def test_malformed_pointer_422(self, env):
        # clip missing required 'filename'
        bad = _envelope(1, {"clips": [{"cam": "RO000M", "date": "20260630"}]})
        r = env["client"].post(
            "/api/ingest/v1/gmn0002/media", json=bad, headers=_hdr(env["secret"])
        )
        assert r.status_code == 422

    def test_clip_pointers_stored_with_lock_metadata(self, env):
        payload = {
            "clips": [{
                "cam": "RO000M", "date": "20260630",
                "filename": "RO000M_20260630_224107_color.mkv",
                "locked": True, "meteor_time": "2026-06-30T22:41:07Z",
                "lock_type": "detection",
            }],
        }
        r = env["client"].post(
            "/api/ingest/v1/gmn0002/media",
            json=_envelope(3, payload),
            headers=_hdr(env["secret"]),
        )
        assert r.status_code == 200
        assert r.get_json()["written"] == 1
        rows = ss.get_media_pointers("gmn0002", path=env["state_path"])
        clip_rows = [row for row in rows if row["kind"] == "clip"]
        assert len(clip_rows) == 1
        clip = clip_rows[0]
        assert clip["filename"] == "RO000M_20260630_224107_color.mkv"
        # locked / meteor_time / lock_type ride the extra JSON blob.
        assert clip["extra"]["locked"] is True
        assert clip["extra"]["meteor_time"] == "2026-06-30T22:41:07Z"
        assert clip["extra"]["lock_type"] == "detection"


# ── live thumbnail (newest-FF maxpixel WebP) ───────────────────────────────


class TestLiveThumb:
    def _thumb_body(self, seq: int, raw: bytes = b"\x00webpbytes",
                    content_type: str = "image/webp",
                    ff_ts: str | None = "2026-06-30T22:41:07.120Z"):
        return _envelope(seq, {
            "content_type": content_type,
            "ff_timestamp": ff_ts,
            "data_b64": base64.b64encode(raw).decode("ascii"),
        })

    def test_stores_latest_per_cam(self, env):
        c, s = env["client"], env["secret"]
        r = c.post("/api/ingest/v1/gmn0002/live_thumb/RO000M",
                   json=self._thumb_body(1, b"first"), headers=_hdr(s))
        assert r.status_code == 200
        assert r.get_json()["bytes"] == len(b"first")
        stored = env["cache"].live_thumbs[("gmn0002", "RO000M")]
        assert stored["data"] == b"first"
        assert stored["content_type"] == "image/webp"
        assert stored["ff_timestamp"] == "2026-06-30T22:41:07.120Z"
        # A newer seq overwrites (latest-writer-wins per cam).
        r2 = c.post("/api/ingest/v1/gmn0002/live_thumb/RO000M",
                    json=self._thumb_body(2, b"second"), headers=_hdr(s))
        assert r2.status_code == 200
        assert env["cache"].live_thumbs[("gmn0002", "RO000M")]["data"] == b"second"

    def test_separate_cams_kept_independently(self, env):
        c, s = env["client"], env["secret"]
        c.post("/api/ingest/v1/gmn0002/live_thumb/RO000M",
               json=self._thumb_body(1, b"m"), headers=_hdr(s))
        c.post("/api/ingest/v1/gmn0002/live_thumb/RO000N",
               json=self._thumb_body(2, b"n"), headers=_hdr(s))
        assert env["cache"].live_thumbs[("gmn0002", "RO000M")]["data"] == b"m"
        assert env["cache"].live_thumbs[("gmn0002", "RO000N")]["data"] == b"n"

    def test_accepts_a_thumb_over_the_old_512k_ceiling(self, env):
        """Regression 2026-09-01: gmnde00's DE001E encodes just over 512 KB and
        was 413'd 52x/day, leaving a healthy camera with a dead live panel."""
        raw = b"x" * (700 * 1024)
        r = env["client"].post("/api/ingest/v1/gmn0002/live_thumb/RO000M",
                               json=self._thumb_body(1, raw), headers=_hdr(env["secret"]))
        assert r.status_code == 200
        assert env["cache"].live_thumbs[("gmn0002", "RO000M")]["data"] == raw

    def test_still_bounds_a_misbehaving_station(self, env):
        raw = b"x" * (ingest_api._MAX_LIVE_THUMB_BYTES + 1)
        r = env["client"].post("/api/ingest/v1/gmn0002/live_thumb/RO000M",
                               json=self._thumb_body(1, raw), headers=_hdr(env["secret"]))
        assert r.status_code == 413
        assert r.get_json()["error"] == "thumb_too_large"

    def test_replay_dropped_no_overwrite(self, env):
        c, s = env["client"], env["secret"]
        c.post("/api/ingest/v1/gmn0002/live_thumb/RO000M",
               json=self._thumb_body(5, b"keep"), headers=_hdr(s))
        r = c.post("/api/ingest/v1/gmn0002/live_thumb/RO000M",
                   json=self._thumb_body(5, b"stale"), headers=_hdr(s))
        assert r.get_json()["dropped"] is True
        assert env["cache"].live_thumbs[("gmn0002", "RO000M")]["data"] == b"keep"

    def test_missing_key_401(self, env):
        r = env["client"].post(
            "/api/ingest/v1/gmn0002/live_thumb/RO000M", json=self._thumb_body(1)
        )
        assert r.status_code == 401

    def test_wrong_station_403(self, env):
        r = env["client"].post(
            "/api/ingest/v1/gmnro10/live_thumb/RO000M",
            json=self._thumb_body(1), headers=_hdr(env["secret"]),
        )
        assert r.status_code == 403

    def test_bad_cam_404(self, env):
        r = env["client"].post(
            "/api/ingest/v1/gmn0002/live_thumb/bad-cam",
            json=self._thumb_body(1), headers=_hdr(env["secret"]),
        )
        assert r.status_code == 404

    def test_malformed_envelope_422(self, env):
        r = env["client"].post(
            "/api/ingest/v1/gmn0002/live_thumb/RO000M",
            json={"payload": {"data_b64": "AAAA"}},  # missing seq
            headers=_hdr(env["secret"]),
        )
        assert r.status_code == 422

    def test_missing_data_422(self, env):
        r = env["client"].post(
            "/api/ingest/v1/gmn0002/live_thumb/RO000M",
            json=_envelope(1, {"content_type": "image/webp"}),  # no data_b64
            headers=_hdr(env["secret"]),
        )
        assert r.status_code == 422

    def test_invalid_base64_422(self, env):
        r = env["client"].post(
            "/api/ingest/v1/gmn0002/live_thumb/RO000M",
            json=_envelope(1, {"data_b64": "not valid base64!!!"}),
            headers=_hdr(env["secret"]),
        )
        assert r.status_code == 422

    def test_disallowed_content_type_422(self, env):
        r = env["client"].post(
            "/api/ingest/v1/gmn0002/live_thumb/RO000M",
            json=self._thumb_body(1, content_type="video/mp4"),
            headers=_hdr(env["secret"]),
        )
        assert r.status_code == 422


# ── Redis seq path (seq/replay guard offloaded from SQLite) ──────────────────


class TestRedisSeqPath:
    """With ROVIMEN_REDIS_URL configured, the per-push seq check-and-bump runs
    in Redis, not SQLite — but the accept/replay/older-seq DROP semantics that
    the SQLite path (and the tests above) assert must be identical."""

    def test_heartbeat_accept_then_replay_dropped(self, redis_env):
        c, s = redis_env["client"], redis_env["secret"]
        r1 = c.post("/api/ingest/v1/gmn0002/heartbeat",
                    json=_envelope(5, {"online": True}), headers=_hdr(s))
        assert r1.status_code == 200
        assert r1.get_json()["dropped"] is False
        # Same seq again → replay, cache not overwritten.
        r2 = c.post("/api/ingest/v1/gmn0002/heartbeat",
                    json=_envelope(5, {"online": False}), headers=_hdr(s))
        assert r2.get_json()["dropped"] is True
        assert redis_env["cache"].status["gmn0002"]["online"] is True

    def test_older_seq_dropped(self, redis_env):
        c, s = redis_env["client"], redis_env["secret"]
        c.post("/api/ingest/v1/gmn0002/heartbeat",
               json=_envelope(10, {"online": True}), headers=_hdr(s))
        r = c.post("/api/ingest/v1/gmn0002/heartbeat",
                   json=_envelope(4, {"online": False}), headers=_hdr(s))
        assert r.get_json()["dropped"] is True

    def test_seq_lives_in_redis_not_sqlite(self, redis_env):
        """The per-push seq write must NOT touch the single-writer SQLite DB."""
        c, s = redis_env["client"], redis_env["secret"]
        c.post("/api/ingest/v1/gmn0002/heartbeat",
               json=_envelope(7, {"online": True}), headers=_hdr(s))
        # Seq is in Redis…
        assert int(redis_env["redis"].hget("ingest:station:seq", "gmn0002:seq")) == 7
        # …and the SQLite last_seq column was NOT bumped by the push.
        assert _sqlite_last_seq(redis_env["state_path"], "gmn0002") == 0

    def test_status_and_vitals_pushes_write_no_last_value_copy(self, redis_env):
        """A heartbeat or vitals push lands in StationCache and bumps the Redis
        seq, and writes nothing else: no SQLite row and no per-station Redis
        "mirror" hash, which nothing read."""
        c, s = redis_env["client"], redis_env["secret"]
        c.post("/api/ingest/v1/gmn0002/heartbeat",
               json=_envelope(3, {"online": True, "rms_running": True}),
               headers=_hdr(s))
        c.post("/api/ingest/v1/gmn0002/vitals",
               json=_envelope(4, {"cpu_pct": 42.0, "temp_c": 51.0}),
               headers=_hdr(s))
        assert redis_env["cache"].status["gmn0002"]["rms_running"] is True
        assert ss.get_last_seq("gmn0002", path=redis_env["state_path"]) == 4
        assert sorted(redis_env["redis"].keys()) == ["ingest:station:seq"]
        assert _sqlite_last_seq(redis_env["state_path"], "gmn0002") == 0

    def test_detections_replay_dropped_no_write(self, redis_env):
        c, s = redis_env["client"], redis_env["secret"]
        batch = _envelope(5, {"detections": [{
            "cam": "RO000M", "date": "20260630",
            "ff_file": "FF_RO000M_20260630_000.fits", "meteor_no": 1,
        }]})
        c.post("/api/ingest/v1/gmn0002/detections", json=batch, headers=_hdr(s))
        r = c.post("/api/ingest/v1/gmn0002/detections", json=batch, headers=_hdr(s))
        assert r.get_json()["dropped"] is True
        assert r.get_json()["written"] == 0

    def test_media_replay_dropped(self, redis_env):
        c, s = redis_env["client"], redis_env["secret"]
        p = {"clips": [{"cam": "RO000M", "date": "20260630", "filename": "c.mkv"}]}
        c.post("/api/ingest/v1/gmn0002/media", json=_envelope(5, p), headers=_hdr(s))
        r = c.post("/api/ingest/v1/gmn0002/media", json=_envelope(5, p), headers=_hdr(s))
        assert r.get_json()["dropped"] is True

    def test_live_thumb_replay_no_overwrite(self, redis_env):
        c, s = redis_env["client"], redis_env["secret"]
        body1 = _envelope(5, {
            "content_type": "image/webp",
            "data_b64": base64.b64encode(b"keep").decode("ascii"),
        })
        body2 = _envelope(5, {
            "content_type": "image/webp",
            "data_b64": base64.b64encode(b"stale").decode("ascii"),
        })
        c.post("/api/ingest/v1/gmn0002/live_thumb/RO000M", json=body1, headers=_hdr(s))
        r = c.post("/api/ingest/v1/gmn0002/live_thumb/RO000M", json=body2, headers=_hdr(s))
        assert r.get_json()["dropped"] is True
        assert redis_env["cache"].live_thumbs[("gmn0002", "RO000M")]["data"] == b"keep"

    def test_redis_error_accepts_pushes_instead_of_dropping_them(self, redis_env, monkeypatch):
        """A Redis error in the seq guard used to answer 200 "dropped: replay".
        The pusher counts any 2xx as delivered, so every push made during a
        Redis blip was lost (chunk listings are only re-sent on change). The
        guard now fails open and the push is applied."""
        c, s = redis_env["client"], redis_env["secret"]
        r = c.post("/api/ingest/v1/gmn0002/heartbeat",
                   json=_envelope(5, {"online": True}), headers=_hdr(s))
        assert r.get_json()["dropped"] is False

        def _redis_down(*_a, **_k):
            raise ConnectionError("redis down")

        monkeypatch.setattr(redis_env["redis"], "pipeline", _redis_down)

        r = c.post("/api/ingest/v1/gmn0002/heartbeat",
                   json=_envelope(6, {"online": False}), headers=_hdr(s))
        assert r.status_code == 200
        assert r.get_json()["dropped"] is False
        assert redis_env["cache"].status["gmn0002"]["online"] is False

        batch = _envelope(7, {"detections": [{
            "cam": "RO000M", "date": "20260630",
            "ff_file": "FF_RO000M_20260630_000.fits", "meteor_no": 1,
        }]})
        r = c.post("/api/ingest/v1/gmn0002/detections", json=batch, headers=_hdr(s))
        assert r.status_code == 200
        assert r.get_json()["dropped"] is False
        assert r.get_json()["written"] == 1

        listing = _envelope(8, {"cam": "RO000M", "date": "20260630",
                                "chunks": [["20260630_220000", 12.5]]})
        r = c.post("/api/ingest/v1/gmn0002/chunks", json=listing, headers=_hdr(s))
        assert r.status_code == 200
        assert r.get_json()["dropped"] is False

        # A duplicate during the blip is applied again; the writes are keyed
        # upserts, so the detection is still stored once.
        r = c.post("/api/ingest/v1/gmn0002/detections", json=batch, headers=_hdr(s))
        assert r.get_json()["dropped"] is False
        import sqlite3
        with sqlite3.connect(str(redis_env["det_path"])) as con:
            assert con.execute("SELECT COUNT(*) FROM detections").fetchone()[0] == 1

    def test_replay_dropped_again_once_redis_recovers(self, redis_env, monkeypatch):
        c, s = redis_env["client"], redis_env["secret"]
        c.post("/api/ingest/v1/gmn0002/heartbeat",
               json=_envelope(5, {"online": True}), headers=_hdr(s))
        real_pipeline = redis_env["redis"].pipeline

        def _redis_down(*_a, **_k):
            raise ConnectionError("redis down")

        monkeypatch.setattr(redis_env["redis"], "pipeline", _redis_down)
        c.post("/api/ingest/v1/gmn0002/heartbeat",
               json=_envelope(6, {"online": True}), headers=_hdr(s))
        monkeypatch.setattr(redis_env["redis"], "pipeline", real_pipeline)

        r = c.post("/api/ingest/v1/gmn0002/heartbeat",
                   json=_envelope(5, {"online": False}), headers=_hdr(s))
        assert r.get_json()["dropped"] is True
        assert r.get_json()["reason"] == "replay"
        assert redis_env["cache"].status["gmn0002"]["online"] is True

    def test_accept_seq_never_writes_sqlite_seq_column(self, redis_env, monkeypatch):
        """Prove the seq write bypasses SQLite: spy on accept_seq's SQLite branch
        by asserting no INSERT advances station_state.last_seq. A detections push
        (whose only durable seq write WAS advance_seq) must record seq in Redis
        and leave the SQLite last_seq column untouched."""
        c, s = redis_env["client"], redis_env["secret"]
        batch = _envelope(11, {"detections": [{
            "cam": "RO000M", "date": "20260630",
            "ff_file": "FF_RO000M_20260630_000.fits", "meteor_no": 1,
        }]})
        r = c.post("/api/ingest/v1/gmn0002/detections", json=batch, headers=_hdr(s))
        assert r.status_code == 200
        # Seq recorded in Redis…
        assert int(redis_env["redis"].hget("ingest:station:seq", "gmn0002:seq")) == 11
        # …and detections do not even create a station_state row (advance_seq is
        # now a Redis-only op for detections), so the SQLite seq stays 0.
        assert _sqlite_last_seq(redis_env["state_path"], "gmn0002") == 0


# ── Archive blob upload (HTTPS-ingest byte plane) ─────────────────────────────

def _blob_url(station: str, cam: str, date: str, relpath: str) -> str:
    return f"/api/ingest/v1/{station}/blob/{cam}/{date}/{relpath}"


class TestSafeBlobRelpath:
    """Pure path-safety unit tests — the core guard that a station key can only
    write inside its own camera/night tree."""

    def test_valid_meteor_clip(self):
        assert ingest_api._safe_blob_relpath(
            "meteors/RO000M_20260709_214720_1_color.mkv"
        ) == ("meteors", "RO000M_20260709_214720_1_color.mkv")

    def test_valid_state_json_root(self):
        assert ingest_api._safe_blob_relpath("state.json") == ("", "state.json")

    def test_valid_rms_and_timelapse(self):
        assert ingest_api._safe_blob_relpath("rms/FTPdetectinfo_RO000M.txt")[0] == "rms"
        assert ingest_api._safe_blob_relpath("timelapse/x_timelapse.mp4")[0] == "timelapse"

    def test_valid_ffthumb(self):
        # BW FF thumbnails pushed live by the detection indexer.
        assert ingest_api._safe_blob_relpath(
            "ffthumbs/RO000M_20260709_214720.webp"
        ) == ("ffthumbs", "RO000M_20260709_214720.webp")

    @pytest.mark.parametrize("bad", [
        "../secret", "meteors/../../etc/passwd", "/abs/path", "meteors/",
        "unknownsub/file.mkv", "a/b/c/file", "state.json/x", "meteors/..",
        "meteors/\\evil", "", " meteors/f", "notallowed.txt", "meteors/a b",
    ])
    def test_rejects_unsafe(self, bad):
        assert ingest_api._safe_blob_relpath(bad) is None


class TestIngestBlob:
    def test_upload_clip_writes_file_and_pointer(self, env):
        c, s = env["client"], env["secret"]
        body = b"\x00\x01colour-mkv-bytes" * 100
        url = _blob_url("gmn0002", "RO000M", "20260709",
                        "meteors/RO000M_20260709_214720_1_color.mkv")
        r = c.post(url, data=body, headers=_hdr(s),
                   content_type="application/octet-stream")
        assert r.status_code == 200, r.get_data(as_text=True)
        assert r.get_json()["bytes"] == len(body)
        assert r.get_json()["spooled"] is True
        # Blob lands in the local spool; drive the flusher to land it on the archive.
        env["app"]._blob_spooler.flush_once()
        f = (env["archive_root"] / "RO000M" / "20260709" / "meteors"
             / "RO000M_20260709_214720_1_color.mkv")
        assert f.is_file() and f.read_bytes() == body
        ptrs = ss.get_media_pointers("gmn0002", "20260709", path=env["state_path"])
        assert any(p["kind"] == "clip" and p["filename"].endswith("_color.mkv")
                   for p in ptrs)

    def test_upload_state_json_at_root(self, env):
        c, s = env["client"], env["secret"]
        r = c.post(_blob_url("gmn0002", "RO000M", "20260709", "state.json"),
                   data=b'{"chunks":{}}', headers=_hdr(s))
        assert r.status_code == 200
        env["app"]._blob_spooler.flush_once()
        assert (env["archive_root"] / "RO000M" / "20260709" / "state.json").is_file()

    def test_requires_key(self, env):
        r = env["client"].post(
            _blob_url("gmn0002", "RO000M", "20260709", "meteors/x_color.mkv"),
            data=b"x")
        assert r.status_code == 401

    def test_wrong_station_key_forbidden(self, env):
        c, s = env["client"], env["secret"]
        r = c.post(_blob_url("gmnro10", "RO000E", "20260709", "meteors/x_color.mkv"),
                   data=b"x", headers=_hdr(s))
        assert r.status_code == 403

    def test_path_traversal_rejected(self, env):
        c, s = env["client"], env["secret"]
        r = c.post("/api/ingest/v1/gmn0002/blob/RO000M/20260709/meteors/..%2f..%2fevil",
                   data=b"x", headers=_hdr(s))
        assert r.status_code in (404, 422)
        assert not (env["archive_root"].parent / "evil").exists()

    def test_bad_date_rejected(self, env):
        c, s = env["client"], env["secret"]
        r = c.post(_blob_url("gmn0002", "RO000M", "2026abcd", "state.json"),
                   data=b"x", headers=_hdr(s))
        assert r.status_code == 422

    def test_same_size_reupload_replaces_the_pending_copy(self, env):
        """A recalibrated FTPdetectinfo is a same-size rewrite: the newer
        bytes must reach the archive, not be skipped as a duplicate."""
        c, s = env["client"], env["secret"]
        rel = "rms/FTPdetectinfo_RO000M_20260709_160000_000000.txt"
        url = _blob_url("gmn0002", "RO000M", "20260709", rel)
        r1 = c.post(url, data=b"old-astrometry", headers=_hdr(s))
        r2 = c.post(url, data=b"new-astrometry", headers=_hdr(s))
        assert r1.status_code == 200 and r2.status_code == 200
        assert not r2.get_json().get("skipped")
        env["app"]._blob_spooler.flush_once()
        f = env["archive_root"] / "RO000M" / "20260709" / rel
        assert f.read_bytes() == b"new-astrometry"

    def test_rms_flush_invalidates_cached_plot_and_rows(self, env, tmp_path, monkeypatch):
        import cache_store
        thumbs = tmp_path / "thumb_cache"
        monkeypatch.setattr(cache_store, "THUMB_CACHE_DIR", thumbs)
        monkeypatch.setattr(cache_store, "CACHE_PATH", tmp_path / "cache")
        name = "RO000M_20260709_160000_000000_calib_report_astrometry.jpg"
        cached = thumbs / "gmn0002" / "rms_plots" / "RO000M" / "20260709" / name
        cached.parent.mkdir(parents=True)
        cached.write_bytes(b"old-plot")
        cache_store._disk_cache_write("rms_detections", "RO000M_20260709", [{"x": 1}])
        before = cache_store.rms_night_generation("RO000M", "20260709")

        c, s = env["client"], env["secret"]
        r = c.post(_blob_url("gmn0002", "RO000M", "20260709", f"rms/{name}"),
                   data=b"new-plot", headers=_hdr(s))
        assert r.status_code == 200
        env["app"]._blob_spooler.flush_once()

        assert not cached.exists()
        assert not cache_store._disk_cache_path("rms_detections", "RO000M_20260709").exists()
        assert cache_store.rms_night_generation("RO000M", "20260709") > before

    def test_oversize_body_rejected(self, env, monkeypatch):
        monkeypatch.setattr(ingest_api, "_MAX_BLOB_BYTES", 8)
        c, s = env["client"], env["secret"]
        r = c.post(_blob_url("gmn0002", "RO000M", "20260709", "meteors/big_color.mkv"),
                   data=b"0123456789abcdef", headers=_hdr(s))
        assert r.status_code == 413
        assert not (env["archive_root"] / "RO000M" / "20260709" / "meteors"
                    / "big_color.mkv").exists()

    def test_empty_body_rejected(self, env):
        c, s = env["client"], env["secret"]
        r = c.post(_blob_url("gmn0002", "RO000M", "20260709", "meteors/empty_color.mkv"),
                   data=b"", headers=_hdr(s))
        assert r.status_code == 422

    def test_spool_below_reserve_answers_507_and_writes_nothing(self, env, monkeypatch):
        """A near-full spool disk refuses the blob with a retryable 507 instead
        of writing until ENOSPC (2026-09-22: 0 bytes free took Redis down)."""
        monkeypatch.setattr(ingest_api, "_BLOB_SPOOL_MIN_FREE_BYTES", 1 << 62)
        c, s = env["client"], env["secret"]
        r = c.post(_blob_url("gmn0002", "RO000M", "20260709", "meteors/full_color.mkv"),
                   data=b"x" * 1024, headers=_hdr(s),
                   content_type="application/octet-stream")
        assert r.status_code == 507
        assert r.headers["Retry-After"] == str(ingest_api._BLOB_SPOOL_FULL_RETRY_AFTER_S)
        assert r.get_json()["error"] == "spool_full"
        spool = env["app"]._blob_spooler.spool_root
        assert not any(p.name.startswith("full_color") or ".full_color" in p.name
                       for p in spool.rglob("*"))

    def test_spool_above_reserve_accepts(self, env, monkeypatch):
        monkeypatch.setattr(ingest_api, "_BLOB_SPOOL_MIN_FREE_BYTES", 0)
        c, s = env["client"], env["secret"]
        r = c.post(_blob_url("gmn0002", "RO000M", "20260709", "meteors/ok_color.mkv"),
                   data=b"x" * 1024, headers=_hdr(s),
                   content_type="application/octet-stream")
        assert r.status_code == 200, r.get_data(as_text=True)

    def test_ffthumb_blob_lands_on_vps_disk_not_spool(self, env, tmp_path, monkeypatch):
        """The indexer's per-chunk BW thumbnail push goes straight to the VPS
        ffthumb dir, never through the blob spool (one Storage Box write per
        file could not keep up), and mints no media pointer."""
        import cache_store
        monkeypatch.setattr(cache_store, "FFTHUMB_DIR", tmp_path / "ffthumbs")
        c, s = env["client"], env["secret"]
        body = b"RIFF....WEBPfake-ffthumb"
        r = c.post(_blob_url("gmn0002", "RO000M", "20260709",
                             "ffthumbs/RO000M_20260709_214718.webp"),
                   data=body, headers=_hdr(s),
                   content_type="application/octet-stream")
        assert r.status_code == 200, r.get_data(as_text=True)
        assert r.get_json()["spooled"] is False
        f = tmp_path / "ffthumbs" / "RO000M" / "20260709" / "RO000M_20260709_214718.webp"
        assert f.read_bytes() == body
        assert not [p for p in (tmp_path / "spool").rglob("*") if p.is_file()]
        ptrs = ss.get_media_pointers("gmn0002", "20260709", path=env["state_path"])
        assert not any(p["filename"].endswith("214718.webp") for p in ptrs)

    # ── Hot clip-cache tee (NVMe promotion at ingest time) ────────────────

    def test_mp4_blob_tees_into_hot_clip_cache(self, env, tmp_path, monkeypatch):
        import clip_cache
        hot_root = tmp_path / "hotclips"
        monkeypatch.setattr(clip_cache, "CLIP_CACHE_DIR", hot_root)
        c, s = env["client"], env["secret"]
        body = b"faststart-mp4-bytes" * 50
        url = _blob_url("gmn0002", "RO000M", "20260709",
                        "meteors/RO000M_20260709_214720_1_color.mp4")
        r = c.post(url, data=body, headers=_hdr(s))
        assert r.status_code == 200, r.get_data(as_text=True)
        hot = (hot_root / "RO000M" / "20260709"
               / "RO000M_20260709_214720_1_color.mp4")
        assert hot.read_bytes() == body
        assert not list(hot.parent.glob(".*"))  # no stray .part files

    def test_mkv_blob_not_teed(self, env, tmp_path, monkeypatch):
        import clip_cache
        hot_root = tmp_path / "hotclips"
        monkeypatch.setattr(clip_cache, "CLIP_CACHE_DIR", hot_root)
        c, s = env["client"], env["secret"]
        r = c.post(_blob_url("gmn0002", "RO000M", "20260709",
                             "meteors/RO000M_20260709_1_color.mkv"),
                   data=b"mkv-bytes", headers=_hdr(s))
        assert r.status_code == 200
        assert not (hot_root / "RO000M").exists()

    def test_hot_cache_failure_never_fails_ingest(self, env, tmp_path, monkeypatch):
        import clip_cache
        blocker = tmp_path / "blocked"
        blocker.write_text("a file where the cache dir should be")
        monkeypatch.setattr(clip_cache, "CLIP_CACHE_DIR", blocker)
        c, s = env["client"], env["secret"]
        url = _blob_url("gmn0002", "RO000M", "20260709",
                        "meteors/RO000M_20260709_214720_2_color.mp4")
        r = c.post(url, data=b"clip-bytes", headers=_hdr(s))
        assert r.status_code == 200  # spool is the truth; the hot tee is best-effort
        env["app"]._blob_spooler.flush_once()
        f = (env["archive_root"] / "RO000M" / "20260709" / "meteors"
             / "RO000M_20260709_214720_2_color.mp4")
        assert f.read_bytes() == b"clip-bytes"


# ── Cross-tenant camera ownership gate ─────────────────────────────────────


@pytest.fixture
def owned_env(tmp_path: Path):
    """Like ``env`` but with a ``cam_owners`` map wired, so the cross-tenant
    write gate is exercised: RO000M belongs to gmn0002, RO000E to gmnro10, and
    every other code is unknown (fail-open). The minted key is gmn0002's."""
    keys_path = tmp_path / "station_keys.yaml"
    det_path = tmp_path / "detections.db"
    state_path = tmp_path / "state.db"
    archive_root = tmp_path / "archive"
    cache = _FakeCache()

    _, secret = station_keys.add_key("gmn0002", path=keys_path)
    station_keys._invalidate_cache()

    owners = {"RO000M": {"gmn0002"}, "RO000E": {"gmnro10"}}

    app = Flask(__name__)
    app.testing = True
    ingest_api.register_ingest_routes(
        app,
        cache_set_status=cache.set_status,
        cache_set_vitals=cache.set_vitals,
        cache_set_live_thumb=cache.set_live_thumb,
        known_stations=lambda: {"gmn0002", "gmnro10"},
        cam_owners=lambda cam: owners.get(cam, set()),
        detections_db_path=det_path,
        station_state_path=state_path,
        station_keys_path=keys_path,
        archive_media_path=archive_root,
        blob_spool_dir=tmp_path / "spool",
        limiter=None,
    )
    return {
        "client": app.test_client(),
        "app": app,
        "secret": secret,
        "archive_root": archive_root,
        "state_path": state_path,
    }


class TestCamOwnership:
    def test_owned_cam_blob_accepted(self, owned_env):
        c, s = owned_env["client"], owned_env["secret"]
        r = c.post(_blob_url("gmn0002", "RO000M", "20260709",
                             "meteors/RO000M_20260709_1_color.mkv"),
                   data=b"clip-bytes", headers=_hdr(s))
        assert r.status_code == 200, r.get_data(as_text=True)

    def test_foreign_cam_blob_forbidden(self, owned_env):
        # gmn0002's own key, own station path — but targeting gmnro10's camera
        # RO000E. Auth passes (key matches station); ownership must reject.
        c, s = owned_env["client"], owned_env["secret"]
        r = c.post(_blob_url("gmn0002", "RO000E", "20260709",
                             "meteors/RO000E_20260709_1_color.mkv"),
                   data=b"evil-bytes", headers=_hdr(s))
        assert r.status_code == 403
        assert r.get_json()["error"] == "cam_not_owned"
        # nothing written into the victim camera's tree
        assert not (owned_env["archive_root"] / "RO000E").exists()

    def test_unknown_cam_blob_allowed_failopen(self, owned_env):
        # A camera absent from the config (commissioning / pre-config-sync) is
        # allowed, so a freshly added camera is never blocked before its entry.
        c, s = owned_env["client"], owned_env["secret"]
        r = c.post(_blob_url("gmn0002", "RO009Z", "20260709",
                             "meteors/RO009Z_20260709_1_color.mkv"),
                   data=b"new-cam-bytes", headers=_hdr(s))
        assert r.status_code == 200, r.get_data(as_text=True)

    def test_foreign_cam_media_pointer_forbidden(self, owned_env):
        c, s = owned_env["client"], owned_env["secret"]
        payload = {"clips": [{"cam": "RO000E", "date": "20260630",
                              "filename": "RO000E_x_color.mkv"}]}
        r = c.post("/api/ingest/v1/gmn0002/media",
                   json=_envelope(1, payload), headers=_hdr(s))
        assert r.status_code == 403
        assert r.get_json()["error"] == "cam_not_owned"
        # batch rejected wholesale — no pointer stored
        assert ss.get_media_pointers("gmn0002", path=owned_env["state_path"]) == []

    def test_owned_cam_media_pointer_accepted(self, owned_env):
        c, s = owned_env["client"], owned_env["secret"]
        payload = {"clips": [{"cam": "RO000M", "date": "20260630",
                              "filename": "RO000M_x_color.mkv"}]}
        r = c.post("/api/ingest/v1/gmn0002/media",
                   json=_envelope(1, payload), headers=_hdr(s))
        assert r.status_code == 200
        assert r.get_json()["written"] == 1

    def test_foreign_cam_live_thumb_forbidden(self, owned_env):
        import base64 as _b64
        c, s = owned_env["client"], owned_env["secret"]
        data_b64 = _b64.b64encode(b"RIFF....WEBP-ish").decode()
        r = c.post("/api/ingest/v1/gmn0002/live_thumb/RO000E",
                   json=_envelope(1, {"content_type": "image/webp",
                                      "data_b64": data_b64}),
                   headers=_hdr(s))
        assert r.status_code == 403
        assert r.get_json()["error"] == "cam_not_owned"


# ── Rate-limit bucket identity (per-station, not shared) ──────────────────


class TestLimiterIdentity:
    """_limiter_identity must isolate stations. All ingest traffic arrives via
    the local reverse proxy (client IP always 127.0.0.1) and the validated key
    isn't attached when flask-limiter evaluates the key_func, so IP-keying used
    to collapse the whole fleet into one bucket -> fleet-wide live_thumb 429s.
    """

    def test_prefers_validated_key_id(self):
        assert ingest_api._limiter_identity("k7", "gmnro01", "127.0.0.1") == "station:k7"

    def test_falls_back_to_url_station(self):
        # No validated key yet (the normal case at limit-eval time).
        assert ingest_api._limiter_identity(None, "gmnro01", "127.0.0.1") == "station:gmnro01"

    def test_two_stations_get_distinct_buckets_behind_same_proxy_ip(self):
        a = ingest_api._limiter_identity(None, "gmnro01", "127.0.0.1")
        b = ingest_api._limiter_identity(None, "gmnro05", "127.0.0.1")
        assert a != b  # one busy station must not throttle another

    def test_last_resort_client_ip(self):
        assert ingest_api._limiter_identity(None, None, "203.0.113.9") == "203.0.113.9"


# ---------------------------------------------------------------------------
# Trajectories — the one READ on the ingest surface (issue #950)
#
# GMN's multi-station solutions live on the VPS; a station has no route to
# globalmeteornetwork.org. The overlay-details re-run asks for the night it is
# re-rendering, station-initiated over the same authenticated channel as the
# writes beside it.
# ---------------------------------------------------------------------------

_TRAJ_ROWS = [{
    "time_utc": "2026-08-01T19:05:20",
    "trajectory": {"trajectory_id": "t1", "velocity_geo_km_s": 59.2,
                   "station_count": 3},
}]


def _traj_url(station="gmn0002", cam="RO000M", date="20260801"):
    return f"/api/ingest/v1/{station}/trajectories/{cam}/{date}"


def test_trajectories_returns_the_nights_solutions(env, monkeypatch):
    import gmn_data
    seen: list[tuple[str, str]] = []

    def _stub(cam, date):
        seen.append((cam, date))
        return _TRAJ_ROWS
    monkeypatch.setattr(gmn_data, "trajectories_for_camera_night", _stub)

    r = env["client"].get(_traj_url(), headers={"X-Station-Key": env["secret"]})
    assert r.status_code == 200
    body = r.get_json()
    assert body["camera"] == "RO000M"
    assert body["date"] == "20260801"
    assert body["count"] == 1
    assert body["trajectories"] == _TRAJ_ROWS
    # Camera code reaches the lookup upper-cased, as GMN files them.
    assert seen == [("RO000M", "20260801")]


def test_trajectories_requires_a_station_key(env):
    r = env["client"].get(_traj_url())
    assert r.status_code == 401
    assert r.get_json()["error"] == "missing_or_invalid_station_key"


def test_trajectories_rejects_another_stations_key(env):
    # A valid key can only read its own station's path — same §2.2 property the
    # writes enforce.
    r = env["client"].get(
        _traj_url(station="gmnro10"), headers={"X-Station-Key": env["secret"]}
    )
    assert r.status_code == 403


def test_trajectories_rejects_the_url_key_param(env):
    r = env["client"].get(_traj_url() + f"?key={env['secret']}")
    assert r.status_code == 400
    assert r.get_json()["error"] == "url_key_param_disabled"


def test_trajectories_validates_cam_and_date(env):
    h = {"X-Station-Key": env["secret"]}
    bad_cam = env["client"].get(_traj_url(cam="ro000m"), headers=h)
    assert bad_cam.status_code == 422
    assert bad_cam.get_json()["error"] == "invalid_cam"
    bad_date = env["client"].get(_traj_url(date="2026-08-01"), headers=h)
    assert bad_date.status_code == 422
    assert bad_date.get_json()["error"] == "invalid_date"


def test_trajectories_reports_empty_rather_than_failing(env, monkeypatch):
    """An unpublished night and a broken GMN cache both mean 'no better numbers
    than the station already has' — never a 500 the caller has to interpret."""
    import gmn_data

    def _boom(*_a, **_k):
        raise RuntimeError("corrupt monthly cache")
    monkeypatch.setattr(gmn_data, "trajectories_for_camera_night", _boom)

    r = env["client"].get(_traj_url(), headers={"X-Station-Key": env["secret"]})
    assert r.status_code == 200
    assert r.get_json() == {
        "camera": "RO000M", "date": "20260801", "count": 0, "trajectories": [],
    }


def test_trajectories_is_listed_in_the_ingest_index(env):
    r = env["client"].get("/api/ingest/v1")
    assert r.status_code == 200
    assert "trajectories" in r.get_json()["endpoints"]


class TestHeartbeatLastUpdated:
    """A heartbeat carries when the station computed its status, so a status
    frozen on the station reads as old, not as fresh as the push."""

    def test_station_stamp_is_kept(self, env):
        r = env["client"].post(
            "/api/ingest/v1/gmn0002/heartbeat",
            json=_envelope(3, {"online": True, "last_updated": "2026-07-01T21:40:00+00:00"}),
            headers=_hdr(env["secret"]),
        )
        assert r.status_code == 200
        assert env["cache"].status["gmn0002"]["last_updated"] == "2026-07-01T21:40:00+00:00"

    def test_without_one_the_send_time_is_used(self, env):
        env["client"].post(
            "/api/ingest/v1/gmn0002/heartbeat",
            json=_envelope(3, {"online": True}),
            headers=_hdr(env["secret"]),
        )
        assert env["cache"].status["gmn0002"]["last_updated"] == "2026-07-01T22:00:00Z"
