"""``json_file_store`` behind compilation_store, hall_of_fame_store and
basket_store: the files keep their exact on-disk format, and a corrupt or
wrong-shaped file reads as empty."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import basket_store
import compilation_store
import hall_of_fame_store


@pytest.fixture
def stores(tmp_path: Path, monkeypatch):
    for mod, name in ((compilation_store, "compilations"), (hall_of_fame_store, "hof"),
                      (basket_store, "basket")):
        monkeypatch.setattr(mod, "_PATH", tmp_path / f"{name}.json")
        monkeypatch.setattr(mod, "_LOCK_PATH", tmp_path / f"{name}.lock")
    return tmp_path


def _rec(i: int) -> dict:
    return {"id": f"id{i}", "camera": "RO000A", "date": "2026-11-01",
            "filename": f"RO000A_20261101_00000{i}_color.mkv", "title": "Brașov"}


def test_files_are_indent_2_json_of_the_whole_document(stores: Path):
    compilation_store.upsert(_rec(1))
    hall_of_fame_store.add(_rec(2))
    basket_store.add("alice", _rec(3), max_items=5)
    assert (stores / "compilations.json").read_text() == json.dumps([_rec(1)], indent=2)
    assert (stores / "hof.json").read_text() == json.dumps([_rec(2)], indent=2)
    assert (stores / "basket.json").read_text() == json.dumps({"alice": [_rec(3)]}, indent=2)
    assert sorted(p.name for p in stores.iterdir() if p.suffix == ".tmp") == []


def test_patch_get_delete_by_id(stores: Path):
    compilation_store.upsert(_rec(1))
    compilation_store.upsert(_rec(2))
    assert compilation_store.patch("id2", {"status": "done"})["status"] == "done"
    assert compilation_store.patch("nope", {"status": "done"}) is None
    assert compilation_store.get("id2")["status"] == "done"
    assert compilation_store.delete("id1") is True
    assert compilation_store.delete("id1") is False
    assert [m["id"] for m in compilation_store.load_all()] == ["id2"]


def test_corrupt_file_is_copied_aside_and_reads_empty(stores: Path):
    (stores / "hof.json").write_text("{not json")
    assert hall_of_fame_store.load_all() == []
    backups = list(stores.glob("hof.corrupt.*"))
    assert len(backups) == 1 and backups[0].read_text() == "{not json"


@pytest.mark.parametrize("mod,name,wrong", [
    (compilation_store, "compilations", {"a": 1}),
    (hall_of_fame_store, "hof", {"a": 1}),
    (basket_store, "basket", [1, 2]),
])
def test_wrong_shape_reads_empty(stores: Path, mod, name, wrong):
    (stores / f"{name}.json").write_text(json.dumps(wrong))
    if mod is basket_store:
        assert basket_store.list_for("alice") == []
    else:
        assert mod.load_all() == []


def test_basket_drops_non_list_users(stores: Path):
    (stores / "basket.json").write_text(json.dumps({"alice": [_rec(1)], "bob": "x"}))
    assert basket_store.list_for("alice") == [_rec(1)]
    assert basket_store.list_for("bob") == []
