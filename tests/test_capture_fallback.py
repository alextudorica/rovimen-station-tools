"""Colour capture falls back to the station's own disk when its disk is gone.

gmnro01 (Ghirdoveni)'s USB colour disk vanished on 2026-10-19; /mnt/data
reverted to a root-owned stub and colour capture crash-looped on Permission
denied for two nights. rovimen_config.capture_root now diverts to a fallback
root, and the janitor applies tighter limits there because that disk also
holds RMS.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import janitor_storage_watchdog as jw
import rovimen_config


@pytest.fixture
def roots(tmp_path):
    stub = tmp_path / "mnt_data"          # the missing disk's mountpoint
    stub.mkdir()
    fallback = tmp_path / "home_color_capture"
    cfg = {
        "videocapture_path": str(stub / "color_capture"),
        "capture_fallback": {"path": str(fallback)},
    }
    yield cfg, stub, fallback
    stub.chmod(0o755)


def test_a_writable_configured_root_is_used(roots):
    cfg, stub, _fallback = roots
    assert rovimen_config.capture_root(cfg) == stub / "color_capture"
    assert not rovimen_config.capture_on_fallback(cfg)


def test_an_unwritable_root_diverts_to_the_fallback(roots):
    cfg, stub, fallback = roots
    stub.chmod(0o555)                      # root-owned stub: not writable
    assert rovimen_config.capture_root(cfg) == fallback
    assert rovimen_config.capture_on_fallback(cfg)


def test_the_disk_coming_back_ends_the_fallback(roots):
    cfg, stub, fallback = roots
    stub.chmod(0o555)
    assert rovimen_config.capture_root(cfg) == fallback
    stub.chmod(0o755)
    assert rovimen_config.capture_root(cfg) == stub / "color_capture"


def _failing_creates(monkeypatch, under: Path, action) -> None:
    """Make file creation under *under* run *action*; elsewhere it works."""
    real = rovimen_config.tempfile.mkstemp

    def mkstemp(*args, **kwargs):
        if str(kwargs.get("dir", "")).startswith(str(under)):
            return action()
        return real(*args, **kwargs)

    monkeypatch.setattr(rovimen_config.tempfile, "mkstemp", mkstemp)


def test_a_root_that_answers_eio_diverts_to_the_fallback(roots, monkeypatch):
    """gmnro01 (Ghirdoveni), 2026-11-09: after its USB disk failed writes,
    the mount still read rw and os.access said writable, but every create
    returned EIO."""
    cfg, stub, fallback = roots

    def eio():
        raise OSError(5, "Input/output error")

    _failing_creates(monkeypatch, stub, eio)
    assert rovimen_config.capture_root(cfg) == fallback


def test_a_hung_root_diverts_without_piling_up_probes(roots, monkeypatch):
    import threading
    cfg, stub, fallback = roots
    release = threading.Event()
    calls = []

    def hang():
        calls.append(1)
        release.wait(5)
        raise OSError(5, "Input/output error")

    _failing_creates(monkeypatch, stub, hang)
    monkeypatch.setattr(rovimen_config, "WRITE_PROBE_TIMEOUT_S", 0.2)
    try:
        assert rovimen_config.capture_root(cfg) == fallback
        # The first probe is still stuck: report unwritable without a second one.
        assert rovimen_config.capture_root(cfg) == fallback
        assert len(calls) == 1
    finally:
        release.set()


def test_the_write_probe_leaves_nothing_behind(roots):
    cfg, stub, _fallback = roots
    assert rovimen_config.capture_root(cfg) == stub / "color_capture"
    assert list(stub.iterdir()) == []


def test_fallback_can_be_switched_off(roots):
    cfg, stub, _fallback = roots
    cfg["capture_fallback"]["enabled"] = False
    stub.chmod(0o555)
    assert rovimen_config.capture_root(cfg) == stub / "color_capture"


def test_default_fallback_is_home_color_capture(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert rovimen_config.capture_fallback_root({}) == tmp_path / "color_capture"
    # No configured root means ~/color_capture already: nothing to fall back from.
    assert not rovimen_config.capture_on_fallback({})


def _janitor(tmp_path, cfg) -> jw.Janitor:
    cfg = {**cfg, "log_path": str(tmp_path / "logs"),
           "retention": {"color_days": 10, "locked_days": 10, "uploaded_days": 10},
           "disk": {"warn_pct": 85, "protect_hours": 24},
           "stations": {"RO000A": {}}}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg))
    return jw.Janitor(str(path))


def test_janitor_keeps_its_limits_on_the_colour_disk(roots, tmp_path):
    cfg, _stub, _fallback = roots
    j = _janitor(tmp_path, cfg)
    assert not j.on_fallback
    assert (j.color_retention, j.protect_hours, j.warn_pct) == (10, 24, 85)


def test_janitor_tightens_its_limits_on_the_fallback(roots, tmp_path):
    cfg, stub, fallback = roots
    stub.chmod(0o555)
    j = _janitor(tmp_path, cfg)
    assert j.on_fallback and j.capture_path == fallback
    assert (j.color_retention, j.uploaded_retention, j.locked_retention) == (1, 1, 3)
    # the whole night is kept (protect_hours 16), with a 15 GB floor for RMS
    assert (j.warn_pct, j.protect_hours, j.min_free_gb) == (85, 16, 25)
    assert j.fallback_floor_gb == 15


def test_fallback_limits_can_be_overridden_per_station(roots, tmp_path):
    cfg, stub, _fallback = roots
    cfg["capture_fallback"]["disk"] = {"protect_hours": 4}
    stub.chmod(0o555)
    j = _janitor(tmp_path, cfg)
    assert j.protect_hours == 4 and j.warn_pct == 85


def test_color_capture_stops_when_its_root_changes(roots, monkeypatch):
    """A daemon that started on the fallback (the disk mounted a moment
    later) or on a disk that then vanished must restart onto the right root."""
    import color_capture
    cfg, stub, fallback = roots
    cc = color_capture.ColorCapture.__new__(color_capture.ColorCapture)
    cc.cfg, cc._running, cc.capture_path = cfg, True, fallback
    cc.log = color_capture.logging.getLogger("test")
    monkeypatch.setattr(color_capture.ColorCapture, "ROOT_CHECK_S", 0)
    cc._capture_root_watch()                       # the stub is writable again
    assert cc.root_changed is True and cc._running is False


def test_fallback_floor_gives_up_tonights_non_detections_for_rms(roots, tmp_path, monkeypatch):
    """Below the floor, non-detection colour of ANY age goes (tonight's too,
    which protect_hours otherwise keeps); detections stay."""
    from datetime import datetime, timedelta, timezone
    cfg, stub, fallback = roots
    stub.chmod(0o555)
    j = _janitor(tmp_path, cfg)
    now = datetime.now(timezone.utc)
    night = fallback / "RO000A" / now.strftime("%Y%m%d")
    night.mkdir(parents=True)
    plain = [night / f"RO000A_{now:%Y%m%d}_0{i}0000_color.mkv" for i in range(3)]
    for p in plain:
        p.write_bytes(b"x")
    detection = night / f"RO000A_{now:%Y%m%d}_050000_color.mkv"
    detection.write_bytes(b"x")
    # Two hours old: past the RMS floor's minimum age, inside protect_hours.
    started = now - timedelta(hours=2)
    chunks = [(started, False, "RO000A", night.name, p) for p in plain]
    chunks.append((started, True, "RO000A", night.name, detection))
    monkeypatch.setattr(j, "_collect_chunks", lambda: chunks)
    freed = {"n": 0}

    def fake_unlink(sid, f):
        f.unlink()
        freed["n"] += 1
        return True

    monkeypatch.setattr(j, "_unlink_chunk", fake_unlink)
    # 10 GB free, +3 GB per deleted chunk: two deletions reach the 15 GB floor.
    monkeypatch.setattr(jw, "_fs_stats", lambda path: (97, 10.0 + 3.0 * freed["n"]))
    assert j._evict_for_rms() == 2
    assert detection.exists()
    assert sum(p.exists() for p in plain) == 1


def test_no_floor_off_the_fallback_when_rms_records_elsewhere(roots, tmp_path, monkeypatch):
    """Off the fallback, the floor exists only for a disk RMS shares."""
    cfg, _stub, _fallback = roots
    j = _janitor(tmp_path, cfg)
    assert not j.rms_on_capture_fs
    monkeypatch.setattr(jw, "_fs_stats", lambda path: (99, 1.0))
    assert j._rms_floor_gb() is None
    assert j._evict_for_rms() == 0


class _Clock:
    """Stands in for rovimen_config's ``time`` module: a monotonic clock the
    test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def counted_probe(monkeypatch):
    """Count write probes, answering from ``state['ok']``, on a fake clock."""
    clock = _Clock()
    state = {"ok": True, "probes": 0}

    def probe(directory):
        state["probes"] += 1
        return state["ok"]

    monkeypatch.setattr(rovimen_config, "time", clock)
    monkeypatch.setattr(rovimen_config, "_write_probe", probe)
    monkeypatch.setattr(rovimen_config, "_probe_results", {})
    return clock, state


def test_the_write_probe_runs_at_most_once_per_window(roots, counted_probe):
    """flags_manager resolves capture_root() for every state lookup, so an
    upload scan used to create and delete thousands of probe files on the
    capture disk."""
    cfg, stub, _fallback = roots
    clock, state = counted_probe
    for _ in range(500):
        assert rovimen_config.capture_root(cfg) == stub / "color_capture"
    assert state["probes"] == 1
    clock.now += rovimen_config.WRITE_PROBE_CACHE_S - 1
    rovimen_config.capture_root(cfg)
    assert state["probes"] == 1
    clock.now += 2
    rovimen_config.capture_root(cfg)
    assert state["probes"] == 2


def test_a_disk_that_dies_is_noticed_within_a_minute(roots, counted_probe, monkeypatch):
    cfg, stub, fallback = roots
    clock, state = counted_probe
    primary_ok = {"ok": True}
    monkeypatch.setattr(rovimen_config, "_write_probe",
                        lambda d: primary_ok["ok"] if Path(d) == stub else True)
    assert rovimen_config.capture_root(cfg) == stub / "color_capture"
    primary_ok["ok"] = False                # every create on it now answers EIO
    clock.now += rovimen_config.WRITE_PROBE_CACHE_S + 0.1
    assert rovimen_config.WRITE_PROBE_CACHE_S <= 60
    assert rovimen_config.capture_root(cfg) == fallback


def test_a_disk_that_comes_back_is_noticed_within_the_window(roots, counted_probe, monkeypatch):
    cfg, stub, fallback = roots
    clock, state = counted_probe
    primary_ok = {"ok": False}

    def per_dir(directory):
        state["probes"] += 1
        return primary_ok["ok"] if Path(directory) == stub else True

    monkeypatch.setattr(rovimen_config, "_write_probe", per_dir)
    assert rovimen_config.capture_root(cfg) == fallback
    primary_ok["ok"] = True
    assert rovimen_config.capture_root(cfg) == fallback      # still inside the window
    clock.now += rovimen_config.WRITE_PROBE_CACHE_S + 0.1
    assert rovimen_config.capture_root(cfg) == stub / "color_capture"
