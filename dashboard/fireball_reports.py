"""Fireball reports: curated per-event write-ups linked from the Hall of Fame.

Each report is a directory under ``fireball_reports_data/<event_id>/``
holding ``report.json`` and its figures. ``routes/fireball_reports.py``
renders the page (``/fireball/<event_id>``) and serves the figures
(``/fireball/<event_id>/<file>``) behind the same visibility checks, which is
why the data lives outside ``static/``: the static route would serve it to
anyone, whatever the "overview" toggle or the stations' ``public`` flag.

``event_id`` is the event's UTC time, ``YYYYMMDD_HHMMSS``. A report lists
the archived clips that show the event, and that list is what links a Hall
of Fame entry to its report: an entry whose camera and filename appear in a
report's ``clips`` gets that report's URL.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import threading
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger(__name__)

REPORTS_DIR = Path(__file__).resolve().parent / "fireball_reports_data"
EVENT_ID_RE = re.compile(r"^\d{8}_\d{6}$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Fact(_Strict):
    label: str
    value: str


class Figure(_Strict):
    # An .mp4 figure is a short looping video (timelapse, animation), rendered
    # muted and inline; an .svg figure is a vector plot that stays sharp when
    # enlarged; "third" puts up to three figures side by side.
    file: str = Field(pattern=r"^[A-Za-z0-9_.-]+\.(webp|png|jpg|svg|mp4)$")
    caption: str
    size: Literal["full", "half", "third"] = "full"
    # Still image a video figure shows before it plays (without one the
    # browser shows a black frame).
    poster: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]+\.(webp|png|jpg)$")

    @property
    def is_video(self) -> bool:
        return self.file.endswith(".mp4")


class Section(_Strict):
    heading: str
    paragraphs: list[str]
    figures: list[Figure] = []


class Clip(_Strict):
    camera: str = Field(pattern=r"^[A-Z0-9]{6}$")
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    filename: str


class Report(_Strict):
    event_id: str = Field(pattern=EVENT_ID_RE.pattern)
    title: str
    time_utc: str
    local_time: str | None = None
    analysed: dt.date
    updated: dt.date | None = None
    status: Literal["preliminary", "final"]
    authors: str
    summary: list[str]
    cameras: list[str]
    facts: list[Fact]
    clips: list[Clip]
    sections: list[Section] = []
    # Revision notes: what changed in an update, shown above the credits.
    notes: list[str] = []
    credits: list[str] = []

    @property
    def event_date(self) -> dt.date:
        return dt.datetime.strptime(self.event_id[:8], "%Y%m%d").date()

    @property
    def url(self) -> str:
        return f"/fireball/{self.event_id}"

    def figure_url(self, filename: str) -> str:
        return f"/fireball/{self.event_id}/{filename}"

    @property
    def figure_files(self) -> set[str]:
        files = set()
        for section in self.sections:
            for fig in section.figures:
                files.add(fig.file)
                if fig.poster:
                    files.add(fig.poster)
        return files


def load_report(path: Path) -> Report:
    """Parse and validate one ``report.json``; raises on any problem."""
    report = Report.model_validate_json(path.read_text())
    if report.event_id != path.parent.name:
        raise ValueError(f"event_id {report.event_id} does not match directory {path.parent.name}")
    for section in report.sections:
        for fig in section.figures:
            for name in filter(None, (fig.file, fig.poster)):
                if not (path.parent / name).is_file():
                    raise ValueError(f"figure {name} missing in {path.parent}")
    return report


_cache_lock = threading.Lock()
_cache: tuple[tuple, dict[str, Report]] = ((), {})


def all_reports(reports_dir: Path | None = None) -> dict[str, Report]:
    """Every valid report keyed by event_id, re-read when a file changes.

    An invalid report is logged and skipped, so one bad file cannot take the
    page or the Hall of Fame down; ``tests/test_fireball_reports.py`` loads
    every committed report strictly.
    """
    global _cache
    root = reports_dir or REPORTS_DIR
    paths = sorted(root.glob("*/report.json"))
    key = tuple((str(p), p.stat().st_mtime_ns) for p in paths)
    with _cache_lock:
        if key == _cache[0]:
            return _cache[1]
        reports: dict[str, Report] = {}
        for p in paths:
            try:
                rep = load_report(p)
            except (OSError, ValueError, ValidationError) as exc:
                logger.error("Skipping fireball report %s: %s", p, exc)
                continue
            reports[rep.event_id] = rep
        _cache = (key, reports)
        return reports


def report_for_clip(camera: str, filename: str) -> Report | None:
    """The report that lists this archived clip, if any."""
    cam = camera.upper()
    for rep in all_reports().values():
        if any(c.camera == cam and c.filename == filename for c in rep.clips):
            return rep
    return None
