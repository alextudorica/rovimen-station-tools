"""Station side of the live fireball alert: rovimen-scripts/fireball_watch.py."""
from __future__ import annotations

import random
import struct
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "rovimen-scripts"))

import fireball_watch as fw  # noqa: E402

T0 = 1_760_000_000.0


def _blocks(series: list[float], n: int = 256) -> list[tuple[float, list[int]]]:
    out = []
    for b in range(0, len(series) - n + 1, n):
        out.append((T0 + b / 25.0, [int(v) for v in series[b:b + n]]))
    return out


def _sky(seconds: float, seed: int = 1) -> list[float]:
    rnd = random.Random(seed)
    return [1_000_000 * (1 + rnd.gauss(0, 0.002)) for _ in range(int(seconds * 25))]


def _bump(series: list[float], start: int, rise: int, fall: int, peak: float, step: float = 0.0) -> None:
    for k in range(rise + fall):
        f = k / rise if k < rise else max(0.0, 1 - (k - rise) / fall)
        series[start + k] *= 1 + peak * f
    for k in range(start + rise + fall, len(series)):
        series[k] *= 1 + step


def _run(series: list[float]) -> list[dict]:
    det = fw.FieldsumDetector("RO000A", 25.0)
    out = []
    for t0, vals in _blocks(series):
        out.extend(det.feed(t0, vals))
    return out


def test_fireball_like_brightening_is_sent_then_cleared_by_step_check() -> None:
    s = _sky(120)
    _bump(s, 60 * 25, rise=15, fall=25, peak=0.20)
    evs = _run(s)
    assert evs, "a 20 % brightening with a 15-frame rise must be sent"
    first, last = evs[0], evs[-1]
    assert first["qualifies"] and first["after"] is None
    assert abs(first["t_peak"] - (T0 + (60 * 25 + 15) / 25.0)) < 0.2
    assert last["after"] is not None and not last["step"]
    assert {e["id"] for e in evs} == {first["id"]}


def test_lightning_rise_is_not_sent() -> None:
    s = _sky(120)
    _bump(s, 60 * 25, rise=1, fall=4, peak=0.30)
    assert _run(s) == []


def test_step_is_flagged() -> None:
    s = _sky(120)
    _bump(s, 60 * 25, rise=12, fall=10, peak=0.15, step=0.12)
    evs = _run(s)
    assert evs and evs[-1]["step"] is True


def test_storm_camera_needs_slow_rise() -> None:
    s = _sky(300)
    for k in (40, 80, 120):                        # three lightning flashes
        _bump(s, k * 25, rise=1, fall=3, peak=0.30)
    _bump(s, 200 * 25, rise=10, fall=10, peak=0.20)  # rise 10 < 20: storm veto
    assert not [e for e in _run(s) if e["t_peak"] > T0 + 190]
    calm = _sky(300)
    _bump(calm, 200 * 25, rise=10, fall=10, peak=0.20)
    assert [e for e in _run(calm) if e["qualifies"]], "without the storm the same rise is sent"


def test_parse_fs_rejects_partial_file() -> None:
    vals = list(range(100, 612))
    data = struct.pack("<H", len(vals)) + struct.pack("<%dI" % len(vals), *vals)
    assert fw.parse_fs(data) == vals
    assert fw.parse_fs(data[:-4]) is None
    assert len(fw.fs_frames(vals)) == 256


def _fr_bytes(lines: list[list[tuple[int, int, int, int, bytes]]]) -> bytes:
    out = struct.pack("<I", len(lines))
    for frames in lines:
        out += struct.pack("<I", len(frames))
        for yc, xc, t, size, crop in frames:
            out += struct.pack("<4I", yc, xc, t, size) + crop
    return out


def test_parse_fr_head_centroid_and_saturation() -> None:
    size = 16
    frames = []
    for t in range(60):
        crop = bytearray([20] * size * size)
        crop[5 * size + 9] = 255           # head at x=9, y=5 in the crop
        frames.append((300, 400 + t, t, size, bytes(crop)))
    data = _fr_bytes([frames])
    lines = fw.parse_fr(data)
    assert lines is not None and len(lines) == 1
    assert lines[0]["n250"][0] == 1
    assert lines[0]["hx"][0] == 400 + 9 - 8 and lines[0]["hy"][0] == 300 + 5 - 8
    assert fw.parse_fr(data[:-3]) is None
    evs = fw.fr_events("RO000A", "FR_RO000A_20261015_225139_764_0583424.bin", lines)
    assert evs[0]["nfr"] == 60 and evs[0]["dur"] == 2.4 and len(evs[0]["t"]) <= fw.FR_TRACK_POINTS
    assert evs[0]["n250_sum"] == 60


def test_disabled_flag() -> None:
    assert fw.enabled({}) is True
    assert fw.enabled({"services": {"fireball_watch": {"enabled": False}}}) is False


class _Sink:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.sent = self.dropped = 0

    def put(self, evs: list[dict]) -> None:
        self.events.extend(evs)


def _fs_name(t0: float) -> str:
    from datetime import datetime, timezone
    d = datetime.fromtimestamp(t0, timezone.utc)
    return f"FS_RO000A_{d:%Y%m%d_%H%M%S}_{d.microsecond:06d}_fieldsum.bin"


def test_watcher_follows_session_and_sends(tmp_path, monkeypatch) -> None:
    cap = tmp_path / "CapturedFiles"
    sess = cap / "RO000A_20251009_080000_000000"
    sess.mkdir(parents=True)
    monkeypatch.setattr(fw, "discover", lambda: {"RO000A": (25.0, [cap])})
    monkeypatch.setattr(fw, "_load_config", lambda: {})
    s = _sky(120)
    _bump(s, 60 * 25, rise=15, fall=25, peak=0.20)
    blocks = _blocks(s)
    sink = _Sink()
    w = fw.Watcher(sink)  # type: ignore[arg-type]
    w._check_platepars = lambda: None  # type: ignore[method-assign]
    w.refresh()
    assert w.cams["RO000A"].session == sess
    for t0, vals in blocks:
        (sess / _fs_name(t0)).write_bytes(struct.pack("<H", len(vals)) + struct.pack("<%dI" % len(vals), *vals))
        if w.ino.ok:
            w.on_events(w.ino.read(0.5))
        w.rescan()
    fs = [e for e in sink.events if e["kind"] == "fs"]
    assert fs and fs[0]["qualifies"] and fs[-1]["after"] is not None
    st = w.status_event()
    assert st["cams"]["RO000A"]["session"] == sess.name
