"""Station janitor retention and disk-pressure tiers.

Regression tests for the gmnde00 (Johannismühle) data loss (2026-07-25): the
normal sweep must hold uploaded chunks for retention.uploaded_days instead of
deleting them the instant the uploaded flag appears (racing the faststart
remux). The pressure pass that also lost clips there is now the ordered
eviction, pinned in test_janitor_ordered_eviction.py.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import janitor_storage_watchdog as jsw


def _days_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime('%Y%m%d')


def _make_janitor(tmp_path: Path, stations: tuple[str, ...] = ('CAM1',),
                  retention: dict | None = None) -> tuple[jsw.Janitor, Path]:
    capture = tmp_path / 'capture'
    capture.mkdir(exist_ok=True)
    cfg: dict = {
        'videocapture_path': str(capture),
        'log_path': str(tmp_path / 'logs'),
        'stations': {s: {} for s in stations},
    }
    if retention is not None:
        cfg['retention'] = retention
    cfg_path = tmp_path / 'config.json'
    cfg_path.write_text(json.dumps(cfg))
    return jsw.Janitor(str(cfg_path)), capture


def _mkv(capture: Path, station: str, date: str, name: str,
         layout: str = 'videos') -> Path:
    night = capture / station / date
    target = night / 'videos' if layout == 'videos' else night
    target.mkdir(parents=True, exist_ok=True)
    f = target / name
    f.write_bytes(b'x')
    return f


@pytest.fixture(autouse=True)
def _reset_janitor_logger():
    """Janitor.__init__ adds handlers to the module logger — drop them so
    repeated instantiations don't pile up file handlers across tests."""
    yield
    for h in list(jsw.logger.handlers):
        jsw.logger.removeHandler(h)
        h.close()


# ---------------------------------------------------------------------------
# Normal sweep — uploaded_days grace
# ---------------------------------------------------------------------------

class TestUploadedDaysGrace:
    def test_knob_defaults_to_locked_days_default(self, tmp_path):
        j, _ = _make_janitor(tmp_path)
        assert j.uploaded_retention == 7

    def test_knob_read_from_retention_block(self, tmp_path):
        j, _ = _make_janitor(tmp_path, retention={'uploaded_days': 3})
        assert j.uploaded_retention == 3

    @pytest.mark.parametrize('layout', ['videos', 'flat'])
    def test_uploaded_chunk_within_grace_is_held(self, monkeypatch, tmp_path, layout):
        j, capture = _make_janitor(tmp_path)
        date = _days_ago(1)
        f = _mkv(capture, 'CAM1', date, f'CAM1_{date}_010101_color.mkv',
                 layout=layout)

        monkeypatch.setattr(jsw.flags_manager, 'is_chunk_uploaded',
                            lambda *a: True)
        monkeypatch.setattr(jsw, '_is_locked', lambda *a: False)

        j._purge_station('CAM1')
        assert f.exists()

    @pytest.mark.parametrize('layout', ['videos', 'flat'])
    def test_uploaded_chunk_past_grace_is_purged(self, monkeypatch, tmp_path, layout):
        j, capture = _make_janitor(tmp_path)
        date = _days_ago(10)
        f = _mkv(capture, 'CAM1', date, f'CAM1_{date}_010101_color.mkv',
                 layout=layout)

        monkeypatch.setattr(jsw.flags_manager, 'is_chunk_uploaded',
                            lambda *a: True)
        monkeypatch.setattr(jsw, '_is_locked', lambda *a: False)

        j._purge_station('CAM1')
        assert not f.exists()

    def test_shorter_uploaded_days_overrides_default(self, monkeypatch, tmp_path):
        j, capture = _make_janitor(tmp_path, retention={'uploaded_days': 1})
        date = _days_ago(2)
        f = _mkv(capture, 'CAM1', date, f'CAM1_{date}_010101_color.mkv')

        monkeypatch.setattr(jsw.flags_manager, 'is_chunk_uploaded',
                            lambda *a: True)
        monkeypatch.setattr(jsw, '_is_locked', lambda *a: False)

        j._purge_station('CAM1')
        assert not f.exists()

    def test_non_uploaded_unlocked_chunk_still_purged_at_color_days(
            self, monkeypatch, tmp_path):
        """The grace only applies to uploaded chunks — plain old unlocked
        color video still expires at color_days."""
        j, capture = _make_janitor(tmp_path)
        date = _days_ago(3)  # past color_days (2), inside uploaded_days (7)
        f = _mkv(capture, 'CAM1', date, f'CAM1_{date}_010101_color.mkv')

        monkeypatch.setattr(jsw.flags_manager, 'is_chunk_uploaded',
                            lambda *a: False)
        monkeypatch.setattr(jsw, '_is_locked', lambda *a: False)

        j._purge_station('CAM1')
        assert not f.exists()


class TestNightProductsInTheNightDir:
    """timelapse_build writes <cam>_<night>_timelapse.mp4 and _night_stack.webp
    straight into the night dir, beside the chunks. The flat-layout chunk sweep
    deleted every non-.mkv file there once the night passed the colour date cap
    (2 days), so a timelapse that had not uploaded yet was lost before the
    upload-aware timelapse_days (7) rule ever saw it."""

    PRODUCTS = ('_timelapse.mp4', '_night_stack.webp')

    def _night(self, tmp_path, days_ago: int, *, archive: bool = True,
               uploaded: bool = False) -> tuple[jsw.Janitor, list[Path], Path]:
        j, capture = _make_janitor(tmp_path)
        j.archive_enabled = archive
        date = _days_ago(days_ago)
        night = capture / 'CAM1' / date
        night.mkdir(parents=True)
        products = []
        for suffix in self.PRODUCTS:
            f = night / f'CAM1_{date}{suffix}'
            f.write_bytes(b'p')
            products.append(f)
        sidecar = night / f'CAM1_{date}_010101_color.mkv.reencoded'
        sidecar.write_bytes(b'')
        if uploaded:
            jsw.flags_manager.mark_timelapse_uploaded('CAM1', date, j.cfg)
            jsw.flags_manager.mark_night_stack_uploaded('CAM1', date, j.cfg)
        return j, products, sidecar

    def test_not_uploaded_products_survive_the_colour_date_cap(self, tmp_path):
        j, products, sidecar = self._night(tmp_path, days_ago=3)
        j._purge_station('CAM1')
        assert all(p.exists() for p in products)
        assert not sidecar.exists(), "other legacy sidecars still go at the date cap"

    def test_uploaded_products_go_after_timelapse_days(self, tmp_path):
        j, products, _ = self._night(tmp_path, days_ago=8, uploaded=True)
        j._purge_station('CAM1')
        assert not any(p.exists() for p in products)

    def test_not_uploaded_products_stay_after_timelapse_days(self, tmp_path):
        j, products, _ = self._night(tmp_path, days_ago=8)
        j._purge_station('CAM1')
        assert all(p.exists() for p in products)

    def test_without_an_archive_they_follow_timelapse_days(self, tmp_path):
        j, products, _ = self._night(tmp_path, days_ago=3, archive=False)
        j._purge_station('CAM1')
        assert all(p.exists() for p in products)
        (tmp_path / 'later').mkdir()
        j, products, _ = self._night(tmp_path / 'later', days_ago=8, archive=False)
        j._purge_station('CAM1')
        assert not any(p.exists() for p in products)


class TestUnreadableNightIsKept:
    """A night whose state.json exists but cannot be read has unknown locks,
    not none. The janitor used to read it as an empty state, so every chunk
    of the night counted as a non-detection for the date cap, the rolling
    release, the RMS floor and the first pressure phase. It now counts every
    chunk as a detection; only the last-resort pressure tiers, which take
    detections too, can still reach it."""

    def _night(self, tmp_path, monkeypatch, how: str, days_ago: int = 3):
        j, capture = _make_janitor(tmp_path)
        date = _days_ago(days_ago)
        chunks = [_mkv(capture, 'CAM1', date, f'CAM1_{date}_0{i}0000_color.mkv', layout='flat')
                  for i in range(3)]
        state = capture / 'CAM1' / date / 'state.json'
        state.write_text(json.dumps({'chunks': {}, 'morning_done': True, 'rms_complete': True}))
        if how == 'corrupt':
            state.write_text('{"chunks": {}, "morning_done": tr')
        else:
            import errno
            real = Path.read_bytes

            def read_bytes(self):
                if self == state:
                    raise OSError(errno.EIO, 'Input/output error')
                return real(self)

            monkeypatch.setattr(Path, 'read_bytes', read_bytes)
        return j, capture, chunks

    @pytest.fixture(params=['corrupt', 'eio'])
    def how(self, request):
        return request.param

    def test_the_date_cap_keeps_it(self, tmp_path, monkeypatch, how):
        j, _, chunks = self._night(tmp_path, monkeypatch, how)
        with jsw.flags_manager.scan_cache():
            j._purge_station('CAM1')
        assert all(c.exists() for c in chunks)

    def test_every_chunk_counts_as_a_detection(self, tmp_path, monkeypatch, how):
        j, _, chunks = self._night(tmp_path, monkeypatch, how)
        found = {f: locked for _dt, locked, _sid, _d, f in j._collect_chunks()}
        assert found == {c: True for c in chunks}

    def test_the_rolling_release_and_rms_floor_keep_it(self, tmp_path, monkeypatch, how):
        j, _, chunks = self._night(tmp_path, monkeypatch, how)
        j.rms_on_capture_fs = True
        j.rms_floor_override = 50.0
        monkeypatch.setattr(j, '_escalate_captured_sent', lambda over: 0)
        monkeypatch.setattr(j, '_lock_tonights_detections', lambda: None)
        monkeypatch.setattr(jsw, '_fs_stats', lambda path: (99, 1.0))
        with jsw.flags_manager.scan_cache():
            j._rolling_release()
            assert j._evict_for_rms() == 0
        assert all(c.exists() for c in chunks)

    def test_a_readable_night_without_locks_still_goes(self, tmp_path, monkeypatch):
        j, capture, chunks = self._night(tmp_path, monkeypatch, 'corrupt')
        (capture / 'CAM1' / _days_ago(3) / 'state.json').write_text(json.dumps({'chunks': {}}))
        j._purge_station('CAM1')
        assert not any(c.exists() for c in chunks)


class TestRmsSoftPrune:
    """2026-09-23: the soft prune compared the top-level name "2026" with a
    YYYYMMDD cutoff, so it deleted the whole FramesFiles/<year> tree (today's
    day frames included) on every station above 85% disk."""

    def _janitor_with_rms(self, tmp_path):
        rms = tmp_path / 'RMS_data' / 'cam1'
        cfg = {'videocapture_path': str(tmp_path / 'cap'), 'log_path': str(tmp_path / 'logs'),
               'stations': {'CAM1': {'rms_data_path': str(rms)}}}
        (tmp_path / 'cap').mkdir()
        (tmp_path / 'config.json').write_text(json.dumps(cfg))
        return jsw.Janitor(str(tmp_path / 'config.json')), rms

    def test_keeps_recent_day_frames_under_the_year_dir(self, tmp_path):
        jan, rms = self._janitor_with_rms(tmp_path)
        year = _days_ago(0)[:4]
        today = rms / 'FramesFiles' / year / f'{_days_ago(0)}-266' / '10'
        old = rms / 'FramesFiles' / year / f'{_days_ago(5)}-261' / '10'
        for d in (today, old):
            d.mkdir(parents=True)
            (d / 'f.jpg').write_bytes(b'x')
        timelapse = rms / 'FramesFiles' / 'CAM1_x_frames_timelapse.tar'
        timelapse.write_bytes(b't')
        jan._escalate_rms_soft()
        assert today.exists(), "today's day frames must survive a soft prune"
        assert not old.parent.exists(), "frames older than 2 days are pruned"
        assert (rms / 'FramesFiles' / year).exists()
        assert timelapse.exists(), "files at the top level are never touched"

    def test_flat_dated_dirs_still_pruned_and_undated_left(self, tmp_path):
        jan, rms = self._janitor_with_rms(tmp_path)
        tf = rms / 'TimeFiles'
        old = tf / f'CAM1_{_days_ago(4)}_000000'
        new = tf / f'CAM1_{_days_ago(0)}_000000'
        odd = tf / 'misc'
        for d in (old, new, odd):
            d.mkdir(parents=True)
        jan._escalate_rms_soft()
        assert not old.exists() and new.exists() and odd.exists()


class _CapturedFilesFixture:
    """One RMS camera with a CapturedFiles session and RMS log helpers."""

    SESSION_TIME = '173344_786481'

    def _setup(self, tmp_path, days_ago: int = 3):
        rms = tmp_path / 'RMS_data' / 'CAM1'
        cfg = {'videocapture_path': str(tmp_path / 'cap'), 'log_path': str(tmp_path / 'logs'),
               'stations': {'CAM1': {'rms_data_path': str(rms)}}}
        (tmp_path / 'cap').mkdir()
        (tmp_path / 'config.json').write_text(json.dumps(cfg))
        date = _days_ago(days_ago)
        session = rms / 'CapturedFiles' / f'CAM1_{date}_{self.SESSION_TIME}'
        session.mkdir(parents=True)
        (session / 'FF_CAM1.fits').write_bytes(b'x')
        (rms / 'ArchivedFiles').mkdir()
        (rms / 'logs').mkdir()
        return jsw.Janitor(str(tmp_path / 'config.json')), rms, session, date

    def _log(self, rms: Path, date: str, lines: list[str]) -> None:
        (rms / 'logs' / f'log_CAM1_{date}_134539_001.log').write_text('\n'.join(lines) + '\n')

    def _start(self, session: Path, kind: str) -> str:
        return (f'2026/10/04 04:22:56-INFO-UploadManager-line:484 - Starting upload of '
                f'/home/gmn/RMS_data/CAM1/ArchivedFiles/{session.name}_{kind}.tar.bz2 '
                f'(24.80 MB) to files/{session.name}_{kind}.tar.bz2')

    def _verified(self, session: Path, kind: str) -> str:
        return (f'2026/10/04 04:23:27-DEBUG-UploadManager-line:498 - File upload verified: '
                f'files/{session.name}_{kind}.tar.bz2')

    OK = '2026/10/04 04:23:27-INFO-UploadManager-line:806 - Upload successful!'
    FAILED = '2026/10/04 04:23:27-WARNING-UploadManager-line:810 - Uploading failed! Retry 1 of 5'


class TestCapturedFilesProofOfUpload(_CapturedFilesFixture):
    """2026-10-05: gmnde00 (Johannismühle) held every CapturedFiles session but
    the newest night forever. The only accepted proof of sending was the
    imgdata + metadata pair in ArchivedFiles, and RMS keeps only its newest
    night's pair there. RMS's own upload log is now accepted as proof too;
    a session that neither proves stays held. Log lines are copied from
    gmnde00's RMS log of 2026-10-03."""

    def test_archives_in_archivedfiles_still_prove_it(self, tmp_path):
        jan, rms, session, _ = self._setup(tmp_path)
        for kind in ('imgdata', 'metadata'):
            (rms / 'ArchivedFiles' / f'{session.name}_{kind}.tar.bz2').write_bytes(b'z')
        jan._purge_captured_files()
        assert not session.exists()

    def test_verified_uploads_in_the_rms_log_prove_it(self, tmp_path):
        jan, rms, session, date = self._setup(tmp_path)
        self._log(rms, date, [self._start(session, 'metadata'), self._verified(session, 'metadata'),
                              self.OK, self._start(session, 'imgdata'),
                              self._verified(session, 'imgdata'), self.OK])
        jan._purge_captured_files()
        assert not session.exists()

    def test_info_only_log_proves_it_by_start_then_success(self, tmp_path):
        jan, rms, session, date = self._setup(tmp_path)
        self._log(rms, date, [self._start(session, 'metadata'), self.OK,
                              self._start(session, 'imgdata'), self.OK])
        jan._purge_captured_files()
        assert not session.exists()

    def test_retry_logged_the_next_day_counts(self, tmp_path):
        jan, rms, session, date = self._setup(tmp_path)
        self._log(rms, date, [self._start(session, 'metadata'), self.OK])
        self._log(rms, _days_ago(2), [self._start(session, 'imgdata'), self.OK])
        jan._purge_captured_files()
        assert not session.exists()

    def test_failed_imgdata_upload_holds_the_session(self, tmp_path):
        jan, rms, session, date = self._setup(tmp_path)
        self._log(rms, date, [self._start(session, 'metadata'), self.OK,
                              self._start(session, 'imgdata'), self.FAILED])
        jan._purge_captured_files()
        assert session.exists(), 'only metadata reached GMN, so the session must stay'

    def test_session_without_any_upload_record_stays(self, tmp_path):
        # The XM0001/XM0096 test sessions on gmnde00: never uploaded, logs long gone.
        jan, _, session, _ = self._setup(tmp_path, days_ago=180)
        jan._purge_captured_files()
        assert session.exists()

    def test_log_from_before_the_session_does_not_count(self, tmp_path):
        jan, rms, session, _ = self._setup(tmp_path)
        self._log(rms, _days_ago(5), [self._start(session, 'metadata'), self.OK,
                                      self._start(session, 'imgdata'), self.OK])
        jan._purge_captured_files()
        assert session.exists()

    # gmnmd01 (Cahul), 2026-10-19: RMS before the 10-17 update sent one
    # <session>_detected.tar.bz2, and every session of 10-12 to 10-16 was held.
    def test_legacy_detected_archive_in_archivedfiles_proves_it(self, tmp_path):
        jan, rms, session, _ = self._setup(tmp_path)
        (rms / 'ArchivedFiles' / f'{session.name}_detected.tar.bz2').write_bytes(b'z')
        jan._purge_captured_files()
        assert not session.exists()

    def test_legacy_detected_upload_in_the_rms_log_proves_it(self, tmp_path):
        jan, rms, session, date = self._setup(tmp_path)
        self._log(rms, date, [self._start(session, 'detected'), self._verified(session, 'detected'),
                              self.OK])
        jan._purge_captured_files()
        assert not session.exists()

    def test_failed_legacy_detected_upload_holds_the_session(self, tmp_path):
        jan, rms, session, date = self._setup(tmp_path)
        self._log(rms, date, [self._start(session, 'detected'), self.FAILED])
        jan._purge_captured_files()
        assert session.exists()

    def test_recent_session_is_kept_even_when_sent(self, tmp_path):
        jan, rms, session, date = self._setup(tmp_path, days_ago=0)
        self._log(rms, date, [self._start(session, 'metadata'), self.OK,
                              self._start(session, 'imgdata'), self.OK])
        jan._purge_captured_files()
        assert session.exists()



class TestCapturedFilesUnderPressure(_CapturedFilesFixture):
    """gmnro14 (Chiribiș), 2026-10-16: the disk filled at 22:08 with the
    previous night's three sessions (~7 GB each, already at GMN) held until
    the 00:00 date cutoff, and ENOSPC hung every RMS camera. Under pressure a
    sent session goes regardless of its date, once dawn is done with it."""

    def _sent(self, tmp_path, days_ago: int = 0, morning_done: bool = True):
        jan, rms, session, date = self._setup(tmp_path, days_ago=days_ago)
        for kind in ('imgdata', 'metadata'):
            (rms / 'ArchivedFiles' / f'{session.name}_{kind}.tar.bz2').write_bytes(b'z')
        if morning_done:
            jsw.flags_manager.mark_morning_done('CAM1', date, jan.cfg)
        return jan, rms, session

    def test_sent_session_goes_before_the_date_cutoff(self, tmp_path):
        jan, _, session = self._sent(tmp_path)
        assert jan._escalate_captured_sent(lambda: True) == 1
        assert not session.exists()

    def test_normal_sweep_still_keeps_it(self, tmp_path):
        jan, _, session = self._sent(tmp_path)
        jan._purge_captured_files()
        assert session.exists()

    def test_session_dawn_has_not_finished_stays(self, tmp_path):
        jan, _, session = self._sent(tmp_path, morning_done=False)
        assert jan._escalate_captured_sent(lambda: True) == 0
        assert session.exists()

    def test_unsent_session_stays(self, tmp_path):
        jan, _, session, date = self._setup(tmp_path, days_ago=0)
        jsw.flags_manager.mark_morning_done('CAM1', date, jan.cfg)
        assert jan._escalate_captured_sent(lambda: True) == 0
        assert session.exists()

    def test_not_over_the_limit_deletes_nothing(self, tmp_path):
        jan, _, session = self._sent(tmp_path)
        assert jan._escalate_captured_sent(lambda: False) == 0
        assert session.exists()

    def test_oldest_first_and_stops_once_relieved(self, tmp_path):
        jan, rms, newer = self._sent(tmp_path, days_ago=0)
        older = rms / 'CapturedFiles' / f'CAM1_{_days_ago(1)}_{self.SESSION_TIME}'
        older.mkdir()
        for kind in ('imgdata', 'metadata'):
            (rms / 'ArchivedFiles' / f'{older.name}_{kind}.tar.bz2').write_bytes(b'z')
        jsw.flags_manager.mark_morning_done('CAM1', _days_ago(1), jan.cfg)
        checks = iter([True, False])
        assert jan._escalate_captured_sent(lambda: next(checks)) == 1
        assert not older.exists()
        assert newer.exists()

    def test_ordered_eviction_spends_it_before_detections(self, tmp_path, monkeypatch):
        jan, _, session = self._sent(tmp_path)
        jan.rms_on_capture_fs = True
        calls: list[str] = []
        monkeypatch.setattr(jan, '_collect_chunks', lambda: [])
        monkeypatch.setattr(jan, '_evict_chunks', lambda chunks, phase: calls.append(phase) or 0)
        monkeypatch.setattr(jan, '_over_limit', lambda: not calls or session.exists())
        monkeypatch.setattr(jsw, '_fs_stats', lambda path: (50, 100.0))
        jan._ordered_eviction()
        assert not session.exists()
        assert calls == ['non-detection >24h'], 'phase 2 must not run once the session freed the disk'


class TestRmsHardPrune(_CapturedFilesFixture):
    """The hard RMS tier (90% on a disk RMS uses) keeps each camera's 3 newest
    CapturedFiles and ArchivedFiles session dirs. It used to delete every older
    one with no proof of sending, so a station whose GMN upload was failing
    lost its unsent raw FF sessions first. Sessions already at GMN now go
    first, oldest first, until the disk is under the limit; unsent ones follow
    only if that was not enough, so RMS still never runs out of space."""

    def _sessions(self, tmp_path, sent: tuple[bool, ...]):
        """One session per entry, oldest first, in both trees; the last three
        are each tree's newest and are never candidates."""
        jan, rms, first, _ = self._setup(tmp_path, days_ago=len(sent))
        captured, archived = [first], []
        for i in range(len(sent) - 1, 0, -1):
            s = rms / 'CapturedFiles' / f'CAM1_{_days_ago(i)}_{self.SESSION_TIME}'
            s.mkdir()
            captured.append(s)
        for s, is_sent in zip(captured, sent):
            a = rms / 'ArchivedFiles' / s.name
            a.mkdir()
            archived.append(a)
            if is_sent:
                for kind in ('imgdata', 'metadata'):
                    (rms / 'ArchivedFiles' / f'{s.name}_{kind}.tar.bz2').write_bytes(b'z')
        return jan, captured, archived

    @staticmethod
    def _over_for(deletions: int):
        """An over-limit check that clears after *deletions* deletions."""
        calls = iter(range(10**6))
        return lambda: next(calls) < deletions

    def test_sent_sessions_go_before_older_unsent_ones(self, tmp_path):
        jan, captured, archived = self._sessions(
            tmp_path, sent=(False, True, True, True, True, True))
        assert jan._escalate_rms_hard(self._over_for(4)) == 4
        assert [s.exists() for s in captured] == [True, False, False, True, True, True]
        assert [s.exists() for s in archived] == [True, False, False, True, True, True]

    def test_sent_sessions_go_oldest_first_and_stop_once_relieved(self, tmp_path):
        jan, captured, archived = self._sessions(
            tmp_path, sent=(True, True, True, True, True, True))
        assert jan._escalate_rms_hard(self._over_for(1)) == 1
        assert [s.exists() for s in captured] == [False, True, True, True, True, True]
        assert all(s.exists() for s in archived)

    def test_not_over_the_limit_deletes_nothing(self, tmp_path):
        jan, captured, archived = self._sessions(
            tmp_path, sent=(True, False, True, True, True, True))
        assert jan._escalate_rms_hard(lambda: False) == 0
        assert all(s.exists() for s in captured + archived)

    def test_unsent_sessions_still_go_when_sent_ones_are_not_enough(self, tmp_path, caplog):
        """The old rule as a last resort: everything beyond each tree's three
        newest, never more, so RMS cannot hit ENOSPC."""
        jan, captured, archived = self._sessions(
            tmp_path, sent=(False, True, False, True, False, True))
        with caplog.at_level('ERROR', logger='atium'):
            assert jan._escalate_rms_hard(lambda: True) == 6
        assert [s.exists() for s in captured] == [False, False, False, True, True, True]
        assert [s.exists() for s in archived] == [False, False, False, True, True, True]
        unsent_logged = [r for r in caplog.records
                         if r.levelname == 'ERROR' and 'NOT sent to GMN' in r.getMessage()]
        assert len(unsent_logged) == 4   # two unsent sessions, both trees

    def test_runs_in_the_capture_disk_check(self, tmp_path, monkeypatch):
        jan, captured, _ = self._sessions(
            tmp_path, sent=(False, True, True, True, True, True))
        jan.rms_on_capture_fs = True
        monkeypatch.setattr(jan, '_ordered_eviction', lambda: None)
        monkeypatch.setattr(jan, '_escalate_rms_soft', lambda: None)
        monkeypatch.setattr(jsw, '_fs_stats', lambda path: (95, 1.0))
        # Two reads before the hard tier, then one per deletion it considers.
        pcts = iter([95, 95, 95, 50])
        monkeypatch.setattr(jsw, '_disk_pct', lambda path: next(pcts, 50))
        jan._run_disk_check()
        assert [s.exists() for s in captured] == [True, False, True, True, True, True]


@pytest.mark.parametrize('log_path', [None, ''])
def test_null_log_path_falls_back_to_home_logs(tmp_path, monkeypatch, log_path):
    """config_defaults.json ships ``"log_path": null`` and config_migrate
    copies missing keys as-is, so a migrated config can hold a present-but-null
    log_path. ``cfg.get('log_path', default)`` returned None there and
    ``Path(None)`` raised TypeError before the janitor could run at all."""
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    capture = tmp_path / 'capture'
    capture.mkdir()
    cfg_path = tmp_path / 'config.json'
    cfg_path.write_text(json.dumps({'videocapture_path': str(capture), 'log_path': log_path,
                                    'stations': {}}))
    jan = jsw.Janitor(str(cfg_path))
    assert jan.log_base == tmp_path / 'logs'


# ---------------------------------------------------------------------------
# Renamed / removed cameras: their dirs stay under the capture root
# ---------------------------------------------------------------------------

class TestUnconfiguredCameraDirs:
    """2026-10-07, gmnro20 (Brăila): RO0020 (renamed to RO001M in September)
    and RO20XB (renamed to RO001N on 2026-10-06) kept their colour nights under
    the old codes. The janitor walked only config["stations"], so about 96 GB
    sat outside retention and pressure eviction until the disk filled at 00:59
    and both RMS cameras stopped with ENOSPC."""

    def test_orphan_dir_is_a_camera_and_non_camera_dirs_are_not(self, tmp_path):
        j, capture = _make_janitor(tmp_path, stations=('RO001N',))
        _mkv(capture, 'RO20XB', _days_ago(3), 'RO20XB_x_010101_color.mkv')
        (capture / 'misc' / 'notes').mkdir(parents=True)
        (capture / '.trash' / _days_ago(3)).mkdir(parents=True)
        (capture / 'stray.txt').write_text('x')

        assert j._camera_ids() == ['RO001N', 'RO20XB']

    def test_normal_sweep_applies_retention_to_orphan_and_removes_its_dir(
            self, monkeypatch, tmp_path):
        j, capture = _make_janitor(tmp_path, stations=('RO001N',))
        old = _days_ago(3)  # past color_days (2)
        f = _mkv(capture, 'RO20XB', old, f'RO20XB_{old}_010101_color.mkv')

        monkeypatch.setattr(jsw.flags_manager, 'is_chunk_uploaded', lambda *a: False)
        monkeypatch.setattr(jsw, '_is_locked', lambda *a: False)

        j._run_normal_sweep()
        assert not f.exists()
        assert not (capture / 'RO20XB').exists()

    def test_orphan_night_within_retention_is_kept(self, monkeypatch, tmp_path):
        j, capture = _make_janitor(tmp_path, stations=('RO001N',))
        recent = _days_ago(1)
        f = _mkv(capture, 'RO20XB', recent, f'RO20XB_{recent}_010101_color.mkv')

        monkeypatch.setattr(jsw.flags_manager, 'is_chunk_uploaded', lambda *a: False)
        monkeypatch.setattr(jsw, '_is_locked', lambda *a: False)

        j._run_normal_sweep()
        assert f.exists()

    def test_pressure_eviction_sees_orphan_nights(self, tmp_path):
        j, capture = _make_janitor(tmp_path, stations=('RO001M',))
        night = _days_ago(2)
        _mkv(capture, 'RO0020', night, f'RO0020_{night}_010101_color.mkv')

        assert [(sid, d) for d, sid, _ in j._collect_date_dirs()] == [('RO0020', night)]
        assert [c[2] for c in j._collect_chunks()] == ['RO0020']


class TestPressureRecord:
    """janitor_state.json's 'pressure' record: an unrelieved escalation episode,
    carried across runs until a run relieves the disk (gmnro14 (Chiribiș)
    logged the ERROR for five days without anyone seeing it)."""

    def _run(self, j: jsw.Janitor, monkeypatch, over: bool) -> dict:
        monkeypatch.setattr(j, '_over_limit', lambda: over)
        monkeypatch.setattr(j, '_ordered_eviction', lambda: None)
        monkeypatch.setattr(jsw, '_fs_stats', lambda path: (98 if over else 70, 3.6 if over else 60.0))
        monkeypatch.setattr(jsw, '_disk_pct', lambda path: 98 if over else 70)
        j.same_fs = False
        j.rms_on_capture_fs = False
        j._unrelieved = None
        j._run_disk_check()
        j._write_state()
        return json.loads((j.log_base / 'janitor_state.json').read_text())

    def test_episode_starts_counts_and_clears(self, tmp_path, monkeypatch):
        j, _ = _make_janitor(tmp_path)
        first = self._run(j, monkeypatch, over=True)['pressure']
        assert first['runs'] == 1 and first['pct'] == 98 and first['free_gb'] == 3.6
        second = self._run(j, monkeypatch, over=True)['pressure']
        assert second['runs'] == 2 and second['since'] == first['since']
        assert self._run(j, monkeypatch, over=False)['pressure'] is None
        assert self._run(j, monkeypatch, over=True)['pressure']['runs'] == 1

    def test_unreadable_previous_state_starts_a_new_episode(self, tmp_path, monkeypatch):
        j, _ = _make_janitor(tmp_path)
        j.log_base.mkdir(parents=True, exist_ok=True)
        (j.log_base / 'janitor_state.json').write_text('{not json')
        assert self._run(j, monkeypatch, over=True)['pressure']['runs'] == 1


class TestRmsOnCaptureDisk:
    """Whether RMS data shares the capture disk is decided from each camera's
    rms_data_path, not from $HOME: gmnro09 (Sibiciu) had ~/color_capture and
    ~/RMS_data both symlinked onto one USB disk, so the old home-based check
    said "separate" and capture-disk pressure never freed RMS sessions already
    at GMN (disk full at 03:17 UTC on 2026-11-11)."""

    def _janitor(self, tmp_path, monkeypatch, rms_on_usb: bool) -> jsw.Janitor:
        usb = tmp_path / 'usb'
        (usb / 'color_capture').mkdir(parents=True)
        (usb / 'RMS_data' / 'cam1').mkdir(parents=True)
        home = tmp_path / 'home'
        (home / 'RMS_data' / 'cam1').mkdir(parents=True)
        (home / 'color_capture').symlink_to(usb / 'color_capture')
        rms = (usb if rms_on_usb else home) / 'RMS_data' / 'cam1'
        real_stat = os.stat

        def fake_stat(path, *a, **kw):
            st = real_stat(path, *a, **kw)
            resolved = Path(os.path.realpath(path))
            for dev, root in ((2, usb), (1, home)):
                if resolved == root or root in resolved.parents:
                    fields = list(st)
                    fields[2] = dev  # st_dev
                    return os.stat_result(fields)
            return st

        monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
        monkeypatch.setattr(jsw.os, 'stat', fake_stat)
        cfg = {
            'videocapture_path': str(home / 'color_capture'),
            'log_path': str(tmp_path / 'logs'),
            'stations': {'CAM1': {'rms_data_path': str(rms)}},
        }
        cfg_path = tmp_path / 'config.json'
        cfg_path.write_text(json.dumps(cfg))
        return jsw.Janitor(str(cfg_path))

    def test_symlinked_rms_on_capture_disk_is_detected(self, tmp_path, monkeypatch):
        jan = self._janitor(tmp_path, monkeypatch, rms_on_usb=True)
        assert jan.same_fs is False
        assert jan.rms_on_capture_fs is True

    def test_rms_on_home_disk_is_separate(self, tmp_path, monkeypatch):
        jan = self._janitor(tmp_path, monkeypatch, rms_on_usb=False)
        assert jan.same_fs is False
        assert jan.rms_on_capture_fs is False

    def test_capture_pressure_frees_sent_rms_sessions(self, tmp_path, monkeypatch):
        jan = self._janitor(tmp_path, monkeypatch, rms_on_usb=True)
        freed: list[str] = []
        monkeypatch.setattr(jan, '_collect_chunks', lambda: [])
        monkeypatch.setattr(jan, '_evict_chunks', lambda chunks, phase: 0)
        monkeypatch.setattr(jan, '_escalate_captured_sent',
                            lambda over: freed.append('sent') or 1)
        monkeypatch.setattr(jan, '_over_limit', lambda: not freed)
        monkeypatch.setattr(jsw, '_fs_stats', lambda path: (50, 100.0))
        jan._ordered_eviction()
        assert freed == ['sent']
