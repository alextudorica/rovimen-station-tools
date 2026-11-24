"""list_nights reads only the nights its caller wants.

The pusher keeps only the newest 2-3 nights of each camera, but list_nights
listed every night directory's files before sorting; gmnde00 (Johannismühle)
keeps a year of nights.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import rovimen_pusher
import station_collect

CAM = "RO000H"


@pytest.fixture
def nights(tmp_path, monkeypatch):
    cam = tmp_path / CAM
    for night in ("20261118", "20261119", "20261120", "20261121"):
        (cam / night).mkdir(parents=True)
        (cam / night / f"{CAM}_{night}_200000_color.mkv").write_bytes(b"x")
    (cam / "20261122").mkdir()            # tonight, nothing captured yet
    (cam / "notanight").mkdir()
    monkeypatch.setattr(station_collect, "capture_base", lambda: tmp_path)
    return cam


@pytest.fixture
def listed(monkeypatch):
    """Night directories whose files list_nights read."""
    seen: list[str] = []
    real = Path.iterdir

    def iterdir(self):
        if len(self.name) == 8 and self.name.isdigit():
            seen.append(self.name)
        return real(self)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    return seen


def test_all_nights_newest_first(nights):
    assert station_collect.list_nights(CAM) == ["20261121", "20261120", "20261119", "20261118"]


def test_a_limit_stops_after_the_newest_nights(nights, listed):
    assert station_collect.list_nights(CAM, limit=2) == ["20261121", "20261120"]
    assert listed == ["20261122", "20261121", "20261120"]


def test_pusher_asks_for_only_the_nights_it_pushes(monkeypatch):
    calls: list[tuple] = []

    class _Collect:
        def list_nights(self, cam, limit=None):
            calls.append((cam, limit))
            return ["20261121", "20261120", "20261119"][:limit]

        def list_chunks(self, cam, date, locked_only=False):
            return {"chunks": []}

    monkeypatch.setattr(rovimen_pusher, "_collect", _Collect())
    monkeypatch.setattr(rovimen_pusher, "fetch_camera_codes", lambda: [CAM])
    rovimen_pusher.fetch_locked_clips()
    assert calls == [(CAM, rovimen_pusher.CLIP_WINDOW_NIGHTS)]
    assert rovimen_pusher.fetch_nights(CAM, limit=2) == ["20261121", "20261120"]


def test_pusher_http_fallback_is_cut_to_the_limit(monkeypatch):
    monkeypatch.setattr(rovimen_pusher, "_collect", None)
    monkeypatch.setattr(rovimen_pusher, "_get_json",
                        lambda url, timeout=None: ["20261121", "20261120", "20261119"])
    assert rovimen_pusher.fetch_nights(CAM, limit=2) == ["20261121", "20261120"]
