"""Fireball reports: every committed report is valid, and the loader's checks.

The committed reports under dashboard/fireball_reports_data/ are public
pages; a broken one would silently drop off the site (the loader logs and
skips it), so this file loads each one strictly.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

import fireball_reports as fr

REPO = Path(__file__).resolve().parent.parent
COMMITTED = sorted(fr.REPORTS_DIR.glob("*/report.json"))
FIGURE_MAX_BYTES = 600 * 1024
VIDEO_MAX_BYTES = 6 * 1024 * 1024


def _registry_cameras() -> set[str]:
    cfg = yaml.safe_load((REPO / "dashboard" / "dashboard_config.example.yaml").read_text())
    return {c["code"].upper() for st in cfg["stations"].values() for c in st.get("cameras", [])}


def test_reports_are_committed():
    if not COMMITTED:
        pytest.skip("published fireball reports are not part of the public repository")
    assert COMMITTED, "no fireball reports found"


@pytest.mark.parametrize("path", COMMITTED, ids=lambda p: p.parent.name)
def test_committed_report_is_valid(path: Path):
    report = fr.load_report(path)
    # Analysed on or after the event, and an update comes after the analysis.
    assert report.analysed >= report.event_date
    if report.updated:
        assert report.updated >= report.analysed
    # A revision note describes an update, so it comes with the update's date.
    if report.notes:
        assert report.updated
    # Every camera resolves to a station, so the page can name its place.
    cameras = _registry_cameras()
    assert set(report.cameras) <= cameras
    assert {c.camera for c in report.clips} <= cameras
    # A clip belongs to the event's night: its filename carries the camera code.
    for clip in report.clips:
        assert clip.filename.startswith(clip.camera + "_")
    for section in report.sections:
        for fig in section.figures:
            cap = VIDEO_MAX_BYTES if fig.is_video else FIGURE_MAX_BYTES
            assert (path.parent / fig.file).stat().st_size <= cap, fig.file


def test_committed_reports_do_not_share_clips():
    seen: dict[tuple[str, str], str] = {}
    for path in COMMITTED:
        report = fr.load_report(path)
        for clip in report.clips:
            key = (clip.camera, clip.filename)
            assert key not in seen, f"{key} in both {seen.get(key)} and {report.event_id}"
            seen[key] = report.event_id


# ── Loader checks on synthetic reports ─────────────────────────────────


def _minimal(event_id: str = "20260101_220000", **over) -> dict:
    base = {
        "event_id": event_id,
        "title": "Test fireball",
        "time_utc": "2026-01-01 22:00:00 UTC",
        "analysed": "2026-01-02",
        "status": "final",
        "authors": "Test",
        "summary": ["A test."],
        "cameras": ["PUB001"],
        "facts": [{"label": "Speed", "value": "20 km/s"}],
        "clips": [{"camera": "PUB001", "date": "2026-01-01",
                   "filename": "PUB001_20260101_215955_color.mkv"}],
    }
    base.update(over)
    return base


def _write(root: Path, data: dict, dirname: str | None = None) -> Path:
    d = root / (dirname or data["event_id"])
    d.mkdir(parents=True, exist_ok=True)
    p = d / "report.json"
    p.write_text(json.dumps(data))
    return p


def test_load_report_rejects_directory_mismatch(tmp_path):
    p = _write(tmp_path, _minimal(), dirname="20260101_220001")
    with pytest.raises(ValueError, match="does not match"):
        fr.load_report(p)


def test_load_report_rejects_missing_figure(tmp_path):
    data = _minimal(sections=[{"heading": "Map", "paragraphs": [],
                               "figures": [{"file": "map.webp", "caption": "x"}]}])
    with pytest.raises(ValueError, match="missing"):
        fr.load_report(_write(tmp_path, data))


def test_load_report_rejects_unknown_keys(tmp_path):
    with pytest.raises(ValueError):
        fr.load_report(_write(tmp_path, _minimal(methods="SkyFit2")))


def test_load_report_rejects_missing_poster(tmp_path):
    data = _minimal(sections=[{"heading": "Train", "paragraphs": [],
                               "figures": [{"file": "t.mp4", "poster": "t.webp", "caption": "x"}]}])
    p = _write(tmp_path, data)
    (p.parent / "t.mp4").write_bytes(b"x")
    with pytest.raises(ValueError, match="t.webp"):
        fr.load_report(p)


def test_load_report_accepts_vector_figures(tmp_path):
    data = _minimal(sections=[{"heading": "Spectrum", "paragraphs": [],
                               "figures": [{"file": "spectrum.svg", "caption": "x"}]}])
    p = _write(tmp_path, data)
    (p.parent / "spectrum.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    assert fr.load_report(p).sections[0].figures[0].file == "spectrum.svg"


def test_figure_names_cannot_leave_the_report_directory(tmp_path):
    data = _minimal(sections=[{"heading": "x", "paragraphs": [],
                               "figures": [{"file": "../secret.webp", "caption": "x"}]}])
    with pytest.raises(ValueError):
        fr.load_report(_write(tmp_path, data))


def test_all_reports_skips_invalid_and_reloads_on_change(tmp_path):
    _write(tmp_path, _minimal())
    _write(tmp_path, _minimal("20260102_220000", status="draft"))
    got = fr.all_reports(tmp_path)
    assert list(got) == ["20260101_220000"]

    _write(tmp_path, _minimal(title="Renamed"))
    p = tmp_path / "20260101_220000" / "report.json"
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert fr.all_reports(tmp_path)["20260101_220000"].title == "Renamed"


def test_report_for_clip_matches_camera_and_filename(tmp_path, monkeypatch):
    _write(tmp_path, _minimal())
    monkeypatch.setattr(fr, "REPORTS_DIR", tmp_path)
    assert fr.report_for_clip("pub001", "PUB001_20260101_215955_color.mkv").event_id == "20260101_220000"
    assert fr.report_for_clip("PUB001", "PUB001_20260101_230000_color.mkv") is None
