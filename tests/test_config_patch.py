"""Tests for config_patch: the locked, re-reading config.json writer."""

from __future__ import annotations

import json
import os
import stat
import threading
from pathlib import Path

import pytest

import config_patch


@pytest.fixture
def cfg_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"a": {"b": 1}, "keep": True}))
    os.chmod(path, 0o640)
    return path


def test_patch_merges_into_the_file_on_disk(cfg_path):
    merged = config_patch.patch_config({"a": {"c": 2}}, cfg_path)
    assert merged == {"a": {"b": 1, "c": 2}, "keep": True}
    assert json.loads(cfg_path.read_text()) == merged


def test_patch_rereads_under_the_lock(cfg_path):
    # Another writer changed the file after we last looked: its key survives.
    cfg_path.write_text(json.dumps({"a": {"b": 1}, "keep": True, "other": 7}))
    config_patch.patch_config({"new": 1}, cfg_path)
    assert json.loads(cfg_path.read_text())["other"] == 7


def test_concurrent_patches_lose_nothing(cfg_path):
    def writer(prefix: str) -> None:
        for i in range(40):
            config_patch.patch_config({prefix: {str(i): i}}, cfg_path)

    threads = [threading.Thread(target=writer, args=(p,)) for p in ("x", "y", "z")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    final = json.loads(cfg_path.read_text())
    for prefix in ("x", "y", "z"):
        assert final[prefix] == {str(i): i for i in range(40)}


def test_file_mode_is_kept(cfg_path):
    config_patch.patch_config({"a": {"b": 2}}, cfg_path)
    assert stat.S_IMODE(cfg_path.stat().st_mode) == 0o640


def test_no_temp_file_is_left_behind(cfg_path):
    config_patch.patch_config({"a": {"b": 2}}, cfg_path)
    leftovers = [p.name for p in cfg_path.parent.iterdir()
                 if p.name not in ("config.json", config_patch.LOCK_NAME)]
    assert leftovers == []


def test_patch_does_not_alias_the_caller_payload(cfg_path):
    payload = {"a": {"list": [1]}}
    merged = config_patch.patch_config(payload, cfg_path)
    merged["a"]["list"].append(2)
    assert payload == {"a": {"list": [1]}}


def test_missing_config_is_not_created_from_the_patch(tmp_path):
    with pytest.raises(FileNotFoundError):
        config_patch.patch_config({"a": 1}, tmp_path / "config.json")
    assert not (tmp_path / "config.json").exists()


def test_non_object_patch_is_rejected(cfg_path):
    with pytest.raises(TypeError):
        config_patch.patch_config([1, 2], cfg_path)  # type: ignore[arg-type]


def test_cli_merges_an_argument_and_prints_key_paths(cfg_path, capsys):
    rc = config_patch.main(["-c", str(cfg_path),
                            '{"stations": {"RO000A": {"color_adaptive": {"sky_luma": 45}}}}'])
    assert rc == 0
    assert capsys.readouterr().out.split() == ["stations.RO000A.color_adaptive.sky_luma"]
    cfg = json.loads(cfg_path.read_text())
    assert cfg["stations"]["RO000A"]["color_adaptive"]["sky_luma"] == 45
    assert cfg["a"] == {"b": 1}


def test_cli_reads_stdin(cfg_path, monkeypatch, capsys):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO('{"stacker_max_concurrent": 2}'))
    assert config_patch.main(["-c", str(cfg_path), "-"]) == 0
    assert json.loads(cfg_path.read_text())["stacker_max_concurrent"] == 2


def test_cli_dry_run_writes_nothing(cfg_path, capsys):
    before = cfg_path.read_text()
    assert config_patch.main(["-c", str(cfg_path), "--dry-run", '{"x": {"y": 1}}']) == 0
    assert capsys.readouterr().out.split() == ["x.y"]
    assert cfg_path.read_text() == before


@pytest.mark.parametrize("arg,rc", [("not json", 2), ("[1, 2]", 2)])
def test_cli_rejects_bad_input(cfg_path, capsys, arg, rc):
    before = cfg_path.read_text()
    assert config_patch.main(["-c", str(cfg_path), arg]) == rc
    assert capsys.readouterr().err
    assert cfg_path.read_text() == before


def test_cli_missing_config_is_an_error(tmp_path, capsys):
    assert config_patch.main(["-c", str(tmp_path / "config.json"), '{"a": 1}']) == 1
    assert "config.json" in capsys.readouterr().err


def test_failed_write_leaves_the_old_file(cfg_path, monkeypatch):
    before = cfg_path.read_text()

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(config_patch.os, "replace", boom)
    with pytest.raises(OSError):
        config_patch.patch_config({"a": {"b": 9}}, cfg_path)
    assert cfg_path.read_text() == before
    assert sorted(p.name for p in cfg_path.parent.iterdir()) == sorted(
        ["config.json", config_patch.LOCK_NAME])


def test_interlocks_with_rovimen_config_update_config(cfg_path):
    """config_patch and rovimen_config.update_config (config_migrate,
    update_config_coords) share one lock, so mixed writers lose nothing."""
    import rovimen_config

    def patcher() -> None:
        for i in range(30):
            config_patch.patch_config({"p": {str(i): i}}, cfg_path)

    def migrator() -> None:
        for i in range(30):
            rovimen_config.update_config({"m": {str(i): i}}, cfg_path)

    threads = [threading.Thread(target=patcher), threading.Thread(target=migrator)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    final = json.loads(cfg_path.read_text())
    assert final["p"] == {str(i): i for i in range(30)}
    assert final["m"] == {str(i): i for i in range(30)}
    assert config_patch.LOCK_NAME == rovimen_config.CONFIG_LOCK_NAME


def test_missing_directory_is_not_created(tmp_path):
    target = tmp_path / "typo" / "config.json"
    with pytest.raises(FileNotFoundError):
        config_patch.patch_config({"a": 1}, target)
    assert not (tmp_path / "typo").exists()


def test_new_file_mode_is_0644(tmp_path):
    target = tmp_path / "config.json"
    config_patch.write_config_atomic({"a": 1}, target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert json.loads(target.read_text()) == {"a": 1}
