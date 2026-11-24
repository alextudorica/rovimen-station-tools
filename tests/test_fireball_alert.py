"""VPS side of the live fireball alert: engine rules, messages, ingest route."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from flask import Flask

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "dashboard"))

import fireball_alert as fa  # noqa: E402
import fireball_store  # noqa: E402
import ingest_api  # noqa: E402
import station_keys  # noqa: E402

T = 1_760_571_101.0  # 2025-10-15 23:31:41 UTC

INFO = {
    "RO000A": fa.CamInfo("gmnro01", "Ghirdoveni", 45.30, 26.11),
    "RO000B": fa.CamInfo("gmnro01", "Ghirdoveni", 45.30, 26.11),
    "RO000H": fa.CamInfo("gmnro03", "Ciocârlia", 45.18, 26.41),
    "RO000T": fa.CamInfo("gmnro04", "Vâlcele", 46.17, 26.16),
    "RO000Z": fa.CamInfo("gmnro09", "Sibiciu", 45.74, 26.54),
    "RO0012": fa.CamInfo("gmnro09", "Sibiciu", 45.74, 26.54),
    "RO000U": fa.CamInfo("gmnro04", "Vâlcele", 46.17, 26.16),
    "RO000L": fa.CamInfo("gmnro03", "Ciocârlia", 45.18, 26.41),
    "DE0018": fa.CamInfo("gmnde01", "Berlin", 52.15, 12.82),
    "DE001B": fa.CamInfo("gmnde00", "Johannismühle", 51.62, 13.70),
}


def _spike(cam: str, dt: float, peak: float = 0.2, after: float | None = None) -> dict:
    return {"kind": "fs", "cam": cam, "id": f"{cam}_{T + dt - 1:.2f}", "t_peak": T + dt,
            "t_start": T + dt - 1, "peak": peak, "snr": 50.0, "fwhm": 20, "rise": 12,
            "after": after, "final": True, "qualifies": True,
            "step": after is not None and after > 0.5 * peak, "storm": False}


def _engine() -> fa.Engine:
    return fa.Engine({k: fa.CamInfo(v.station, v.place, v.lat, v.lon) for k, v in INFO.items()})


def _feed(eng: fa.Engine, spikes: list[dict], now: float) -> list[fa.Message]:
    for s in spikes:
        eng.add(INFO[s["cam"]].station, s, now)
    return eng.tick(now)


def test_rule_a_preliminary_then_confirmed() -> None:
    eng = _engine()
    cams = ["RO000A", "RO000B", "RO000H", "RO000T", "RO000Z"]
    eng.add("gmnro03", _fr("RO000H", 300, 1.0, t0=T - 0.5), T + 6)
    msgs = _feed(eng, [_spike(c, 0.1 * k) for k, c in enumerate(cams)], T + 6)
    assert [m.kind for m in msgs] == ["preliminary"]
    assert "5 cameras at 4 stations" in msgs[0].text and "RO000H (Ciocârlia)" in msgs[0].text
    assert "FR files at gmnro03 (Ciocârlia)" in msgs[0].text
    # step levels arrive: all fine
    msgs = _feed(eng, [_spike(c, 0.1 * k, after=0.01) for k, c in enumerate(cams)], T + 22)
    assert [m.kind for m in msgs] == ["confirmed"]
    assert "Rule A holds" in msgs[0].text


def test_rule_a_retracted_when_steps() -> None:
    eng = _engine()
    cams = ["RO000A", "RO000B", "RO000H", "RO000T", "RO000Z"]
    eng.add("gmnro03", _fr("RO000H", 300, 1.0, t0=T - 0.5), T + 6)
    assert [m.kind for m in _feed(eng, [_spike(c, 0.1 * k) for k, c in enumerate(cams)], T + 6)] == ["preliminary"]
    stepped = [_spike(c, 0.1 * k, after=0.15 if c in ("RO000A", "RO000B") else 0.0) for k, c in enumerate(cams)]
    msgs = _feed(eng, stepped, T + 22)
    assert [m.kind for m in msgs] == ["retracted"]
    assert "RO000A (Ghirdoveni)" in msgs[0].text
    assert "3 of 5 cameras pass the step check" in msgs[0].text


def test_rule_a_needs_three_stations_and_distance() -> None:
    eng = _engine()
    # four cameras at two stations: no alert
    assert _feed(eng, [_spike(c, 0) for c in ("RO000A", "RO000B", "RO000H", "DE001B")], T + 6) == []


def _fr(cam: str, n250: int, dur: float, t0: float = T, idx: int = 0) -> dict:
    nfr = int(dur * 25)
    return {"kind": "fr", "cam": cam, "id": f"{cam}_{t0}_{idx}", "file": f"FR_{cam}_{idx}.bin", "line": 0,
            "t0": t0, "fps": 25.0, "tstart": t0, "tend": t0 + dur, "nfr": nfr, "dur": dur,
            "peak": 255, "n250_sum": n250, "nsat_frames": nfr, "flux_sum": 1,
            "t": list(range(nfr)), "hx": [100.0 + k for k in range(nfr)], "hy": [200.0] * nfr}


GOOD = {"ok": True, "why": "ok", "h_a": [95.0, 60.0], "h_b": [90.0, 62.0], "v": [30.0, 31.0],
        "ll_a": [[45.1, 25.9], [45.0, 26.1]], "dt": 0.1}


def test_rule_b_pair_and_veto(monkeypatch) -> None:
    eng = _engine()
    monkeypatch.setattr(eng, "_pair", lambda a, b: GOOD)
    eng.add("gmnro01", _fr("RO000A", 8000, 2.4), T + 13)
    eng.add("gmnro03", _fr("RO000H", 200, 1.2), T + 13)
    msgs = eng.tick(T + 13)
    assert [m.kind for m in msgs] == ["preliminary"]
    assert "95 to 60 km, 30 km/s" in msgs[0].text
    assert [m.kind for m in eng.tick(T + 29)] == ["confirmed"]

    eng2 = _engine()
    monkeypatch.setattr(eng2, "_pair", lambda a, b: GOOD)
    for k in range(3):   # a lamp: three FR files in a minute on RO000A
        eng2.add("gmnro01", _fr("RO000A", 8000, 2.4, t0=T - 40 + 20 * k, idx=k), T + 13)
    eng2.add("gmnro03", _fr("RO000H", 200, 1.2, t0=T + 0.5), T + 13)
    assert eng2.tick(T + 13) == []


def test_rule_a_without_fr_is_silent_until_an_fr_arrives() -> None:
    """2026-11-09 18:09:34 UTC: five faint spikes at four stations, no FR anywhere."""
    eng = _engine()
    cams = ["RO000A", "RO000B", "RO000H", "RO000T", "RO000Z"]
    assert _feed(eng, [_spike(c, 0.1 * k) for k, c in enumerate(cams)], T + 6) == []
    # an FR on a camera outside the cluster, or a lamp camera, does not count
    eng.add("gmnro04", _fr("RO000U", 300, 1.0, t0=T), T + 10)
    for k in range(3):
        eng.add("gmnro01", _fr("RO000A", 300, 1.0, t0=T - 40 + 20 * k, idx=k), T + 10)
    assert eng.tick(T + 10) == []
    # the FR of a cluster camera arrives: the alert goes out then
    eng.add("gmnro09", _fr("RO000Z", 300, 1.5, t0=T - 0.2), T + 14)
    msgs = eng.tick(T + 14)
    assert [m.kind for m in msgs] == ["preliminary"]
    assert "FR files at gmnro09 (Sibiciu)" in msgs[0].text


def test_rule_a_gives_up_waiting_for_fr() -> None:
    eng = _engine()
    cams = ["RO000A", "RO000B", "RO000H", "RO000T", "RO000Z"]
    assert _feed(eng, [_spike(c, 0.1 * k) for k, c in enumerate(cams)], T + 6) == []
    eng.add("gmnro09", _fr("RO000Z", 300, 1.5, t0=T), T + fa.A_FR_WAIT_S + 5)
    assert eng.tick(T + fa.A_FR_WAIT_S + 5) == []


def test_rule_a_strong_cluster_needs_no_fr() -> None:
    eng = _engine()
    cams = ["RO000A", "RO000B", "RO000H", "RO000L", "RO000T", "RO000U", "RO000Z", "RO0012"]
    msgs = _feed(eng, [_spike(c, 0.1 * k) for k, c in enumerate(cams)], T + 6)
    assert [m.kind for m in msgs] == ["preliminary"]
    assert "strong enough without FR files" in msgs[0].text
    msgs = _feed(eng, [_spike(c, 0.1 * k, after=0.01) for k, c in enumerate(cams)], T + 22)
    assert [m.kind for m in msgs] == ["confirmed"]
    # seven cameras would not do
    eng2 = _engine()
    assert _feed(eng2, [_spike(c, 0.1 * k) for k, c in enumerate(cams[:7])], T + 6) == []


def test_rule_a_step_known_before_the_fr_does_not_cancel_the_cluster() -> None:
    """One step camera in a 6-camera cluster: the preliminary stage drops it and
    re-checks the counts, as the confirm stage does, whatever order the step
    update and the FR arrive in."""
    eng = _engine()
    cams = ["RO000A", "RO000B", "RO000H", "RO000L", "RO000T", "RO000Z"]   # 6 cameras at 4 stations
    assert _feed(eng, [_spike(c, 0.1 * k) for k, c in enumerate(cams)], T + 6) == []   # no FR yet
    # RO000A's level 6-10 s later arrives first: a step (a light or a gain change)
    assert _feed(eng, [_spike("RO000A", 0.0, after=0.15)], T + 8) == []
    # then the FR of a cluster camera: the other five cameras at four stations still hold
    eng.add("gmnro03", _fr("RO000H", 300, 1.0, t0=T - 0.5), T + 12)
    msgs = eng.tick(T + 12)
    assert [m.kind for m in msgs] == ["preliminary"]
    assert "5 cameras at 4 stations" in msgs[0].text and "RO000A (Ghirdoveni)" not in msgs[0].text
    rest = [_spike(c, 0.1 * k, after=0.01) for k, c in enumerate(cams) if c != "RO000A"]
    msgs = _feed(eng, rest, T + 28)
    assert [m.kind for m in msgs] == ["confirmed"]
    assert "Rule A holds: 5 cameras at 4 stations" in msgs[0].text


def test_pull_reach_leaves_out_far_stations() -> None:
    eng = _engine()
    al = fa.Alert("x", T, T, rule_a={"spikes": [{"cam": c, "station": INFO[c].station}
                                                for c in ("RO000A", "RO000H", "RO000T")]})
    near, far = fa.stations_in_reach(al, eng.info, ["gmnro01", "gmnro03", "gmnro04", "gmnro09", "gmnde00", "gmnde01"])
    assert near == ["gmnro01", "gmnro03", "gmnro04", "gmnro09"]
    assert far == ["gmnde00", "gmnde01"]


def test_fr_files_near_counts_only_the_event(tmp_path) -> None:
    import zipfile
    z = tmp_path / "RO000A.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for name in ("FR_RO000A_20251015_233135_000_0012800.bin", "FR_RO000A_20251015_232900_000_0000000.bin",
                     "FF_RO000A_20251015_233135_000_0012800.fits"):
            zf.writestr(name, b"x")
    assert fa.fr_files_near(z, T) == 1
    assert fa.fr_files_near(tmp_path / "missing.zip", T) == 0


def test_withdraw_goes_public_and_turns_the_lamp_off() -> None:
    sent: list[tuple[str, str]] = []

    class Out:
        def send(self, text: str, kind: str = "") -> bool:
            sent.append((kind, text))
            return True

    class Sig:
        kinds: list[str] = []

        def send(self, msg, info):
            self.kinds.append(msg.kind)

    sig = Sig()
    al = fa.Alert("20251015_233141", T, T, state="confirmed")
    fa.withdraw(al, "no FR files.", Out(), sig, INFO)
    assert al.state == "withdrawn" and sig.kinds == ["retracted"]
    assert sent[0][0] == "withdrawn" and "Correction: false alarm" in sent[0][1]
    import alert_channels as ac
    assert "withdrawn" in ac.COMMUNITY and "withdrawn" in ac.EMAIL


# ── the pull after a confirmed alert, and the automatic withdrawal ────

ALERT_CAMS = ["RO000A", "RO000B", "RO000H", "RO000T", "RO000Z"]   # rule A: 5 cameras at 4 stations
OTHER_CAMS = ["RO000L", "RO000U", "RO0012"]                       # near, but not in the alert


class _PackResp:
    def __init__(self, body: bytes) -> None:
        self.status_code = 200
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def iter_content(self, n: int):
        yield self.body


class _PackSession:
    """Stands in for requests.Session against /api/fireball_pack: each camera
    answers with a pack (FR bin near the event or FF files only) or times out."""

    def __init__(self, fr: set[str], slow: set[str]) -> None:
        self.fr, self.slow = fr, slow
        self.asked: list[str] = []

    def __call__(self) -> "_PackSession":   # requests.Session() returns the session itself
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def get(self, url: str, **kw):
        import io
        import zipfile
        cam = url.split("/api/fireball_pack/")[1].split("/")[0]
        self.asked.append(cam)
        if cam in self.slow:
            raise TimeoutError(f"{cam} pack not ready")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(f"frames/FF_{cam}_20251015_233135_000_0012800.fits", b"x")
            if cam in self.fr:
                zf.writestr(f"frames/FR_{cam}_20251015_233135_000_0012800.bin", b"x")
        return _PackResp(buf.getvalue())


class _Outlet:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, text: str, kind: str) -> bool:
        self.sent.append((kind, text))
        return True

    def send_photos(self, photos, kind: str) -> bool:
        return True


class _Signals:
    def __init__(self) -> None:
        self.kinds: list[str] = []

    def send(self, msg, info) -> list:
        self.kinds.append(msg.kind)
        return []


def _pull(monkeypatch, tmp_path, al: fa.Alert, fr: set[str], slow: set[str]) -> tuple[_Outlet, _Signals, _PackSession]:
    import types

    import requests
    stations: dict[str, list[str]] = {}
    for cam in ALERT_CAMS + OTHER_CAMS:
        stations.setdefault(INFO[cam].station, []).append(cam)
    cfg = types.SimpleNamespace(station_api_port=7779, stations={
        hk: types.SimpleNamespace(should_contact=True, jump_hosts=[], host_for_path=lambda p: "127.0.0.1",
                                  cameras=[types.SimpleNamespace(code=c) for c in cams])
        for hk, cams in stations.items()})
    session = _PackSession(fr, slow)
    monkeypatch.setattr(requests, "Session", session)
    monkeypatch.setattr(fa, "EVENTS_DIR", tmp_path)
    out, sig = _Outlet(), _Signals()
    fa._safe_pull(al, cfg, _engine(), out, sig)
    return out, sig, session


def _confirmed_rule_a() -> fa.Alert:
    hit = {"t": T, "span": 0.4, "tier": "fr", "fr_stations": ["gmnro03"],
           "spikes": [{"cam": c, "station": INFO[c].station, "t_peak": T, "peak": 0.2} for c in ALERT_CAMS]}
    return fa.Alert(fa.event_id(T), T, T, rule_a=hit, confirmed_a=hit, state="confirmed")


def test_real_fireball_with_slow_alert_cameras_is_not_withdrawn_by_unrelated_packs(monkeypatch, tmp_path) -> None:
    """Three of the alert's cameras time out, two answer without an FR in the window
    (the fireball outside their field), three cameras outside the alert answer with
    nothing: eight packs asked, five arrived, no FR in any, yet only two are the
    alert's own, so the alert stands."""
    out, sig, session = _pull(monkeypatch, tmp_path, _confirmed_rule_a(), fr=set(),
                              slow={"RO000A", "RO000H", "RO000Z"})
    assert sorted(session.asked) == sorted(ALERT_CAMS + OTHER_CAMS)
    assert [k for k, _ in out.sent] == ["pulled"] and sig.kinds == []


def test_fr_pair_alert_is_never_withdrawn(monkeypatch, tmp_path) -> None:
    """A rule B alert exists because two stations already sent FR tracks."""
    al = _confirmed_rule_a()
    al.pairs = [{"t": T, "a": _fr("RO000A", 8000, 2.4), "b": _fr("RO000H", 200, 1.2), "geo": GOOD}]
    out, sig, _ = _pull(monkeypatch, tmp_path, al, fr=set(), slow=set())
    assert [k for k, _ in out.sent] == ["pulled"] and sig.kinds == [] and al.state == "confirmed"


def test_fieldsum_false_alarm_is_still_withdrawn(monkeypatch, tmp_path) -> None:
    """The alert's own cameras answered and none recorded a bright moving object;
    an FR on a camera outside the alert does not save it."""
    al = _confirmed_rule_a()
    out, sig, _ = _pull(monkeypatch, tmp_path, al, fr={"RO000U"}, slow={"RO000A"})
    assert [k for k, _ in out.sent] == ["pulled", "withdrawn"]
    assert "none of the 4 cameras in the alert" in out.sent[1][1]
    assert al.state == "withdrawn" and sig.kinds == ["retracted"]
    # one FR bin near the event on an alert camera keeps it
    out, sig, _ = _pull(monkeypatch, tmp_path, _confirmed_rule_a(), fr={"RO000T"}, slow=set())
    assert [k for k, _ in out.sent] == ["pulled"] and sig.kinds == []


def test_rule_b_brightness_gate(monkeypatch) -> None:
    eng = _engine()
    monkeypatch.setattr(eng, "_pair", lambda a, b: GOOD)
    eng.add("gmnro01", _fr("RO000A", 4000, 2.4), T + 13)
    eng.add("gmnro03", _fr("RO000H", 200, 1.2), T + 13)
    assert eng.tick(T + 13) == []


def test_night_and_ids() -> None:
    assert fa.night_of(T) == "20251015"
    assert fa.event_id(T) == "20251015_233141"
    assert fa.Engine(INFO).events_url(T).endswith(
        "/events?tab=grid&date=20251015&from=23:30&to=23:33&gfrom=23:30&gto=23:33")


# ── ingest route ──────────────────────────────────────────────────────

@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ROVIMEN_STATION_KEYS_REQUIRED", "1")
    monkeypatch.setattr(fireball_store, "DB_PATH", tmp_path / "fb.db")
    station_keys._invalidate_cache()
    keys = tmp_path / "keys.yaml"
    _, secret = station_keys.add_key("gmnro01", path=keys)
    station_keys._invalidate_cache()
    app = Flask(__name__)
    app.testing = True
    ingest_api.register_ingest_routes(
        app, cache_set_status=lambda *a: None, cache_set_vitals=lambda *a: None,
        known_stations=lambda: {"gmnro01", "gmnro03"},
        cam_owners=lambda cam: {"gmnro01"} if cam in ("RO000A", "RO000B") else {"gmnro03"},
        detections_db_path=tmp_path / "det.db", station_state_path=tmp_path / "st.db",
        station_keys_path=keys, limiter=None)
    yield app.test_client(), secret, tmp_path / "fb.db"
    station_keys._invalidate_cache()


def _post(c, secret: str, events: list[dict], station: str = "gmnro01"):
    return c.post(f"/api/ingest/v1/{station}/fireball", headers={"X-Station-Key": secret},
                  json={"seq": 1, "payload": {"events": events}})


def test_ingest_stores_events_and_platepars(client) -> None:
    c, secret, db = client
    pp = {"kind": "platepar", "cam": "RO000A", "id": "x", "mtime": 5, "pp": {"F_scale": 1}}
    r = _post(c, secret, [_spike("RO000A", 0), pp])
    assert r.status_code == 200, r.get_json()
    rows = fireball_store.read_since(0, path=db)
    assert [r[3]["kind"] for r in rows] == ["fs", "platepar"]
    assert fireball_store.platepars(path=db)["RO000A"][1]["mtime"] == 5
    # same seq again is still stored: the route is not seq-gated
    assert _post(c, secret, [_spike("RO000A", 1)]).status_code == 200
    assert len(fireball_store.read_since(0, path=db)) == 3


def test_ingest_refuses_foreign_camera_bad_kind_and_wrong_key(client) -> None:
    c, secret, _db = client
    assert _post(c, secret, [_spike("RO000H", 0)]).status_code == 403
    assert _post(c, secret, [{"kind": "shell", "cam": "RO000A"}]).status_code == 422
    assert _post(c, "nope", [_spike("RO000A", 0)]).status_code == 401
    assert _post(c, secret, [_spike("RO000A", 0)], station="gmnro03").status_code == 403


def test_speed_text_uses_highest_and_lowest_camera() -> None:
    g = {"v": [16.7, 12.4], "v_prof": [[17.2, 102.0, 72.0, 14.0, 64.0, 53.0],
                                       [16.7, 55.0, 38.0, 7.0, 34.0, 29.0]]}
    assert fa.Engine._speed_text(g) == "17 km/s at 102-72 km, slowing to 7 km/s at 34-29 km"
    steady = {"v": [30.0], "v_prof": [[30.0, 95.0, 80.0, 29.0, 70.0, 60.0]]}
    assert fa.Engine._speed_text(steady) == "30 km/s at 95-80 km"
    assert fa.Engine._speed_text({"v": [30.0, 31.0]}) == "30 km/s"


def test_clip_url_iso_night_and_mp4() -> None:
    assert fa.clip_url("RO0003", "20261021", "RO0003_20261021_201621_color.mkv", True) == \
        f"{fa.PUBLIC_BASE}/media/v1/clip/RO0003/2026-10-21/RO0003_20261021_201621_color.mp4"
    assert fa.clip_url("RO0003", "20261021", "RO0003_20261021_201621_color.mkv", False).endswith(
        "/2026-10-21/RO0003_20261021_201621_color.mkv")
