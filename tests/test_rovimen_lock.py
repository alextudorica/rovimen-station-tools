"""Tests for rovimen_lock.py — readers and cleanup for legacy .locked sidecars."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import rovimen_lock


# -- Fixtures ----------------------------------------------------------------

@pytest.fixture
def mkv(tmp_path) -> Path:
    """Create a dummy MKV file and return its path."""
    p = tmp_path / "RO000H_20260322_210000_color.mkv"
    p.write_bytes(b"\x1a\x45\xdf\xa3")  # minimal Matroska header bytes
    return p


def _write_sidecar(mkv: Path, lock_type: str = "detection",
                   detection_time: str | None = None,
                   meteor_time: str | None = None) -> Path:
    """A sidecar as the retired writer left it on older chunks."""
    data: dict = {"lock_type": lock_type, "detection_time": detection_time}
    if meteor_time is not None:
        data["meteor_time"] = meteor_time
    lp = mkv.with_suffix(".mkv.locked")
    lp.write_text(json.dumps(data))
    return lp


# -- _lock_path --------------------------------------------------------------

class TestLockPath:
    def test_appends_locked_suffix(self, mkv):
        lp = rovimen_lock._lock_path(mkv)
        assert lp == mkv.with_suffix(".mkv.locked")
        assert lp.name == "RO000H_20260322_210000_color.mkv.locked"


# -- unlock ------------------------------------------------------------------

class TestUnlock:
    def test_removes_sidecar(self, mkv):
        lp = _write_sidecar(mkv, detection_time="20260322_210500")
        rovimen_lock.unlock(mkv)
        assert not lp.exists()

    def test_safe_on_nonexistent(self, mkv):
        """Unlocking when no sidecar exists should not raise."""
        rovimen_lock.unlock(mkv)  # no error

    def test_safe_on_nonexistent_mkv(self, tmp_path):
        """Unlocking a path where neither MKV nor sidecar exist should not raise."""
        ghost = tmp_path / "ghost.mkv"
        rovimen_lock.unlock(ghost)


# -- is_locked ---------------------------------------------------------------

class TestIsLocked:
    def test_true_when_locked(self, mkv):
        _write_sidecar(mkv)
        assert rovimen_lock.is_locked(mkv) is True

    def test_false_when_not_locked(self, mkv):
        assert rovimen_lock.is_locked(mkv) is False

    def test_false_after_unlock(self, mkv):
        _write_sidecar(mkv)
        rovimen_lock.unlock(mkv)
        assert rovimen_lock.is_locked(mkv) is False


# -- chunk_is_locked ---------------------------------------------------------

class TestChunkIsLocked:
    def test_state_lock_counts(self, mkv):
        assert rovimen_lock.chunk_is_locked(mkv, {"lock": {"lock_type": "detection"}}) is True

    def test_sidecar_counts(self, mkv):
        _write_sidecar(mkv)
        assert rovimen_lock.chunk_is_locked(mkv, {}) is True

    def test_neither_is_unlocked(self, mkv):
        assert rovimen_lock.chunk_is_locked(mkv, {"lock": None}) is False


# -- get_lock_info -----------------------------------------------------------

class TestGetLockInfo:
    def test_returns_metadata(self, mkv):
        _write_sidecar(mkv, "detection", detection_time="20260322_210500",
                       meteor_time="2026-03-22T21:05:03.456000")
        info = rovimen_lock.get_lock_info(mkv)
        assert info is not None
        assert info["lock_type"] == "detection"
        assert info["detection_time"] == "20260322_210500"
        assert info["meteor_time"] == "2026-03-22T21:05:03.456000"

    def test_returns_none_when_not_locked(self, mkv):
        assert rovimen_lock.get_lock_info(mkv) is None

    def test_returns_none_on_corrupted_json(self, mkv):
        lp = mkv.with_suffix(".mkv.locked")
        lp.write_text("{{{bad json")
        assert rovimen_lock.get_lock_info(mkv) is None

    def test_returns_none_on_binary_garbage(self, mkv):
        lp = mkv.with_suffix(".mkv.locked")
        lp.write_bytes(b"\x00\xff\xfe")
        assert rovimen_lock.get_lock_info(mkv) is None

    def test_missing_fields_return_none_values(self, mkv):
        """A .locked file with only lock_type should still parse without error."""
        lp = mkv.with_suffix(".mkv.locked")
        lp.write_text(json.dumps({"lock_type": "manual"}))
        info = rovimen_lock.get_lock_info(mkv)
        assert info["lock_type"] == "manual"
        assert info["detection_time"] is None
        assert info["meteor_time"] is None


# -- get_lock_type -----------------------------------------------------------

class TestGetLockType:
    def test_returns_type_string(self, mkv):
        _write_sidecar(mkv, "fireball")
        assert rovimen_lock.get_lock_type(mkv) == "fireball"

    def test_returns_none_when_not_locked(self, mkv):
        assert rovimen_lock.get_lock_type(mkv) is None

    def test_returns_none_on_corrupted_sidecar(self, mkv):
        mkv.with_suffix(".mkv.locked").write_text("not json")
        assert rovimen_lock.get_lock_type(mkv) is None
