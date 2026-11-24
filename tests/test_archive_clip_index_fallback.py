"""_archive_clip_path must trust the archive index when the mount stat fails.

Regression for the 2026-09-17 dawn window: the storage-box FUSE mount sheds
under the fleet's upload burst, direct stats read as "file absent", and the
events page 404'd shortclips whose bytes were on the box the whole time.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "dashboard"))

CAM = "RO0004"
NIGHT = "20260916"
CLIP = f"{CAM}_{NIGHT}_211614_color.mkv"


def _with_index(monkeypatch, tmp_path, meteor_files):
    import routes.archive as ra

    monkeypatch.setattr(ra, "ARCHIVE_PATH", tmp_path)

    class _Idx:
        def night_files(self, cam, date):
            assert (cam, date) == (CAM, NIGHT)
            return SimpleNamespace(meteor_files=tuple(meteor_files))

    monkeypatch.setattr(ra, "_DEPS", SimpleNamespace(archive_idx=_Idx()))
    return ra


def test_indexed_clip_survives_a_failed_stat(monkeypatch, tmp_path):
    """Nothing on disk (= the shed/timeout view), but the index lists the
    clip: the resolver must hand back the meteors/ path, not None."""
    ra = _with_index(monkeypatch, tmp_path, [CLIP])
    p = ra._archive_clip_path(CAM, NIGHT, CLIP)
    assert p == tmp_path / CAM / NIGHT / "meteors" / CLIP


def test_unindexed_clip_is_still_a_miss(monkeypatch, tmp_path):
    ra = _with_index(monkeypatch, tmp_path, ["something_else.mkv"])
    assert ra._archive_clip_path(CAM, NIGHT, CLIP) is None


def test_direct_stat_still_wins(monkeypatch, tmp_path):
    """A real on-disk file resolves without touching the index."""
    import routes.archive as ra

    monkeypatch.setattr(ra, "ARCHIVE_PATH", tmp_path)
    monkeypatch.setattr(ra, "_DEPS", None)
    target = tmp_path / CAM / NIGHT / "meteors" / CLIP
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x")
    assert ra._archive_clip_path(CAM, NIGHT, CLIP) == target


def test_no_index_registered_behaves_as_before(monkeypatch, tmp_path):
    import routes.archive as ra

    monkeypatch.setattr(ra, "ARCHIVE_PATH", tmp_path)
    monkeypatch.setattr(ra, "_DEPS", None)
    assert ra._archive_clip_path(CAM, NIGHT, CLIP) is None
