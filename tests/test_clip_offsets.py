"""Measured meteor offsets in colour clips, end to end.

The players used to seek to "meteor time minus clip-name time" with both
cut to whole seconds, which put the meteor 0.5-2.5 s after the marker and the
cameras of one event out of step. Now the station measures where the meteor
starts in its clip (clip_timing), keeps sub-second times, and the dashboard
prefers the measurement.
"""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timezone

import pytest

import detection_indexer as di
import rovimen_rms


# ── sub-second times and the first-frame pixel ───────────────────────────────

def test_detection_rows_keep_milliseconds_and_the_first_pixel():
    ftp = {'_begin_dt': datetime(2026, 10, 21, 3, 40, 12, 680000), 'ff_file': 'FF_x.fits',
           'meteor_no': 1, 'x_beg': 1011.32, 'y_beg': 548.25}
    row = rovimen_rms.merge_row(ftp, {'time_utc': '2026-10-21T03:40:12', 'jd': 1.0})
    assert row['time_utc'] == '2026-10-21T03:40:12.680'
    assert (row['x_beg'], row['y_beg']) == (1011.32, 548.25)
    assert rovimen_rms.merge_row(ftp, None)['time_utc'] == '2026-10-21T03:40:12.680'


# ── station index: measuring pass and keeping the measurement ────────────────

CAM, NIGHT = 'RO001R', '20261020'
CLIP = 'RO001R_20261021_034009_color.mkv'
SPLICE = 'RO001R_20261021_033949_034009_color.mkv'


@pytest.fixture
def index(tmp_path, monkeypatch):
    monkeypatch.setattr(di, 'DB_PATH', tmp_path / 'detections_index.db')
    cap = tmp_path / 'cap'
    night = cap / CAM / NIGHT
    night.mkdir(parents=True)
    for name in (CLIP, SPLICE):
        (night / name).write_bytes(b'mkv')
        old = time.time() - 600
        os.utime(night / name, (old, old))
    monkeypatch.setattr(di, '_ffthumb_env_ok', True)
    calls = []

    def fake_measure(clip, x, y, expected, rotate=False):
        calls.append((clip.name, round(expected, 2)))
        return 4.12

    import clip_timing
    monkeypatch.setattr(clip_timing, 'measure_meteor_start', fake_measure)
    done_nights = {(CAM, NIGHT)}
    monkeypatch.setattr(di.flags_manager, 'is_morning_done',
                        lambda cam, night, cfg: (cam, night) in done_nights)
    con = di._open_db()
    yield con, {'videocapture_path': str(cap)}, calls
    con.close()


def _insert(con, chunk=CLIP):
    payload = {c: None for c in di.COLS}
    payload.update(cam=CAM, date=NIGHT, ff_file='FF_RO001R_20261021_034012_133_1019904.fits',
                   meteor_no=1, time_utc='2026-10-21T03:40:12.680',
                   x_beg=1011.32, y_beg=548.25, chunk_file=chunk)
    con.execute(di._INSERT_SQL, payload)
    con.commit()


def _offset(con):
    return con.execute('SELECT clip_offset_s, clip_offset_file FROM detections').fetchone()


def test_pass_measures_with_the_clip_name_estimate(index):
    con, cfg, calls = index
    _insert(con)
    assert di._clip_offset_pass(con, cfg) == 1
    assert calls == [(CLIP, 3.68)]                    # 03:40:12.68 - 03:40:09
    assert _offset(con) == (4.12, CLIP)
    assert di._clip_offset_pass(con, cfg) == 0        # measured once


def test_reindexing_keeps_the_measurement(index):
    con, cfg, calls = index
    _insert(con)
    di._clip_offset_pass(con, cfg)
    _insert(con)                                       # the session is re-parsed
    assert _offset(con) == (4.12, CLIP)
    assert di._clip_offset_pass(con, cfg) == 0


def test_a_new_splice_is_measured_again(index):
    con, cfg, calls = index
    _insert(con)
    di._clip_offset_pass(con, cfg)
    _insert(con, chunk=SPLICE)                         # detection_lock wrote a splice
    assert di._clip_offset_pass(con, cfg) == 1
    assert calls[-1] == (SPLICE, 23.68)
    assert _offset(con) == (4.12, SPLICE)


def test_a_clip_still_being_written_waits(index):
    con, cfg, calls = index
    _insert(con)
    clip = di._capture_base(cfg) / CAM / NIGHT / CLIP
    os.utime(clip, None)                               # written just now
    assert di._clip_offset_pass(con, cfg) == 0 and calls == []


def test_a_night_before_its_dawn_is_left_for_daylight(index, monkeypatch):
    con, cfg, calls = index
    monkeypatch.setattr(di.flags_manager, 'is_morning_done', lambda cam, night, cfg: False)
    _insert(con)
    assert di._clip_offset_pass(con, cfg) == 0 and calls == []


def test_upgrade_from_v2_reindexes_every_session(tmp_path, monkeypatch):
    db = tmp_path / 'detections_index.db'
    monkeypatch.setattr(di, 'DB_PATH', db)
    con = sqlite3.connect(db)
    con.executescript(di._SCHEMA.replace('    clip_offset_file TEXT,\n', ''))
    con.execute("INSERT INTO indexed_sessions VALUES ('/s', 1.0, 3, 0)")
    con.execute('PRAGMA user_version=2')
    con.commit()
    con.close()
    con = di._open_db()
    cols = {r[1] for r in con.execute('PRAGMA table_info(detections)')}
    assert 'clip_offset_file' in cols
    assert con.execute('SELECT COUNT(*) FROM indexed_sessions').fetchone()[0] == 0
    con.close()


# ── dashboard: the measured offset wins, the camera's median corrects the rest

def test_dashboard_prefers_the_measured_offset():
    from routes.detections import _chunk_from_index_row, _camera_lags
    row = {'cam': CAM, 'time_utc': '2026-10-21T03:40:12.680', 'chunk_file': CLIP,
           'clip_offset_s': 4.12}
    chunk = _chunk_from_index_row(row, CLIP, {CLIP}, frozenset(), ())
    assert chunk['detection_offset_s'] == 4.12
    # measured in another clip than the one served: estimate, not measurement
    other = _chunk_from_index_row(row, SPLICE, {SPLICE}, frozenset(), ())
    assert other['detection_offset_s'] == pytest.approx(23.68)

    lags = _camera_lags([row])
    assert lags[CAM] == pytest.approx(0.44)
    unmeasured = {'cam': CAM, 'time_utc': '2026-10-21T03:45:05.500',
                  'chunk_file': 'RO001R_20261021_034449_color.mkv'}
    got = _chunk_from_index_row(unmeasured, unmeasured['chunk_file'], set(), frozenset(), (),
                                camera_lag_s=lags[CAM])
    assert got['detection_offset_s'] == pytest.approx(16.5 + 0.44)


# ── rotated cameras: offsets measured before #1425 are measured again ────────

# Vâlcele at 11:00 UTC and 20:00 UTC on 2026-10-23: sun well up, sun down.
VALCELE = {'latitude': 46.08, 'longitude': 25.95}
NOON = datetime(2026, 10, 23, 11, 0, tzinfo=timezone.utc)
EVENING = datetime(2026, 10, 23, 20, 0, tzinfo=timezone.utc)

def test_rotated_camera_offsets_are_measured_again_once(index):
    con, cfg, _calls = index
    _insert(con)
    con.execute("UPDATE detections SET clip_offset_s=3.9, clip_offset_file=?", (CLIP,))
    con.commit()
    rotated = {**cfg, **VALCELE, 'stations': {CAM: {'rotate': True}, 'RO001S': {}}}
    assert di._sync_rotation_offsets(con, rotated, now=EVENING) == 0  # waits for daylight
    assert _offset(con) == (3.9, CLIP)
    assert di._sync_rotation_offsets(con, rotated, now=NOON) == 1
    assert _offset(con) == (3.9, None)
    con.execute("UPDATE detections SET clip_offset_file=?", (CLIP,))
    con.commit()
    assert di._sync_rotation_offsets(con, rotated, now=NOON) == 0  # recorded: not again
    assert _offset(con) == (3.9, CLIP)


def test_unrotated_cameras_keep_their_offsets(index):
    con, cfg, _calls = index
    _insert(con)
    con.execute("UPDATE detections SET clip_offset_s=4.12, clip_offset_file=?", (CLIP,))
    con.commit()
    assert di._sync_rotation_offsets(con, {**cfg, 'stations': {CAM: {}}}) == 0
    assert _offset(con) == (4.12, CLIP)


def test_a_camera_whose_rotate_flag_changes_is_measured_again(index):
    con, cfg, _calls = index
    _insert(con)
    di._sync_rotation_offsets(con, {**cfg, 'stations': {CAM: {}}})
    con.execute("UPDATE detections SET clip_offset_s=4.12, clip_offset_file=?", (CLIP,))
    con.commit()
    assert di._sync_rotation_offsets(con, {**cfg, 'stations': {CAM: {'rotate': True}}}) == 1
    assert _offset(con) == (4.12, None)
