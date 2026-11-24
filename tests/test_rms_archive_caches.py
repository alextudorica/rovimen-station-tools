"""The RMS tab's archive lookups are cached, incremental and SSHFS-bounded.

Opening one camera's RMS tab used to stat every archived night of the camera
on the Storage Box and re-parse FTPdetectinfo for each of them, unguarded,
all at once — the single largest source of 504s on prod.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "dashboard"))


# ── archived RMS caches behind the RMS tab ───────────────────────────────

@pytest.fixture
def media(tmp_path, monkeypatch):
    import cache_store
    from routes import archive as archive_mod
    from routes import media as media_mod

    archive = tmp_path / "archive"
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(media_mod, "ARCHIVE_PATH", archive)
    monkeypatch.setattr(archive_mod, "ARCHIVE_PATH", archive)
    monkeypatch.setattr(cache_store, "CACHE_PATH", cache_dir)
    archive_mod._rms_archive_cache.clear()
    media_mod._rms_nights_cache.clear()
    return media_mod, archive


@pytest.fixture
def rows_mod(media):
    """routes.archive, home of the archived detection rows the RMS tab, the
    Detections tab and the clip player's detail panel share."""
    from routes import archive as archive_mod

    return archive_mod


class _Idx:
    def __init__(self, nights):
        self._nights = nights

    def nights(self, cam):
        return list(self._nights)


def test_rms_nights_probe_only_unknown_nights_and_persist(media, monkeypatch):
    media_mod, archive = media
    for d in ("20260901", "20260902", "20260903"):
        (archive / "RO000H" / d).mkdir(parents=True)
    (archive / "RO000H" / "20260901" / "rms").mkdir()
    (archive / "RO000H" / "20260902" / "rms").mkdir()
    idx = _Idx(["20260903", "20260902", "20260901"])

    probed = []
    real_is_dir = Path.is_dir

    def spy(self):
        if self.name == "rms":
            probed.append(self.parent.name)
        return real_is_dir(self)

    monkeypatch.setattr(Path, "is_dir", spy)
    assert media_mod._archive_rms_nights(idx, "RO000H") == {"20260901", "20260902"}
    # Probed four at a time, newest submitted first.
    assert sorted(probed) == ["20260901", "20260902", "20260903"]

    # Within the TTL: no probes at all.
    probed.clear()
    assert media_mod._archive_rms_nights(idx, "RO000H") == {"20260901", "20260902"}
    assert probed == []

    # TTL expired, a new night appeared: only nights not yet known to have
    # rms/ are stat'ed (the two known ones are immutable).
    st = media_mod._rms_nights_cache["RO000H"]
    st["checked"] = {d: ts - 10_000 for d, ts in st["checked"].items()}
    (archive / "RO000H" / "20260904").mkdir()
    (archive / "RO000H" / "20260904" / "rms").mkdir()
    idx._nights.insert(0, "20260904")
    assert media_mod._archive_rms_nights(idx, "RO000H") == {"20260901", "20260902", "20260904"}
    assert sorted(probed) == ["20260903", "20260904"]

    # A recycled worker starts from the persisted set AND the persisted
    # "checked recently, no rms" list, so it probes nothing at all.
    media_mod._rms_nights_cache.clear()
    probed.clear()
    assert media_mod._archive_rms_nights(idx, "RO000H") == {"20260901", "20260902", "20260904"}
    assert probed == []


def test_rms_nights_probe_is_bounded_and_keeps_partial_progress(media, monkeypatch):
    media_mod, archive = media
    nights = [f"202607{d:02d}" for d in range(1, 31)][::-1]      # 30 nights, newest first
    for d in nights:
        (archive / "RO000H" / d / "rms").mkdir(parents=True)
    idx = _Idx(nights)
    monkeypatch.setattr(media_mod, "_RMS_NIGHTS_PROBE_BUDGET_S", 0.05)
    real_is_dir = Path.is_dir
    probed = []

    def slow(self):
        if self.name == "rms":
            probed.append(self.parent.name)
            time.sleep(0.02)                 # a 20 ms Storage Box round-trip
        return real_is_dir(self)

    monkeypatch.setattr(Path, "is_dir", slow)
    first = media_mod._archive_rms_nights(idx, "RO000H")
    assert 0 < len(first) < 30, "one call must stop at its budget yet keep what it found"
    # Newest nights first (four probes run at once, so any of the first four).
    assert first == set(probed) and "20260730" in probed[:4]
    # The next call continues where the last one stopped.
    second = media_mod._archive_rms_nights(idx, "RO000H")
    assert first < second
    # Enough calls cover every night; then nothing is probed any more.
    for _ in range(20):
        result = media_mod._archive_rms_nights(idx, "RO000H")
    assert result == set(nights)
    n = len(probed)
    media_mod._archive_rms_nights(idx, "RO000H")
    assert len(probed) == n


def test_rms_nights_progress_is_shared_between_workers(media, monkeypatch):
    """A worker that loaded the state earlier picks up another worker's
    probes from disk instead of probing the box again."""
    media_mod, archive = media
    nights = ["20260903", "20260902", "20260901"]
    for d in nights:
        (archive / "RO000H" / d / "rms").mkdir(parents=True)
    idx = _Idx(nights)
    # This worker loaded an empty state before the other worker's probe.
    media_mod._rms_nights_cache["RO000H"] = {"with_rms": set(), "checked": {}, "disk_mtime": 0}
    other_worker = {"with_rms": sorted(nights), "checked": {d: time.time() for d in nights}}
    media_mod._disk_cache_write("rms_nights", "RO000H", other_worker)

    probed = []
    real_is_dir = Path.is_dir

    def spy(self):
        if self.name == "rms":
            probed.append(self.parent.name)
        return real_is_dir(self)

    monkeypatch.setattr(Path, "is_dir", spy)
    assert media_mod._archive_rms_nights(idx, "RO000H") == set(nights)
    assert probed == []


def test_rms_nights_write_keeps_another_workers_progress(media, monkeypatch):
    media_mod, archive = media
    for d in ("20260902", "20260901"):
        (archive / "RO000H" / d / "rms").mkdir(parents=True)
    # Worker A knows 20260901; worker B, meanwhile, saved 20260902 only.
    media_mod._rms_nights_cache["RO000H"] = {
        "with_rms": {"20260901"}, "checked": {"20260901": time.time()}, "disk_mtime": 0}
    media_mod._disk_cache_write("rms_nights", "RO000H", {"with_rms": ["20260902"], "checked": {}})
    # The idx lists a third night A must still probe, which triggers a write.
    (archive / "RO000H" / "20260903").mkdir(parents=True)
    idx = _Idx(["20260903", "20260902", "20260901"])
    assert media_mod._archive_rms_nights(idx, "RO000H") == {"20260901", "20260902"}
    saved = media_mod._disk_cache_read("rms_nights", "RO000H", 3600)
    assert set(saved["with_rms"]) == {"20260901", "20260902"}


def test_rms_rows_are_parsed_once_and_bounded(rows_mod, monkeypatch):
    parses = []

    def fake_parse(cam, date):
        parses.append((cam, date))
        return [{"time_utc": "2026-09-01T22:00:00", "mag_apparent": -1.0}]

    monkeypatch.setattr(rows_mod, "archive_rms_detections", fake_parse)
    rows = rows_mod._archive_rms_rows("RO000H", "20260901")
    assert rows and len(parses) == 1
    assert rows_mod._archive_rms_rows("RO000H", "20260901") == rows
    assert len(parses) == 1

    # Another worker (empty memory) reads the persisted rows.
    rows_mod._rms_archive_cache.clear()
    assert rows_mod._archive_rms_rows("RO000H", "20260901") == rows
    assert len(parses) == 1

    # A slow Storage Box yields the previous rows without caching the miss.
    rows_mod._rms_archive_cache.clear()
    monkeypatch.setattr(rows_mod, "_disk_cache_read", lambda *a, **k: None)
    monkeypatch.setattr(rows_mod, "_with_sshfs_timeout", lambda fn, timeout, default: default)
    assert rows_mod._archive_rms_rows("RO000H", "20260902") == []
    assert ("RO000H", "20260902") not in rows_mod._rms_archive_cache


def test_rms_rows_reparse_after_the_night_is_reuploaded(rows_mod, monkeypatch):
    """A reprocess with a new platepar re-uploads FTPdetectinfo under the same
    name; every worker must drop its parsed rows, not wait out the 6 h TTL."""
    import cache_store
    version = {"ra": 10.0}

    def fake_parse(cam, date):
        return [{"time_utc": "2026-09-01T22:00:00", "ra": version["ra"]}]

    monkeypatch.setattr(rows_mod, "archive_rms_detections", fake_parse)
    assert rows_mod._archive_rms_rows("RO000H", "20260901")[0]["ra"] == 10.0

    version["ra"] = 20.0
    cache_store.invalidate_rms_night("RO000H", "20260901", "FTPdetectinfo_x.txt")
    assert rows_mod._archive_rms_rows("RO000H", "20260901")[0]["ra"] == 20.0
