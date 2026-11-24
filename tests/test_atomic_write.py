"""``atomic_write.write_atomic``: the one temp-then-rename writer behind the
dashboard's stores, caches and seed files."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from atomic_write import write_atomic


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


@pytest.fixture
def umask_022():
    old = os.umask(0o022)
    yield
    os.umask(old)


def test_named_temp_keeps_the_umask_mode_of_write_text(tmp_path, umask_022):
    """A caller that passes its own temp name gets the file mode
    ``Path.write_text`` gave it (nginx reads some of these files)."""
    ref = tmp_path / "ref.json"
    ref.write_text("{}")
    out = tmp_path / "out.json"
    write_atomic(out, "{}", tmp=tmp_path / "out.json.tmp")
    assert out.read_text() == "{}"
    assert _mode(out) == _mode(ref) == 0o644
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out.json", "ref.json"]


def test_default_temp_is_private_like_mkstemp(tmp_path, umask_022):
    """No temp name: a private mkstemp sibling, so the file is 0600 (the
    pushed calibration store's behaviour)."""
    out = tmp_path / "calibration.json"
    write_atomic(out, b"{}")
    assert out.read_bytes() == b"{}"
    assert _mode(out) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["calibration.json"]


def test_replaces_an_existing_file(tmp_path):
    out = tmp_path / "x.bin"
    out.write_bytes(b"old")
    write_atomic(out, b"new", tmp=tmp_path / "x.bin.part")
    assert out.read_bytes() == b"new"


def test_text_encoding_and_chmod(tmp_path):
    out = tmp_path / "users.yaml"
    write_atomic(out, "label: București\n", tmp=tmp_path / "users.tmp",
                 encoding="utf-8", chmod=0o600)
    assert out.read_bytes() == "label: București\n".encode("utf-8")
    assert _mode(out) == 0o600


def test_mkdir_creates_the_parent(tmp_path):
    out = tmp_path / "a" / "b" / "c.json"
    write_atomic(out, "1", mkdir=True)
    assert out.read_text() == "1"


def test_missing_parent_without_mkdir_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        write_atomic(tmp_path / "nope" / "c.json", "1", tmp=tmp_path / "nope" / "c.tmp")


def test_failed_replace_removes_the_temp_and_keeps_the_old_file(tmp_path, monkeypatch):
    out = tmp_path / "x.json"
    out.write_text("old")

    def _fail(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", _fail)
    with pytest.raises(OSError, match="disk full"):
        write_atomic(out, "new", tmp=tmp_path / "x.json.tmp")
    with pytest.raises(OSError, match="disk full"):
        write_atomic(out, "new")
    assert out.read_text() == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["x.json"]
