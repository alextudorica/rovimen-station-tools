"""Stations upload each meteor clip once, as .mkv (archive_upload.py).

The faststart .mp4 sibling used to be remuxed on the station and uploaded next
to every ``_color.mkv`` and ``.details.mkv``: the same H.264 bitstream twice
over the uplink (gmnro01 (Ghirdoveni), night 20261023: ~17.7 GB for the night,
half of it duplicate). The VPS now derives it at ingest
(tests/test_faststart_sibling.py), so no pass may make or send an .mp4 clip,
whatever ``publish_faststart_mp4`` says.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import archive_upload
from archive_upload import _Uploader

pytestmark = pytest.mark.usefixtures('ingest_ready')

CAM, NIGHT = "CAM", "20260101"


def _cfg(tmp_path: Path, publish: bool = True) -> dict:
    return {
        "archive": {"enabled": True, "publish_faststart_mp4": publish},
        "videocapture_path": str(tmp_path),
        "stations": {CAM: {}},
    }


def _posting(monkeypatch) -> list[str]:
    posted: list[str] = []
    monkeypatch.setattr(_Uploader, "_post_one",
                        lambda self, local, cam, date, subdir:
                        (posted.append(f"{subdir}/{Path(local).name}"), True)[1])
    monkeypatch.setattr(archive_upload, "_is_locked", lambda *a, **k: True)
    monkeypatch.setattr(_Uploader, "_is_reencoded", lambda self, *a, **k: True)
    monkeypatch.setattr(_Uploader, "_upload_rms_files", lambda *a, **k: 0)
    return posted


def _night(tmp_path: Path) -> Path:
    d = tmp_path / CAM / NIGHT
    d.mkdir(parents=True)
    (d / f"{CAM}_{NIGHT}_010101_color.mkv").write_bytes(b"x")
    return d


def test_station_has_no_remux_left():
    assert not hasattr(archive_upload, "remux_faststart_mp4")
    assert not hasattr(_Uploader, "_publish_mp4_sibling")


@pytest.mark.parametrize("publish", [True, False])
def test_cycle_night_sends_the_mkv_only(monkeypatch, tmp_path, publish):
    night = _night(tmp_path)
    posted = _posting(monkeypatch)
    _Uploader(_cfg(tmp_path, publish)).cycle_night(CAM, NIGHT)
    assert f"meteors/{CAM}_{NIGHT}_010101_color.mkv" in posted
    assert not [p for p in posted if p.endswith(".mp4")]
    assert not list(night.glob("*.mp4"))


def test_cycle_sends_the_mkv_only(monkeypatch, tmp_path):
    _night(tmp_path)
    posted = _posting(monkeypatch)
    _Uploader(_cfg(tmp_path)).cycle()
    assert f"meteors/{CAM}_{NIGHT}_010101_color.mkv" in posted
    assert not [p for p in posted if p.endswith(".mp4")]


def test_upload_chunk_sends_the_mkv_only(monkeypatch, tmp_path):
    night = _night(tmp_path)
    posted = _posting(monkeypatch)
    monkeypatch.setattr(archive_upload.encoder, "process_chunk", lambda *a, **k: None)
    mkv = night / f"{CAM}_{NIGHT}_010101_color.mkv"
    assert archive_upload.upload_chunk(CAM, NIGHT, mkv, _cfg(tmp_path)) is True
    assert not [p for p in posted if p.endswith(".mp4")]
