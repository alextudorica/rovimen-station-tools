"""Event grouping behind the Highlights page (dashboard/highlight_events.py)."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))

import highlight_events as he  # noqa: E402

DATE = "2026-10-15"


def _det(cam: str, host: str, t: str, chunk: str, mag: float | None = -2.0, **kw) -> dict:
    return {
        "cam": cam,
        "host_key": host,
        "station_label": host.title(),
        "filename": f"{cam}_20261015_{chunk}_color.mkv",
        "stack": f"{cam}_20261015_{chunk}_stack.webp",
        "meteor_time": t,
        "rms": {"mag_apparent": mag, "duration_s": 1.5, "shower": None},
        **kw,
    }


def _gmn(ev_id: str, t: str, cams: list[str], mag: float = -6.0) -> dict:
    return {"id": ev_id, "time": t, "stations": cams, "peak_mag": mag,
            "duration_s": 3.2, "shower": "ORI", "velocity": 60.0,
            "altitude_begin": 110.0, "altitude_end": 70.0}


def test_gmn_event_gathers_every_camera_on_the_trajectory() -> None:
    dets = [
        _det("RO000Z", "gmnro09", "2026-10-15T22:51:41.2", "225128"),
        _det("RO000B", "gmnro01", "2026-10-15T22:51:42.0", "225133"),
        _det("RO000T", "gmnro04", "2026-10-15T22:51:40.5", "225136"),
        _det("RO000T", "gmnro04", "2026-10-15T23:10:00", "230950"),   # another meteor
    ]
    gmn = [_gmn("g1", "2026-10-15 22:51:41.000", ["RO000Z", "RO000B", "RO000T", "XX0001"])]
    events = he.night_events(dets, gmn, [], DATE)

    assert len(events) == 1
    ev = events[0]
    assert ev["confirmed"] and ev["fireball"]
    assert ev["peak_magnitude"] == -6.0 and ev["magnitude_kind"] == "absolute"
    assert {w["camera"] for w in ev["witnesses"]} == {"RO000Z", "RO000B", "RO000T"}
    assert ev["station_count"] == 3
    assert ev["gmn_cameras"] == ["RO000B", "RO000T", "RO000Z", "XX0001"]
    w = next(w for w in ev["witnesses"] if w["camera"] == "RO000Z")
    assert w["clip_url"] == f"/api/highlights/clip/RO000Z/{DATE}/RO000Z_20261015_225128_color.mkv"
    assert w["stack_url"] == f"/api/highlights/stack/RO000Z/{DATE}/RO000Z_20261015_225128_stack.webp"
    assert w["detection_offset_s"] == 13.2


def test_gmn_event_speed_is_the_entry_speed_never_vg() -> None:
    """Vg drops below the 11.2 km/s minimum entry speed for a slow meteoroid
    (4.7 km/s, RO001F + RO001U on 2026-11-20), so "Speed" carries Vinit only."""
    det = _det("RO001U", "gmnro20", "2026-10-15T22:24:19.8", "222412")
    slow = {**_gmn("g1", "2026-10-15T22:24:19.8", ["RO001U", "RO001F"]),
            "velocity": 4.7, "velocity_init": 11.8}
    assert he.night_events([det], [slow], [], DATE)[0]["velocity_kms"] == 11.8
    no_init = _gmn("g1", "2026-10-15T22:24:19.8", ["RO001U", "RO001F"])
    assert he.night_events([det], [no_init], [], DATE)[0]["velocity_kms"] is None


def test_coincidence_already_in_a_gmn_event_is_not_shown_twice() -> None:
    a = _det("RO000Z", "gmnro09", "2026-10-15T22:51:41", "225128")
    b = _det("RO000B", "gmnro01", "2026-10-15T22:51:41", "225133")
    gmn = [_gmn("g1", "2026-10-15T22:51:41", ["RO000Z", "RO000B"])]
    coinc = [{"event_time": "2026-10-15T22:51:41", "witnesses": [a, b]}]
    events = he.night_events([a, b], gmn, coinc, DATE)
    assert [e["id"] for e in events] == ["gmn-g1"]


def test_unsolved_coincidence_ranks_after_confirmed_events() -> None:
    a = _det("RO000Z", "gmnro09", "2026-10-15T20:00:00", "195950", mag=-8.0)
    b = _det("RO000B", "gmnro01", "2026-10-15T20:00:01", "195955", mag=-7.0)
    c = _det("RO000T", "gmnro04", "2026-10-15T21:00:00", "205950")
    d = _det("RO000Q", "gmnro02", "2026-10-15T21:00:00", "205955")
    gmn = [_gmn("g2", "2026-10-15T21:00:00", ["RO000T", "RO000Q"], mag=-1.0)]
    coinc = [{"event_time": "2026-10-15T20:00:00", "witnesses": [a, b]}]
    events = he.night_events([a, b, c, d], gmn, coinc, DATE)
    events.sort(key=he.rank_key)
    assert [e["confirmed"] for e in events] == [True, False]
    rv = events[1]
    assert rv["peak_magnitude"] == -8.0 and rv["magnitude_kind"] == "apparent"
    assert rv["fireball"]


def test_witness_without_archived_clip_keeps_its_stack() -> None:
    a = _det("RO000Z", "gmnro09", "2026-10-15T22:51:41", "225128", pending=True)
    b = _det("RO000B", "gmnro01", "2026-10-15T22:51:41", "225133")
    ev = he.night_events([a, b], [_gmn("g1", "2026-10-15T22:51:41", ["RO000Z", "RO000B"])], [], DATE)[0]
    tiles = {w["camera"]: w for w in ev["witnesses"]}
    assert tiles["RO000Z"]["clip_url"] is None and tiles["RO000Z"]["stack_url"]
    assert ev["witnesses"][0]["camera"] == "RO000B"     # clips first


def test_event_with_no_clip_is_dropped() -> None:
    a = _det("RO000Z", "gmnro09", "2026-10-15T22:51:41", "225128", no_clip=True)
    gmn = [_gmn("g1", "2026-10-15T22:51:41", ["RO000Z", "RO000B"])]
    assert he.night_events([a], gmn, [], DATE) == []


def test_visible_filter_drops_hidden_cameras() -> None:
    a = _det("RO000Z", "gmnro09", "2026-10-15T22:51:41", "225128")
    b = _det("RO000B", "gmnro01", "2026-10-15T22:51:41", "225133")
    gmn = [_gmn("g1", "2026-10-15T22:51:41", ["RO000Z", "RO000B"])]
    coinc = [{"event_time": "2026-10-15T22:51:41", "witnesses": [a, b]}]
    ev = he.night_events([a, b], gmn, coinc, DATE, visible=lambda w: w["camera"] != "RO000B")[0]
    assert [w["camera"] for w in ev["witnesses"]] == ["RO000Z"]


def test_duplicate_chunks_of_one_camera_collapse_to_one_tile() -> None:
    a1 = _det("RO000Z", "gmnro09", "2026-10-15T22:51:41", "225128")
    a2 = _det("RO000Z", "gmnro09", "2026-10-15T22:51:41", "225138")
    b = _det("RO000B", "gmnro01", "2026-10-15T22:51:41", "225133")
    ev = he.night_events([a1, a2, b], [_gmn("g1", "2026-10-15T22:51:41", ["RO000Z", "RO000B"])], [], DATE)[0]
    z = [w for w in ev["witnesses"] if w["camera"] == "RO000Z"]
    assert len(z) == 1 and z[0]["filename"].endswith("225128_color.mkv")   # more pre-event context


def test_reports_attach_by_clip_or_time() -> None:
    clip = SimpleNamespace(camera="RO000Z", filename="RO000Z_20261015_225128_color.mkv")
    by_clip = SimpleNamespace(event_id="20261015_225141", url="/fireball/20261015_225141",
                              title="Oct 15 fireball", status="final", clips=[clip])
    by_time = SimpleNamespace(event_id="20261011_182306", url="/fireball/20261011_182306",
                              title="Oct 11 fireball", status="preliminary", clips=[])
    events = [
        {"time_utc": "2026-10-15T22:51:45", "witnesses": [{"camera": "RO000Z", "filename": clip.filename}]},
        {"time_utc": "2026-10-11T18:23:10", "witnesses": [{"camera": "RO000A", "filename": "x.mkv"}]},
        {"time_utc": "2026-10-11T19:00:00", "witnesses": []},
    ]
    he.attach_reports(events, [by_clip, by_time])
    assert events[0]["report"]["url"] == "/fireball/20261015_225141"
    assert events[1]["report"]["status"] == "preliminary"
    assert events[2]["report"] is None
