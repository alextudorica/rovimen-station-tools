"""The MDC shower table is loaded from disk on a worker boot, not refetched.

At the 2026-09 recycle rate every worker boot pulled 1.5 MB from ta3.sk and
re-parsed it (~600 fetches a day) for data that changes once a year.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "dashboard"))


# ── MDC poller: disk cache ────────────────────────────────────────────────

def test_mdc_poller_boots_from_disk_and_persists_a_fetch(tmp_path, monkeypatch):
    import mdc_poller

    monkeypatch.setattr(mdc_poller, "_DISK_CACHE", tmp_path / "mdc_showers.json")
    fetches = []

    def fake_fetch():
        fetches.append(1)
        return "raw"

    monkeypatch.setattr(mdc_poller, "_fetch_raw", fake_fetch)
    monkeypatch.setattr(mdc_poller, "_parse", lambda raw: {"PER": {"name": "Perseids"}})
    with mdc_poller._cache_lock:
        mdc_poller._cache = {}

    def _one_iteration() -> threading.Event:
        """A stop event whose first wait() ends the loop: one pass, then exit."""
        ev = threading.Event()

        def _stop_now(*a, **kw):
            ev.set()
            return True

        ev.wait = _stop_now          # type: ignore[method-assign]
        return ev

    # Cold disk: one fetch, then persisted.
    mdc_poller._poll_loop(_one_iteration())
    assert fetches == [1]
    saved = json.loads((tmp_path / "mdc_showers.json").read_text())
    assert saved["showers"] == {"PER": {"name": "Perseids"}}

    # A recycled worker: served from disk, no fetch.
    with mdc_poller._cache_lock:
        mdc_poller._cache = {}
    mdc_poller._poll_loop(_one_iteration())
    assert fetches == [1]
    with mdc_poller._cache_lock:
        assert mdc_poller._cache == {"PER": {"name": "Perseids"}}

    # A stale disk copy is refreshed.
    saved["saved_at"] = time.time() - mdc_poller._POLL_INTERVAL_S - 1
    (tmp_path / "mdc_showers.json").write_text(json.dumps(saved))
    mdc_poller._poll_loop(_one_iteration())
    assert fetches == [1, 1]


def test_a_stale_disk_copy_is_served_while_the_refresh_fails(tmp_path, monkeypatch):
    import mdc_poller

    monkeypatch.setattr(mdc_poller, "_DISK_CACHE", tmp_path / "mdc_showers.json")
    (tmp_path / "mdc_showers.json").write_text(json.dumps({
        "saved_at": time.time() - 30 * 86400, "showers": {"GEM": {"name": "Geminids"}},
    }))
    monkeypatch.setattr(mdc_poller, "_fetch_raw", lambda: None)   # ta3.sk is down
    with mdc_poller._cache_lock:
        mdc_poller._cache = {}
    waits = []

    ev = threading.Event()

    def _record_wait(t=None):
        waits.append(t)
        ev.set()
        return True

    ev.wait = _record_wait          # type: ignore[method-assign]
    mdc_poller._poll_loop(ev)
    with mdc_poller._cache_lock:
        assert mdc_poller._cache == {"GEM": {"name": "Geminids"}}, "stale beats empty"
    assert waits == [mdc_poller._RETRY_AFTER_FAILURE_S], "a failed refresh is retried soon, not next week"


def test_a_disk_copy_in_an_older_format_is_served_but_refreshed_at_once(tmp_path, monkeypatch):
    """#1406 added fields to the parsed table; a week of the old cached copy
    would have meant no radiant (no Moon or viewing times) on the Showers page."""
    import mdc_poller

    path = tmp_path / "mdc_showers.json"
    monkeypatch.setattr(mdc_poller, "_DISK_CACHE", path)
    # written a minute ago, by a build without the schema field
    path.write_text(json.dumps({"saved_at": time.time() - 60, "showers": {"PER": {"name": "Perseids"}}}))
    showers, age = mdc_poller._load_disk_cache()
    assert showers == {"PER": {"name": "Perseids"}}
    assert age >= mdc_poller._POLL_INTERVAL_S          # the poller refreshes now

    mdc_poller._save_disk_cache({"PER": {"name": "Perseids", "sol_peak": 140.1}})
    assert json.loads(path.read_text())["schema"] == mdc_poller._CACHE_SCHEMA
    showers, age = mdc_poller._load_disk_cache()
    assert age < 60 and showers["PER"]["sol_peak"] == 140.1
