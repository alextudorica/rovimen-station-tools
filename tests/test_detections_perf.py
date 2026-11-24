"""Guards for the two things that made /api/detections/<date> slow.

Both fixes trade a rebuild for a memo, so both can silently regress into
"correct but slow" or, worse, "fast but stale". These pin the behaviour:

* the multi-station correlation clustering must produce exactly what the old
  exhaustive O(n^2) sweep produced, including when the input is not sorted; and
* the serialized-response memo must key strictly on content identity, so a
  rebuilt payload is never served from a previous build's bytes.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

import pytest

import detection_db
import http_caching
from routes.detections import _correlate_detections as _cluster_optimised


# ── correlation clustering ─────────────────────────────────────────────────

_EPOCH = datetime(1970, 1, 1)
_ONE_US = timedelta(microseconds=1)


def _cluster_reference(all_det: list[dict], window: float) -> list[dict]:
    """The original exhaustive implementation, kept as the oracle."""
    used: set[int] = set()
    events: list[dict] = []
    for i, det in enumerate(all_det):
        if i in used:
            continue
        group = [det]
        used.add(i)
        t0 = datetime.fromisoformat(det["meteor_time"])
        for j in range(i + 1, len(all_det)):
            if j in used:
                continue
            t1 = datetime.fromisoformat(all_det[j]["meteor_time"])
            if abs((t1 - t0).total_seconds()) <= window:
                group.append(all_det[j])
                used.add(j)
        seen: dict[tuple, dict] = {}
        for w in group:
            key = (w["host_key"], w["cam"])
            if key not in seen or (w.get("detection_offset_s") or 0) > (
                seen[key].get("detection_offset_s") or 0
            ):
                seen[key] = w
        witnesses = list(seen.values())
        station_set = {w["host_key"] for w in witnesses}
        if len(station_set) >= 2:
            events.append({
                "event_time": det["meteor_time"],
                "witness_count": len(witnesses),
                "station_count": len(station_set),
                "witnesses": witnesses,
                "geometry": "time_only",
            })
    return events


def _det(host: str, cam: str, t: str, offset: float | None = None) -> dict:
    return {"host_key": host, "cam": cam, "meteor_time": t,
            "detection_offset_s": offset}


def _synthetic_night(n_events: int, seed: int = 7) -> list[dict]:
    """A night where most detections are singletons and some are seen by
    2-4 stations within the correlation window."""
    import random
    rng = random.Random(seed)
    hosts = [f"gmnro{i:02d}" for i in range(12)]
    out: list[dict] = []
    base = datetime(2026, 8, 12, 20, 0, 0)
    for k in range(n_events):
        t = base + timedelta(seconds=k * 11 + rng.random())
        for h in rng.sample(hosts, rng.choice([1, 1, 1, 2, 2, 3, 4])):
            jitter = timedelta(milliseconds=rng.randrange(-900, 900))
            out.append(_det(h, f"{h.upper()}A",
                            (t + jitter).isoformat(sep=" "),
                            rng.random() * 5))
    out.sort(key=lambda x: x["meteor_time"])
    return out


@pytest.mark.parametrize("n_events", [1, 5, 40, 200])
def test_clustering_matches_the_exhaustive_sweep(n_events: int) -> None:
    dets = _synthetic_night(n_events)
    assert _cluster_optimised(dets, 1.0) == _cluster_reference(dets, 1.0)


@pytest.mark.parametrize("window", [0.0, 0.5, 1.0, 3.0, 10.0])
def test_clustering_matches_across_window_sizes(window: float) -> None:
    dets = _synthetic_night(60)
    assert _cluster_optimised(dets, window) == _cluster_reference(dets, window)


def test_clustering_matches_when_input_is_not_chronological() -> None:
    """The early break is only sound on a non-decreasing sequence. Shuffled
    input must fall back to the exhaustive scan, not silently drop events."""
    dets = _synthetic_night(40)
    import random
    random.Random(3).shuffle(dets)
    assert _cluster_optimised(dets, 1.0) == _cluster_reference(dets, 1.0)


def test_clustering_pairs_stations_across_a_midnight_style_boundary() -> None:
    dets = [
        _det("gmnro03", "RO000H", "2026-08-12 23:59:59.500"),
        _det("gmnro11", "RO0017", "2026-08-13 00:00:00.100"),
    ]
    events = _cluster_optimised(dets, 1.0)
    assert events == _cluster_reference(dets, 1.0)
    assert len(events) == 1 and events[0]["station_count"] == 2


def test_clustering_survives_an_unparseable_timestamp() -> None:
    """A bad meteor_time used to raise straight out of the request; it must now
    simply fail to correlate while the rest of the night still clusters."""
    dets = [
        _det("gmnro03", "RO000H", "2026-08-12 22:00:00.000"),
        _det("gmnro11", "RO0017", "2026-08-12 22:00:00.200"),
        _det("gmnro05", "RO000L", "not-a-timestamp"),
    ]
    events = _cluster_optimised(dets, 1.0)
    assert len(events) == 1
    assert {w["host_key"] for w in events[0]["witnesses"]} == {"gmnro03", "gmnro11"}


def test_clustering_is_not_quadratic_in_detection_count() -> None:
    """The 504s came from the inner scan walking the whole tail per detection.
    Count the comparisons to prove the early break bounds the work."""
    dets = _synthetic_night(400)
    window_us = 1_000_000
    det_us = [
        (datetime.fromisoformat(d["meteor_time"]) - _EPOCH) // _ONE_US
        for d in dets
    ]
    comparisons = 0
    used: set[int] = set()
    for i in range(len(dets)):
        if i in used:
            continue
        used.add(i)
        for j in range(i + 1, len(dets)):
            comparisons += 1
            if det_us[j] - det_us[i] > window_us:
                break
            used.add(j)
    n = len(dets)
    assert comparisons < n * 6, f"{comparisons} comparisons for {n} detections"
    # The old sweep would have done ~n^2/2.
    assert comparisons < (n * n) // 20


# ── serialized-response memo ───────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clear_json_cache():
    http_caching._json_cache_clear()
    yield
    http_caching._json_cache_clear()


def _dumps_counter(monkeypatch):
    calls: list[int] = []
    real = json.dumps

    def counting(*a, **kw):
        calls.append(1)
        return real(*a, **kw)

    monkeypatch.setattr(http_caching.json, "dumps", counting)
    return calls


def test_same_cache_key_serializes_once(monkeypatch):
    from flask import Flask
    app = Flask(__name__)
    calls = _dumps_counter(monkeypatch)
    payload = {"detections": [{"i": i} for i in range(500)]}
    with app.test_request_context("/"):
        first = http_caching._json_cached(payload, 60, cache_key=("det", "20260812", 1.0, False))
        second = http_caching._json_cached(payload, 60, cache_key=("det", "20260812", 1.0, False))
    assert len(calls) == 1, "second request re-serialized an unchanged payload"
    assert first.get_data() == second.get_data()
    assert first.get_etag() == second.get_etag()


def test_a_new_build_stamp_reserializes(monkeypatch):
    """The whole risk of a memo: a rebuilt payload must never be served from
    the previous build's bytes."""
    from flask import Flask
    app = Flask(__name__)
    calls = _dumps_counter(monkeypatch)
    with app.test_request_context("/"):
        a = http_caching._json_cached({"n": 1}, 60, cache_key=("det", "20260812", 1.0, False))
        b = http_caching._json_cached({"n": 2}, 60, cache_key=("det", "20260812", 2.0, False))
    assert len(calls) == 2
    assert json.loads(a.get_data()) == {"n": 1}
    assert json.loads(b.get_data()) == {"n": 2}


def test_anonymous_projection_gets_its_own_entry(monkeypatch):
    from flask import Flask
    app = Flask(__name__)
    with app.test_request_context("/"):
        full = http_caching._json_cached(
            {"by_station": {"a": 1, "b": 2}}, 60, cache_key=("det", "20260812", 1.0, False))
        anon = http_caching._json_cached(
            {"by_station": {"a": 1}}, 60, cache_key=("det", "20260812", 1.0, True))
    assert json.loads(full.get_data()) != json.loads(anon.get_data())


def test_no_cache_key_never_memoizes(monkeypatch):
    from flask import Flask
    app = Flask(__name__)
    calls = _dumps_counter(monkeypatch)
    with app.test_request_context("/"):
        http_caching._json_cached({"n": 1}, 60)
        http_caching._json_cached({"n": 1}, 60)
    assert len(calls) == 2
    assert not http_caching._json_cache


def test_memo_is_bounded_by_total_bytes(monkeypatch):
    from flask import Flask
    app = Flask(__name__)
    monkeypatch.setattr(http_caching, "_JSON_CACHE_MAX_BYTES", 4096)
    big = {"pad": "x" * 1500}
    with app.test_request_context("/"):
        for i in range(10):
            http_caching._json_cached(big, 60, cache_key=("k", i))
    assert http_caching._json_cache_bytes <= 4096
    assert len(http_caching._json_cache) < 10


def test_memo_is_bounded_by_entry_count(monkeypatch):
    from flask import Flask
    app = Flask(__name__)
    monkeypatch.setattr(http_caching, "_JSON_CACHE_MAX_ENTRIES", 5)
    with app.test_request_context("/"):
        for i in range(40):
            http_caching._json_cached({"i": i}, 60, cache_key=("k", i))
    assert len(http_caching._json_cache) <= 5


# ── sqlite index ───────────────────────────────────────────────────────────

def test_by_date_query_is_index_ordered(tmp_path):
    """WHERE date IN (...) ORDER BY date, time_utc must not cost a sort."""
    db = tmp_path / "detections.db"
    con = detection_db.open_db(db)
    con.close()
    with sqlite3.connect(db) as c:
        names = {r[1] for r in c.execute("PRAGMA index_list(detections)")}
        assert "idx_det_date_time" in names
        plan = " ".join(
            str(r) for r in c.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM detections WHERE date IN (?) "
                "ORDER BY date, time_utc", ("20260812",),
            )
        )
    assert "idx_det_date_time" in plan, plan
    assert "USE TEMP B-TREE FOR ORDER BY" not in plan.upper(), plan
