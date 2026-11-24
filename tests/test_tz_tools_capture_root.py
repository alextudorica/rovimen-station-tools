"""The July tz repair tools resolve the colour-capture root through rovimen_config.

`tools/tz_fix_chunk_names.py` and `tools/diag_missing_chunk.py` run on a
station against the same capture tree every station script uses. They used to
spell the config-key chain out by hand, so a key added to
`rovimen_config.CAPTURE_ROOT_KEYS` would not have reached them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import diag_missing_chunk
import rovimen_config
import tz_fix_chunk_names

RESOLVERS = [tz_fix_chunk_names._capture_root, diag_missing_chunk.capture_base]


@pytest.mark.parametrize("resolve", RESOLVERS, ids=lambda f: f.__module__)
def test_follows_the_shared_key_chain(resolve, monkeypatch):
    monkeypatch.setattr(rovimen_config, "CAPTURE_ROOT_KEYS",
                        ("new_root_key",) + rovimen_config.CAPTURE_ROOT_KEYS)
    cfg = {"new_root_key": "/mnt/new", "videocapture_path": "/mnt/old"}
    assert resolve(cfg) == Path("/mnt/new")


@pytest.mark.parametrize("resolve", RESOLVERS, ids=lambda f: f.__module__)
def test_first_non_empty_key_wins_else_home(resolve):
    assert resolve({"videocapture_path": "", "color_capture_path": "/mnt/c",
                    "ssd_color_path": "/mnt/s"}) == Path("/mnt/c")
    assert resolve({}) == Path.home() / "color_capture"


def test_diag_prints_every_configured_root_key(capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(rovimen_config, "CAPTURE_ROOT_KEYS",
                        ("new_root_key",) + rovimen_config.CAPTURE_ROOT_KEYS)
    monkeypatch.setattr(diag_missing_chunk, "cfg_load",
                        lambda: {"new_root_key": str(tmp_path), "ssd_color_path": "/mnt/s"})
    monkeypatch.setattr(diag_missing_chunk, "DB", tmp_path / "absent.db")
    diag_missing_chunk.main()
    out = capsys.readouterr().out
    assert "cfg.new_root_key=%s" % tmp_path in out
    assert "cfg.ssd_color_path=/mnt/s" in out
