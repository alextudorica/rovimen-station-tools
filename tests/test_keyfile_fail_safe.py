"""The key stores never trade an unreadable file for an empty key set.

Regression for 2026-11-20 21:29-21:38 UTC: ``mint_station_key.py`` run as root
left ``station_keys.yaml`` root-owned 0600, the dashboard user could not read
it, the loader returned no keys, and every ingest POST got 401.
"""

from __future__ import annotations

import logging
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import api_keys
import keyfile_io
import station_keys

REPO = Path(__file__).resolve().parent.parent
IS_ROOT = os.geteuid() == 0


@pytest.fixture(autouse=True)
def _reset_caches():
    for mod in (station_keys, api_keys):
        mod._invalidate_cache()
        mod._last_good.clear()
    keyfile_io._last_logged.clear()
    yield
    keyfile_io._last_logged.clear()
    for mod in (station_keys, api_keys):
        mod._invalidate_cache()
        mod._last_good.clear()


def _bump_mtime(path: Path) -> None:
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))


class FakeClock:
    def __init__(self, t: float = 1_795_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    c = FakeClock()
    monkeypatch.setattr(station_keys._last_good, "clock", c)
    monkeypatch.setattr(api_keys._last_good, "clock", c)
    return c


def _make_unreadable(path: Path, monkeypatch) -> None:
    """Simulate the 2026-11-20 state (file intact, read denied) as any user,
    root included, and bump the mtime so the cache reloads."""
    real = Path.read_text

    def deny(self: Path, *a, **kw):
        if self == path:
            raise PermissionError(13, "Permission denied", str(path))
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", deny)
    _bump_mtime(path)


def test_unreadable_file_serves_last_good_keys_within_window(tmp_path, monkeypatch, clock, caplog):
    path = tmp_path / "station_keys.yaml"
    _, secret = station_keys.add_key("gmnro14", path=path)
    assert station_keys.authorizes(secret, "gmnro14", path=path)

    _make_unreadable(path, monkeypatch)
    station_keys._invalidate_cache()  # also what a write in another worker looks like
    with caplog.at_level(logging.ERROR):
        assert station_keys.authorizes(secret, "gmnro14", path=path)
        clock.t += keyfile_io.STALE_KEYS_WINDOW_S - 1
        assert station_keys.authorizes(secret, "gmnro14", path=path)
    errors = [r.getMessage() for r in caplog.records if "cannot read" in r.getMessage()]
    assert len(errors) == 1  # rate-limited to once a minute
    assert "PermissionError" in errors[0] and str(path) in errors[0]
    assert "serving the 1 last good keys until" in errors[0]
    [state] = station_keys._last_good.status()
    assert state["serving_stale_keys"] is True
    assert state["error"] == "PermissionError"
    assert state["stale_until_utc"]


def test_unreadable_file_fails_closed_after_window(tmp_path, monkeypatch, clock, caplog):
    path = tmp_path / "station_keys.yaml"
    _, secret = station_keys.add_key("gmnro14", path=path)
    assert station_keys.authorizes(secret, "gmnro14", path=path)
    _make_unreadable(path, monkeypatch)
    assert station_keys.authorizes(secret, "gmnro14", path=path)  # opens the window
    clock.t += keyfile_io.STALE_KEYS_WINDOW_S
    with caplog.at_level(logging.ERROR):
        assert not station_keys.authorizes(secret, "gmnro14", path=path)
    assert any("failing closed" in r.getMessage() and "stale window ended" in r.getMessage()
               for r in caplog.records)
    [state] = station_keys._last_good.status()
    assert state["serving_stale_keys"] is False


def test_parse_error_fails_closed_at_once_despite_last_good(tmp_path, monkeypatch, clock, caplog):
    path = tmp_path / "station_keys.yaml"
    _, secret = station_keys.add_key("gmnro14", path=path)
    assert station_keys.authorizes(secret, "gmnro14", path=path)
    path.write_text("keys: [unclosed\n")  # a botched hand edit, e.g. a revocation
    _bump_mtime(path)
    with caplog.at_level(logging.ERROR):
        assert not station_keys.authorizes(secret, "gmnro14", path=path)
    assert any("file is invalid" in r.getMessage() for r in caplog.records)
    # A later read error must not resurrect the keys from before the edit.
    _make_unreadable(path, monkeypatch)
    assert not station_keys.authorizes(secret, "gmnro14", path=path)


def test_validation_error_fails_closed_at_once(tmp_path, clock):
    path = tmp_path / "api_keys.yaml"
    _, secret = api_keys.add_key("astromania", path=path)
    assert api_keys.validate(secret, path=path) is not None
    path.write_text("keys:\n  - label: no id\n")
    _bump_mtime(path)
    assert api_keys.validate(secret, path=path) is None


def test_recovery_clears_the_window(tmp_path, monkeypatch, clock):
    path = tmp_path / "station_keys.yaml"
    _, secret = station_keys.add_key("gmnro14", path=path)
    assert station_keys.authorizes(secret, "gmnro14", path=path)
    with monkeypatch.context() as m:
        _make_unreadable(path, m)
        assert station_keys.authorizes(secret, "gmnro14", path=path)
    clock.t += keyfile_io.STALE_KEYS_WINDOW_S - 60
    _bump_mtime(path)
    assert station_keys.authorizes(secret, "gmnro14", path=path)  # good reload
    assert station_keys._last_good.status() == []
    # A new outage gets a full window of its own.
    _make_unreadable(path, monkeypatch)
    assert station_keys.authorizes(secret, "gmnro14", path=path)
    clock.t += keyfile_io.STALE_KEYS_WINDOW_S - 1
    assert station_keys.authorizes(secret, "gmnro14", path=path)


@pytest.mark.skipif(IS_ROOT, reason="root reads a 000 file")
def test_real_chmod_000_is_treated_as_unreadable(tmp_path, clock):
    path = tmp_path / "station_keys.yaml"
    _, secret = station_keys.add_key("gmnro14", path=path)
    assert station_keys.authorizes(secret, "gmnro14", path=path)
    path.chmod(0)
    _bump_mtime(path)
    try:
        assert station_keys.authorizes(secret, "gmnro14", path=path)
    finally:
        path.chmod(0o600)


def test_unreadable_file_with_no_earlier_keys_fails_closed(tmp_path, monkeypatch, clock):
    path = tmp_path / "station_keys.yaml"
    _, secret = station_keys.add_key("gmnro14", path=path)
    station_keys._last_good.clear()
    station_keys._invalidate_cache()
    _make_unreadable(path, monkeypatch)
    assert not station_keys.authorizes(secret, "gmnro14", path=path)


@pytest.mark.parametrize("bad", ["keys: [unclosed\n", "- just\n- a list\n", "keys:\n  - id: x\n"])
def test_mint_refuses_on_unparseable_file_and_leaves_it_untouched(tmp_path, bad):
    path = tmp_path / "station_keys.yaml"
    path.write_text(bad)
    before = path.read_bytes()
    with pytest.raises(keyfile_io.KeyFileError):
        station_keys.add_key("gmnro14", path=path)
    with pytest.raises(keyfile_io.KeyFileError):
        station_keys.disable_key("gmnro14", path=path)
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["station_keys.yaml", "station_keys.yaml.lock"]


def test_mint_tool_refuses_with_message_and_exit_2(tmp_path):
    path = tmp_path / "station_keys.yaml"
    path.write_text("keys: [unclosed\n")
    before = path.read_bytes()
    proc = subprocess.run(
        [sys.executable, str(REPO / "tools" / "mint_station_key.py"), "--station", "gmnro14", "--path", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 2
    assert "cannot parse" in proc.stderr
    assert "Nothing was written" in proc.stderr
    assert "secret:" not in proc.stdout
    assert path.read_bytes() == before


def test_api_key_mint_refuses_on_unparseable_file(tmp_path):
    path = tmp_path / "api_keys.yaml"
    path.write_text("keys: [unclosed\n")
    before = path.read_bytes()
    with pytest.raises(keyfile_io.KeyFileError):
        api_keys.add_key("astromania", path=path)
    assert path.read_bytes() == before


def test_api_key_still_validates_while_file_unreadable(tmp_path, monkeypatch, clock):
    path = tmp_path / "api_keys.yaml"
    _, secret = api_keys.add_key("astromania", path=path)
    assert api_keys.validate(secret, path=path) is not None
    _make_unreadable(path, monkeypatch)
    api_keys._invalidate_cache()
    assert api_keys.validate(secret, path=path) is not None
    clock.t += keyfile_io.STALE_KEYS_WINDOW_S
    assert api_keys.validate(secret, path=path) is None


@pytest.mark.parametrize("start, expected", [(0o600, 0o600), (0o640, 0o640), (0o644, 0o640), (0o666, 0o640)])
def test_write_caps_mode(tmp_path, caplog, start, expected):
    path = tmp_path / "station_keys.yaml"
    station_keys.add_key("gmnro14", path=path)
    path.chmod(start)
    with caplog.at_level(logging.WARNING):
        station_keys.add_key("gmnro22", path=path)
    assert stat.S_IMODE(path.stat().st_mode) == expected
    world_readable_warned = any("world-readable" in r.getMessage() for r in caplog.records)
    assert world_readable_warned == bool(start & 0o004)
    station_keys.disable_key("gmnro22", path=path)
    assert stat.S_IMODE(path.stat().st_mode) == expected
    assert not list(tmp_path.glob("*.tmp"))


def test_new_file_gets_0600_and_directory_owner(tmp_path):
    path = tmp_path / "station_keys.yaml"
    station_keys.add_key("gmnro14", path=path)
    st = path.stat()
    assert stat.S_IMODE(st.st_mode) == 0o600
    assert (st.st_uid, st.st_gid) == (tmp_path.stat().st_uid, tmp_path.stat().st_gid)


@pytest.mark.skipif(not IS_ROOT, reason="chown to another user needs root")
def test_root_write_keeps_original_owner(tmp_path):
    path = tmp_path / "station_keys.yaml"
    station_keys.add_key("gmnro14", path=path)
    os.chown(path, 65534, 65534)
    station_keys.add_key("gmnro22", path=path)
    st = path.stat()
    assert (st.st_uid, st.st_gid) == (65534, 65534)


@pytest.mark.skipif(IS_ROOT, reason="root can chown")
def test_write_refuses_when_owner_cannot_be_kept(tmp_path, monkeypatch):
    path = tmp_path / "station_keys.yaml"
    station_keys.add_key("gmnro14", path=path)
    before = path.read_bytes()
    real = keyfile_io._target_attrs

    def foreign_owner(p: Path) -> tuple[int, int, int]:
        uid, gid, mode = real(p)
        return uid + 1, gid, mode

    monkeypatch.setattr(keyfile_io, "_target_attrs", foreign_owner)
    with pytest.raises(keyfile_io.KeyFileError, match="cannot give the new file owner"):
        station_keys.add_key("gmnro22", path=path)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))


def test_write_refuses_when_current_file_unreadable(tmp_path, monkeypatch):
    path = tmp_path / "station_keys.yaml"
    station_keys.add_key("gmnro14", path=path)
    before = path.read_bytes()
    _make_unreadable(path, monkeypatch)
    with pytest.raises(keyfile_io.KeyFileUnreadable):
        station_keys.add_key("gmnro22", path=path)
    monkeypatch.undo()
    assert path.read_bytes() == before


def test_admin_health_route_reports_stale_window(tmp_path, monkeypatch, clock):
    import rovimen_dashboard as rd
    from flask import Flask

    from admin_config import register_admin_user_routes

    path = tmp_path / "station_keys.yaml"
    _, secret = station_keys.add_key("gmnro14", path=path)
    monkeypatch.setattr(station_keys, "DEFAULT_KEYS_PATH", path)
    monkeypatch.setattr(api_keys, "DEFAULT_KEYS_PATH", tmp_path / "api_keys.yaml")
    monkeypatch.setattr(rd, "require_admin", lambda f: f)
    app = Flask(__name__)
    register_admin_user_routes(app)
    client = app.test_client()

    assert station_keys.authorizes(secret, "gmnro14", path=path)
    body = client.get("/api/admin/health/key_stores").get_json()
    assert body["station_keys"]["failures"] == []

    _make_unreadable(path, monkeypatch)
    body = client.get("/api/admin/health/key_stores").get_json()
    [state] = body["station_keys"]["failures"]
    assert state["serving_stale_keys"] is True
    assert state["error"] == "PermissionError"
    assert body["api_keys"]["failures"] == []
