"""A storage-box scan that fails must not read as "this night is empty".

``_with_sshfs_timeout`` returns its ``default`` on three different conditions:
the 5 s (now 12 s) budget expiring, the call being shed because the SSHFS
concurrency limit is saturated, and the leaked-thread circuit breaker being
open. ``night_files`` used to pass the default ``None``, which is also what
``_scan_night`` returns for a night that genuinely holds no files.

The detections fast path reads an empty ``meteor_files`` as "no clip archived"
and emits a "clip pending" marker per detection, so while the fleet's dawn
uploads had the box busy, whole cameras flipped to walls of pending markers
with their clips sitting on the box the whole time. Observed live on
2026-09-15 05:35 UTC.
"""

from __future__ import annotations

import cache_store


def _index(monkeypatch, tmp_path):
    monkeypatch.setattr(cache_store, "ARCHIVE_PATH", tmp_path)
    # The index also saves old nights to CACHE_PATH/archive_nights. Another test
    # file that pointed CACHE_PATH at a writable temp dir would otherwise leave
    # this file's first listing on disk for the "nothing cached" test to find.
    monkeypatch.setattr(cache_store, "CACHE_PATH", tmp_path / "cache")
    return cache_store._ArchiveIndex()


def _night(tmp_path, cam: str, date: str, meteors: list[str]) -> None:
    d = tmp_path / cam / date / "meteors"
    d.mkdir(parents=True)
    for name in meteors:
        (d / name).write_bytes(b"")


def test_scan_failure_serves_the_last_known_listing(monkeypatch, tmp_path):
    idx = _index(monkeypatch, tmp_path)
    _night(tmp_path, "RO0003", "20260914", ["RO0003_20260914_190000_color.mkv"])

    first = idx.night_files("RO0003", "20260914")
    assert first is not None and len(first.meteor_files) == 1

    # Expire the on-demand entry, then make every storage-box call fail the
    # way a saturated/slow box does.
    idx._ondemand[("RO0003", "20260914")] = (0.0, first)
    import station_client
    monkeypatch.setattr(
        station_client, "_with_sshfs_timeout",
        lambda fn, timeout=5.0, default=None: default,
    )

    again = idx.night_files("RO0003", "20260914")
    assert again is not None, "a failed scan must not report an empty night"
    assert again.meteor_files == first.meteor_files


def test_scan_failure_with_nothing_cached_returns_none(monkeypatch, tmp_path):
    idx = _index(monkeypatch, tmp_path)
    import station_client
    monkeypatch.setattr(
        station_client, "_with_sshfs_timeout",
        lambda fn, timeout=5.0, default=None: default,
    )
    assert idx.night_files("RO0003", "20260914") is None


def test_empty_night_is_cached_so_the_box_is_not_rescanned(monkeypatch, tmp_path):
    """A genuinely empty night gets a short negative TTL.

    Without it every request for a cold camera-night rescans the box, and a
    fan-out over 50+ cameras turns one cold night into a scan storm — the
    feedback loop that saturated the SSHFS semaphore.
    """
    idx = _index(monkeypatch, tmp_path)
    calls: list[str] = []
    import station_client
    real = station_client._with_sshfs_timeout

    def _counting(fn, timeout=5.0, default=None):
        calls.append("scan")
        return real(fn, timeout=timeout, default=default)

    monkeypatch.setattr(station_client, "_with_sshfs_timeout", _counting)

    assert idx.night_files("RO0003", "20260914") is None
    assert idx.night_files("RO0003", "20260914") is None
    assert len(calls) == 1, f"empty night rescanned {len(calls)}x"
    assert idx._ondemand[("RO0003", "20260914")][1] is None


def test_scan_budget_is_larger_than_the_old_five_seconds():
    # A busy night is five subdirectory walks over SSHFS; 5 s was not enough
    # while the fleet was uploading, and overrunning it looked like an empty
    # night rather than a slow one.
    assert cache_store._ARCHIVE_SCAN_TIMEOUT_S > 5.0
    assert 0 < cache_store._ARCHIVE_NEGATIVE_TTL <= cache_store._ARCHIVE_ONDEMAND_TTL
