"""clip_requester: asks stations for the detection clips the archive lacks."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

import clip_requester
from clip_requester import DONE, FAILED, GONE, REFUSED, REQUESTED

NIGHT = "20260927"
# 13:30 UTC the day after the night: past READY_HOUR_UTC, sun up in Romania.
DAY = datetime(2026, 9, 28, 13, 30, tzinfo=timezone.utc)
T = "2026-09-28T00:54:49"
CHUNK_A = "RO000V_20260928_005448_color.mkv"
CHUNK_B = "RO0005_20260928_005437_color.mkv"


def _config(**b_overrides):
    b = {"ip": "100.1.1.2", "label": "Bistrița", "ssh_user": "rmsuser",
         "lat": 47.1, "lon": 24.5,
         "cameras": [{"code": "RO0005", "cam_ip": "192.168.1.5", "label": "N"}]}
    b.update(b_overrides)
    return clip_requester.DashboardConfig.model_validate({
        "correlation_window_s": 1,
        "station_api_port": 7779,
        "stations": {
            "gmnro04": {"ip": "100.1.1.1", "label": "Vâlcele", "ssh_user": "gmn",
                        "lat": 46.0, "lon": 25.8,
                        "cameras": [{"code": "RO000V", "cam_ip": "192.168.1.9",
                                     "label": "S"}]},
            "gmnro05": b,
        },
    })


def _row(cam, chunk, t=T, mag=-1.0, night=NIGHT):
    return {"cam": cam, "date": night, "time_utc": t, "chunk_file": chunk,
            "mag_apparent": mag}


def _archive(tmp_path, *, a_has_clip=True, b_files=None):
    root = tmp_path / "box"
    (root / "RO000V" / NIGHT / "meteors").mkdir(parents=True, exist_ok=True)
    if a_has_clip:
        (root / "RO000V" / NIGHT / "meteors" / CHUNK_A).write_bytes(b"x")
    (root / "RO0005" / NIGHT / "stacks").mkdir(parents=True, exist_ok=True)
    if b_files is not None:
        (root / "RO0005" / NIGHT / "meteors").mkdir(exist_ok=True)
        for name in b_files:
            (root / "RO0005" / NIGHT / "meteors" / name).write_bytes(b"x")
    return root


class _Resp:
    def __init__(self, code, body=None):
        self.status_code = code
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class _Session:
    """Scripted station answers; the last one repeats. Records every POST URL.
    The GET probe answers ``probe`` (a status code, or a body dict for 200),
    or ``probe(url)`` when it is callable."""

    def __init__(self, *codes, probe=None):
        self.codes = list(codes) or [200]
        self.urls: list[str] = []
        self.gets: list[str] = []
        self.probe = {"dawn_running": False, "idle_supported": True} if probe is None else probe

    def get(self, url, timeout):
        self.gets.append(url)
        probe = self.probe(url) if callable(self.probe) else self.probe
        if isinstance(probe, Exception):
            raise probe
        if isinstance(probe, int):
            return _Resp(probe)
        return _Resp(200, probe)

    def post(self, url, timeout):
        self.urls.append(url)
        code = self.codes.pop(0) if len(self.codes) > 1 else self.codes[0]
        if isinstance(code, Exception):
            raise code
        return _Resp(code)


def _run(tmp_path, rows, session, now=DAY, config=None, root=None, **kw):
    return clip_requester.run(
        config or _config(), now, nights=1, max_per_station=kw.pop("max_per_station", 10),
        archive_root=root or _archive(tmp_path), state_path=tmp_path / "state.json",
        session=session, rows=rows, **kw)


def _state(tmp_path):
    return json.loads((tmp_path / "state.json").read_text())


# ── what is wanted ───────────────────────────────────────────────────────────


def test_missing_witness_clip_is_requested_with_idle(tmp_path):
    s = _Session(200)
    summary = _run(tmp_path, [_row("RO000V", CHUNK_A), _row("RO0005", CHUNK_B)], s)
    assert s.urls == [
        f"http://100.1.1.2:7779/api/process_clip/RO0005/{NIGHT}/{CHUNK_B}?idle=1"]
    assert summary["gmnro05"]["requested"] == 1
    assert "gmnro04" not in summary  # its clip is in the archive
    assert _state(tmp_path)[f"RO0005/{NIGHT}/{CHUNK_B}"]["status"] == REQUESTED


def test_single_station_detection_is_requested(tmp_path):
    s = _Session(200)
    _run(tmp_path, [_row("RO0005", CHUNK_B)], s)
    assert s.urls == [
        f"http://100.1.1.2:7779/api/process_clip/RO0005/{NIGHT}/{CHUNK_B}?idle=1"]


def test_single_station_clip_already_archived_is_not_requested(tmp_path):
    root = _archive(tmp_path, b_files=[CHUNK_B])
    s = _Session(200)
    _run(tmp_path, [_row("RO0005", CHUNK_B)], s, root=root)
    assert s.urls == []


def test_witnesses_go_before_brighter_single_station_detections(tmp_path):
    # A -4 single-station meteor at 02:00 and a +2 witness at 00:54 (seen
    # by RO000V too): the witness is asked for first, then the rest.
    lone = "RO0005_20260928_020000_color.mkv"
    rows = [_row("RO000V", CHUNK_A), _row("RO0005", CHUNK_B, mag=2.0),
            _row("RO0005", lone, t="2026-09-28T02:00:10", mag=-4.0)]
    s = _Session(200)
    _run(tmp_path, rows, s, max_per_station=1)
    assert [u.split("/")[-1].split("?")[0] for u in s.urls] == [CHUNK_B]
    s2 = _Session(200)
    _run(tmp_path, rows, s2, max_per_station=1)
    assert [u.split("/")[-1].split("?")[0] for u in s2.urls] == [lone]


def test_two_detections_in_one_chunk_are_one_request(tmp_path):
    rows = [_row("RO0005", CHUNK_B, mag=1.0),
            _row("RO0005", CHUNK_B, t="2026-09-28T00:54:55", mag=-2.0)]
    s = _Session(200)
    _run(tmp_path, rows, s)
    assert len(s.urls) == 1


def test_stitched_splice_in_the_archive_counts_as_delivered(tmp_path):
    root = _archive(tmp_path, b_files=["RO0005_20260928_005429_005449_color.mkv"])
    s = _Session(200)
    _run(tmp_path, [_row("RO000V", CHUNK_A), _row("RO0005", CHUNK_B)], s, root=root)
    assert s.urls == []


def test_pruned_clip_is_not_requested_again(tmp_path):
    root = _archive(tmp_path)
    (root / "RO0005" / NIGHT / "state.json").write_text(
        json.dumps({"chunks": {CHUNK_B: {"video_pruned": True}}}))
    s = _Session(200)
    _run(tmp_path, [_row("RO000V", CHUNK_A), _row("RO0005", CHUNK_B)], s, root=root)
    assert s.urls == []


def test_unmounted_archive_requests_nothing(tmp_path):
    """No camera directory at all reads as "box not mounted", not as missing."""
    s = _Session(200)
    _run(tmp_path, [_row("RO000V", CHUNK_A), _row("RO0005", CHUNK_B)], s,
         root=tmp_path / "empty")
    assert s.urls == []


def test_commissioning_station_is_never_contacted(tmp_path):
    s = _Session(200)
    _run(tmp_path, [_row("RO000V", CHUNK_A), _row("RO0005", CHUNK_B)], s,
         config=_config(status="commissioning"))
    assert s.urls == []


def test_camera_without_a_clip_pipeline_is_skipped(tmp_path):
    s = _Session(200)
    cfg = _config(cameras=[{"code": "RO0005", "cam_ip": "192.168.1.5", "label": "N",
                            "clips": False}])
    _run(tmp_path, [_row("RO000V", CHUNK_A), _row("RO0005", CHUNK_B)], s, config=cfg)
    assert s.urls == []


def test_brightest_first_and_capped_per_station(tmp_path):
    rows = []
    for i, mag in enumerate((2.0, -3.0, 0.5)):
        t = f"2026-09-28T01:0{i}:10"
        rows += [_row("RO000V", f"RO000V_20260928_010{i}00_color.mkv", t),
                 _row("RO0005", f"RO0005_20260928_010{i}00_color.mkv", t, mag)]
    root = _archive(tmp_path, a_has_clip=False)
    s = _Session(200)
    _run(tmp_path, rows, s, root=root, max_per_station=2)
    b_urls = [u for u in s.urls if "RO0005" in u]
    assert [u.split("/")[-1].split("?")[0] for u in b_urls] == [
        "RO0005_20260928_010100_color.mkv", "RO0005_20260928_010200_color.mkv"]


# ── when ─────────────────────────────────────────────────────────────────────


def test_night_is_not_due_before_noon_the_next_day():
    assert clip_requester.recent_nights(datetime(2026, 9, 28, 11, 59, tzinfo=timezone.utc), 1) \
        == ["20260926"]
    assert clip_requester.recent_nights(datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc), 2) \
        == ["20260927", "20260926"]


def test_nothing_is_requested_after_dark(tmp_path):
    s = _Session(200)
    summary = _run(tmp_path, [_row("RO000V", CHUNK_A), _row("RO0005", CHUNK_B)], s,
                   now=datetime(2026, 9, 28, 20, 0, tzinfo=timezone.utc))
    assert s.urls == []
    assert summary["gmnro05"] == {"missing": 1, "night": 1}


def test_station_without_coordinates_is_never_asked(tmp_path):
    s = _Session(200)
    _run(tmp_path, [_row("RO000V", CHUNK_A), _row("RO0005", CHUNK_B)], s,
         config=_config(lat=None, lon=None))
    assert s.urls == []


# ── station answers ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("code,status", [(409, DONE), (404, GONE)])
def test_terminal_answers_are_never_asked_again(tmp_path, code, status):
    rows = [_row("RO000V", CHUNK_A), _row("RO0005", CHUNK_B)]
    s = _Session(code)
    _run(tmp_path, rows, s)
    assert _state(tmp_path)[f"RO0005/{NIGHT}/{CHUNK_B}"]["status"] == status
    later = _Session(200)
    _run(tmp_path, rows, later, now=DAY.replace(hour=15))
    assert later.urls == []


def test_dawn_busy_stops_the_station_for_this_pass(tmp_path):
    rows = []
    for i in range(3):
        t = f"2026-09-28T01:0{i}:10"
        rows += [_row("RO000V", f"RO000V_20260928_010{i}00_color.mkv", t),
                 _row("RO0005", f"RO0005_20260928_010{i}00_color.mkv", t)]
    s = _Session(503)
    summary = _run(tmp_path, rows, s, root=_archive(tmp_path, a_has_clip=False))
    assert len([u for u in s.urls if "RO0005" in u]) == 1
    assert summary["gmnro05"]["busy"] == 1
    # Not counted as an attempt: the next pass asks straight away.
    nxt = _Session(200)
    _run(tmp_path, rows, nxt, now=DAY.replace(minute=40),
         root=_archive(tmp_path, a_has_clip=False))
    assert len([u for u in nxt.urls if "RO0005" in u]) == 3


def test_unreachable_station_stops_the_pass(tmp_path):
    s = _Session(requests.ConnectionError("down"))
    summary = _run(tmp_path, [_row("RO000V", CHUNK_A), _row("RO0005", CHUNK_B)], s)
    assert summary["gmnro05"]["error"] == 1
    assert f"RO0005/{NIGHT}/{CHUNK_B}" not in _state(tmp_path)


def test_requested_clip_is_asked_again_only_after_half_an_hour(tmp_path):
    rows = [_row("RO000V", CHUNK_A), _row("RO0005", CHUNK_B)]
    _run(tmp_path, rows, _Session(200))
    soon = _Session(200)
    _run(tmp_path, rows, soon, now=DAY.replace(minute=55))
    assert soon.urls == []
    later = _Session(200)
    _run(tmp_path, rows, later, now=DAY.replace(hour=14, minute=1))
    assert len(later.urls) == 1
    assert _state(tmp_path)[f"RO0005/{NIGHT}/{CHUNK_B}"]["attempts"] == 2


def test_gives_up_after_max_attempts(tmp_path):
    rows = [_row("RO000V", CHUNK_A), _row("RO0005", CHUNK_B)]
    (tmp_path / "state.json").write_text(json.dumps({
        f"RO0005/{NIGHT}/{CHUNK_B}": {"status": REQUESTED, "last": 0,
                                      "attempts": clip_requester.MAX_ATTEMPTS}}))
    s = _Session(200)
    summary = _run(tmp_path, rows, s)
    assert s.urls == []
    assert summary["gmnro05"]["failed"] == 1
    assert _state(tmp_path)[f"RO0005/{NIGHT}/{CHUNK_B}"]["status"] == FAILED


def test_dry_run_posts_nothing_and_writes_no_state(tmp_path):
    s = _Session(200)
    summary = _run(tmp_path, [_row("RO000V", CHUNK_A), _row("RO0005", CHUNK_B)], s,
                   dry_run=True)
    assert s.urls == []
    assert summary["gmnro05"]["requested"] == 1
    assert not (tmp_path / "state.json").exists()


def test_camera_host_override_routes_the_request(tmp_path):
    s = _Session(200)
    cfg = _config(cameras=[{"code": "RO0005", "cam_ip": "192.168.1.5", "label": "N",
                            "host": "100.9.9.9"}])
    _run(tmp_path, [_row("RO000V", CHUNK_A), _row("RO0005", CHUNK_B)], s, config=cfg)
    assert s.urls[0].startswith("http://100.9.9.9:7779/")


# ── capability probe ─────────────────────────────────────────────────────────


def test_old_bundle_is_left_alone(tmp_path):
    """An older station takes a POST at once, dawn or not: never send one."""
    s = _Session(200, probe=405)
    summary = _run(tmp_path, [_row("RO000V", CHUNK_A), _row("RO0005", CHUNK_B)], s)
    assert s.urls == [] and len(s.gets) == 1
    assert summary["gmnro05"]["old_bundle"] == 1
    assert "?" not in s.gets[0]


def test_probe_seeing_dawn_skips_the_pass(tmp_path):
    s = _Session(200, probe={"dawn_running": True})
    summary = _run(tmp_path, [_row("RO000V", CHUNK_A), _row("RO0005", CHUNK_B)], s)
    assert s.urls == []
    assert summary["gmnro05"]["busy"] == 1


def test_one_probe_per_station_per_pass(tmp_path):
    rows = []
    for i in range(3):
        t = f"2026-09-28T01:0{i}:10"
        rows += [_row("RO000V", f"RO000V_20260928_010{i}00_color.mkv", t),
                 _row("RO0005", f"RO0005_20260928_010{i}00_color.mkv", t)]
    s = _Session(200)
    _run(tmp_path, rows, s, root=_archive(tmp_path, a_has_clip=False))
    assert len([g for g in s.gets if "RO0005" in g]) == 1
    assert len([u for u in s.urls if "RO0005" in u]) == 3


def test_nothing_due_means_no_probe(tmp_path):
    rows = [_row("RO000V", CHUNK_A), _row("RO0005", CHUNK_B)]
    _run(tmp_path, rows, _Session(409))
    again = _Session(200)
    _run(tmp_path, rows, again, now=DAY.replace(hour=15))
    assert again.gets == [] and again.urls == []


# ── refused clips never hold the queue ───────────────────────────────────────

# 2026-10-04/05: the brightest missing witness at gmnro05 (Bistrița) was a
# boundary splice, which the station's process_clip answered 400. It was probed
# first on every pass and the pass stopped there, so nothing was requested for
# two nights.
SPLICE_B = "RO0005_20260928_005437_005455_color.mkv"
WITNESSES = [  # (RO0005 clip, time); the splice is the brightest
    (SPLICE_B, "2026-09-28T00:54:49"),
    ("RO0005_20260928_010100_color.mkv", "2026-09-28T01:01:10"),
    ("RO0005_20260928_010200_color.mkv", "2026-09-28T01:02:10"),
]


def _v_clip(t):
    return f"RO000V_20260928_{t[11:19].replace(':', '')}_color.mkv"


def _three_witnesses():
    rows = []
    for i, (name, t) in enumerate(WITNESSES):
        rows += [_row("RO000V", _v_clip(t), t), _row("RO0005", name, t, mag=-3.0 + i)]
    return rows


def _archive_with_v(tmp_path):
    """gmnro04 (Vâlcele) has all its clips, so only gmnro05 (Bistrița) is asked."""
    root = _archive(tmp_path, a_has_clip=False)
    for _name, t in WITNESSES:
        (root / "RO000V" / NIGHT / "meteors" / _v_clip(t)).write_bytes(b"x")
    return root


# Inside Bistrița's daylight window on 2026-09-28, with REFUSED_RETRY_S still
# before sunset minus the margin.
NOON = DAY.replace(hour=12, minute=5)


def _ok_unless_splice(url):
    if SPLICE_B in url:
        return 400
    return {"dawn_running": False, "idle_supported": True}


def test_a_probe_refusing_the_first_clip_does_not_stop_the_pass(tmp_path):
    s = _Session(200, probe=_ok_unless_splice)
    summary = _run(tmp_path, _three_witnesses(), s, root=_archive_with_v(tmp_path))
    posted = s.urls
    assert len(posted) == 2 and not any(SPLICE_B in u for u in posted)
    assert summary["gmnro05"]["refused"] == 1 and summary["gmnro05"]["error"] == 0
    entry = _state(tmp_path)[f"RO0005/{NIGHT}/{SPLICE_B}"]
    assert entry["status"] == REFUSED and entry["http"] == 400


def test_a_refused_post_does_not_stop_the_pass(tmp_path):
    s = _Session(400, 200, 200)
    _run(tmp_path, _three_witnesses(), s, root=_archive_with_v(tmp_path))
    assert len(s.urls) == 3
    state = _state(tmp_path)
    assert state[f"RO0005/{NIGHT}/{SPLICE_B}"]["status"] == REFUSED
    assert state[f"RO0005/{NIGHT}/{WITNESSES[1][0]}"]["status"] == REQUESTED


def test_every_due_clip_refused_means_no_post(tmp_path):
    s = _Session(200, probe=400)
    summary = _run(tmp_path, _three_witnesses(), s, root=_archive_with_v(tmp_path))
    assert s.urls == []
    assert summary["gmnro05"]["refused"] == 3


def test_a_refused_clip_is_asked_again_only_after_the_retry_wait(tmp_path):
    rows = [_row("RO000V", CHUNK_A), _row("RO0005", CHUNK_B)]
    key = f"RO0005/{NIGHT}/{CHUNK_B}"
    retry = timedelta(seconds=clip_requester.REFUSED_RETRY_S)
    _run(tmp_path, rows, _Session(400), now=NOON)
    soon = _Session(200)
    _run(tmp_path, rows, soon, now=NOON + retry - timedelta(minutes=10))
    assert soon.urls == []
    again = _Session(400)
    _run(tmp_path, rows, again, now=NOON + retry)
    assert len(again.urls) == 1
    assert _state(tmp_path)[key]["refusals"] == 2
    assert _state(tmp_path)[key]["status"] == REFUSED


def test_a_refused_clip_is_given_up_after_max_refusals(tmp_path):
    rows = [_row("RO000V", CHUNK_A), _row("RO0005", CHUNK_B)]
    key = f"RO0005/{NIGHT}/{CHUNK_B}"
    (tmp_path / "state.json").write_text(json.dumps({key: {
        "status": REFUSED, "last": 0, "attempts": 0, "http": 400,
        "refusals": clip_requester.MAX_REFUSALS - 1}}))
    _run(tmp_path, rows, _Session(400))
    assert _state(tmp_path)[key]["status"] == FAILED
    after = _Session(200)
    _run(tmp_path, rows, after,
         now=DAY + timedelta(seconds=clip_requester.REFUSED_RETRY_S, minutes=1))
    assert after.urls == [] and after.gets == []


def test_refusals_outlast_a_bundle_rollout():
    """A station refusing a name its next bundle accepts (stations update at
    14:00-16:45 UTC; passes run in daylight) must still be asked once it has
    the bundle: the retries span at least half a day."""
    span = clip_requester.REFUSED_RETRY_S * (clip_requester.MAX_REFUSALS - 1)
    assert span >= 12 * 3600


def test_a_refused_clip_is_delivered_once_the_station_takes_it(tmp_path):
    rows = [_row("RO000V", CHUNK_A), _row("RO0005", CHUNK_B)]
    _run(tmp_path, rows, _Session(400), now=NOON)
    later = _Session(200)
    _run(tmp_path, rows, later, now=NOON + timedelta(seconds=clip_requester.REFUSED_RETRY_S))
    assert len(later.urls) == 1
    assert _state(tmp_path)[f"RO0005/{NIGHT}/{CHUNK_B}"]["status"] == REQUESTED


# ── last night, before READY_HOUR_UTC ────────────────────────────────────────

MORNING = datetime(2026, 9, 28, 10, 30, tzinfo=timezone.utc)  # after Bistrița's dawn


def _on_demand_probe(at_dawn):
    body = {"dawn_running": False, "idle_supported": True}
    if at_dawn is not None:
        body["clips_at_dawn"] = at_dawn
    return body


def test_last_night_is_early_only_before_noon():
    assert clip_requester.early_night(MORNING) == NIGHT
    assert clip_requester.early_night(DAY) is None


def test_on_demand_station_gets_last_night_before_noon(tmp_path):
    s = _Session(200, probe=_on_demand_probe(False))
    _run(tmp_path, [_row("RO0005", CHUNK_B)], s, now=MORNING)
    assert s.urls == [
        f"http://100.1.1.2:7779/api/process_clip/RO0005/{NIGHT}/{CHUNK_B}?idle=1"]


@pytest.mark.parametrize("at_dawn", [True, None])
def test_station_that_encodes_at_dawn_waits_for_noon(tmp_path, at_dawn):
    s = _Session(200, probe=_on_demand_probe(at_dawn))
    summary = _run(tmp_path, [_row("RO0005", CHUNK_B)], s, now=MORNING)
    assert s.urls == []
    assert summary["gmnro05"]["requested"] == 0
    assert not (tmp_path / "state.json").exists() or _state(tmp_path) == {}


def test_due_nights_go_before_last_night(tmp_path):
    older = (datetime.strptime(NIGHT, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
    old_chunk = "RO0005_20260927_005437_color.mkv"
    root = _archive(tmp_path)
    (root / "RO0005" / older / "stacks").mkdir(parents=True)
    rows = [_row("RO0005", CHUNK_B, mag=-5.0),
            _row("RO0005", old_chunk, t="2026-09-27T00:54:49", mag=3.0, night=older)]
    s = _Session(200, probe=_on_demand_probe(False))
    clip_requester.run(_config(), MORNING, nights=1, max_per_station=1, archive_root=root,
                       state_path=tmp_path / "state.json", session=s, rows=rows)
    assert [u.split("/")[-1].split("?")[0] for u in s.urls] == [old_chunk]


def test_dawn_still_running_sends_nothing_early(tmp_path):
    s = _Session(200, probe={"dawn_running": True, "clips_at_dawn": False})
    _run(tmp_path, [_row("RO0005", CHUNK_B)], s, now=MORNING)
    assert s.urls == []
