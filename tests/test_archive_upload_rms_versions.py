"""RMS files re-upload whenever their content changes (archive_upload.py).

A reprocess with a new platepar rewrites FTPdetectinfo, the calibration plots
and the radiant map. On 2026-10-21 gmnro22 (Ferigile) recalibrated a night and
the archive kept the old files: the upload record was the file's size alone,
and a recalibrated FTPdetectinfo keeps its size (fixed-width columns), while the
stale ArchivedFiles copies of the plots outranked the rewritten CapturedFiles
ones until RMS re-archived them.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from tests.test_archive_upload_characterization import (  # noqa: F401  (fixture)
    CAM, FTP, NIGHT, PLOT, RADIANTS, SESSION, _build, _uploader, recorded,
)


def _rms_session(tmp_path: Path, sub: str = 'ArchivedFiles') -> Path:
    return tmp_path / 'rms' / sub / SESSION


def _rms_posts(posts: list[str]) -> list[str]:
    return sorted(p for p in posts if p.startswith('rms/'))


def _marks(night: Path) -> dict:
    return json.loads((night / 'state.json').read_text())['rms_uploaded']


def _set_mtime(path: Path, age_s: float) -> None:
    t = time.time() - age_s
    os.utime(path, (t, t))


def test_same_size_rewrite_is_sent_again(tmp_path, recorded):
    cfg, night = _build(tmp_path)
    _uploader(cfg).cycle_night(CAM, NIGHT)
    recorded.clear()

    ftp = _rms_session(tmp_path) / FTP
    ftp.write_text('FTP')            # same 3 bytes, recalibrated content
    _set_mtime(ftp, 60)
    _uploader(cfg).cycle()

    assert _rms_posts(recorded) == [f'rms/{FTP}']
    assert _marks(night)[FTP][2] == hashlib.sha1(b'FTP').hexdigest()


def test_unchanged_files_are_not_sent_again(tmp_path, recorded):
    cfg, _night = _build(tmp_path)
    _uploader(cfg).cycle_night(CAM, NIGHT)
    recorded.clear()
    _uploader(cfg).cycle()
    assert _rms_posts(recorded) == []


def test_a_touched_but_identical_file_only_refreshes_its_record(tmp_path, recorded):
    """A copy into ArchivedFiles can carry a new mtime with the same bytes."""
    cfg, night = _build(tmp_path)
    _uploader(cfg).cycle_night(CAM, NIGHT)
    recorded.clear()

    plot = _rms_session(tmp_path) / PLOT
    _set_mtime(plot, 60)
    _uploader(cfg).cycle()

    assert _rms_posts(recorded) == []
    assert _marks(night)[PLOT][1] == plot.stat().st_mtime_ns


def test_a_newer_capturedfiles_copy_beats_a_stale_archived_one(tmp_path, recorded):
    """Reprocess rewrites CapturedFiles minutes before it re-archives."""
    cfg, night = _build(tmp_path)
    _uploader(cfg).cycle_night(CAM, NIGHT)
    recorded.clear()

    captured = _rms_session(tmp_path, 'CapturedFiles')
    captured.mkdir(parents=True)
    (captured / PLOT).write_bytes(b'recalibrated-plot')
    _set_mtime(captured / PLOT, 60)
    _uploader(cfg).cycle()

    assert _rms_posts(recorded) == [f'rms/{PLOT}']
    assert _marks(night)[PLOT][2] == hashlib.sha1(b'recalibrated-plot').hexdigest()


def test_archived_copy_wins_a_tie(tmp_path):
    cfg, _night = _build(tmp_path)
    archived = _rms_session(tmp_path) / FTP
    captured = _rms_session(tmp_path, 'CapturedFiles')
    captured.mkdir(parents=True)
    (captured / FTP).write_text('pre')
    st = archived.stat()
    os.utime(captured / FTP, ns=(st.st_atime_ns, st.st_mtime_ns))

    files = {f.name: f for f in _uploader(cfg)._find_rms_files(CAM, NIGHT)}
    assert files[FTP] == archived


def _legacy_marks(night: Path) -> None:
    state = json.loads((night / 'state.json').read_text())
    state['rms_uploaded'] = {FTP: len('ftp'), RADIANTS: len('radiants'), PLOT: len('plot')}
    (night / 'state.json').write_text(json.dumps(state))


def test_legacy_size_marks_resend_recent_text_once(tmp_path, recorded):
    """Equal-size text from the last week may be a recalibration: sent once.
    An equal-size plot is taken as archived (a rewrite changes its size)."""
    cfg, night = _build(tmp_path)
    _legacy_marks(night)

    _uploader(cfg).cycle()
    assert _rms_posts(recorded) == sorted([f'rms/{FTP}', f'rms/{RADIANTS}'])
    assert all(isinstance(v, list) and len(v) == 3 for v in _marks(night).values())

    recorded.clear()
    _uploader(cfg).cycle()
    assert _rms_posts(recorded) == []


def test_legacy_size_marks_leave_the_old_backlog_alone(tmp_path, recorded):
    cfg, night = _build(tmp_path)
    _legacy_marks(night)
    for name in (FTP, RADIANTS, PLOT):
        _set_mtime(_rms_session(tmp_path) / name, 30 * 86400)

    _uploader(cfg).cycle()
    assert _rms_posts(recorded) == []
    assert all(isinstance(v, list) and len(v) == 3 for v in _marks(night).values())


def test_legacy_size_mismatch_is_sent(tmp_path, recorded):
    cfg, night = _build(tmp_path)
    _legacy_marks(night)
    plot = _rms_session(tmp_path) / PLOT
    plot.write_bytes(b'a bigger recalibrated plot')
    _set_mtime(plot, 30 * 86400)
    for name in (FTP, RADIANTS):
        _set_mtime(_rms_session(tmp_path) / name, 30 * 86400)

    _uploader(cfg).cycle()
    assert _rms_posts(recorded) == [f'rms/{PLOT}']
