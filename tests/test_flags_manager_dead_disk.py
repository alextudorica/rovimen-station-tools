"""Tests for flags_manager's behaviour when the capture disk is gone.

This is the gmn0004 failure, twice: the colour disk died, ``/mnt/data``
reverted to a root-owned stub on ``/``, and every attempt to record an
upload raised ``PermissionError`` out of ``save()`` -- which unwound through
``mark_rms_file_uploaded`` and aborted the ENTIRE archive-upload cycle, for
RMS plots and meteor clips that have nothing to do with colour video. The
station POSTed a file, died before recording it, and re-sent the same file
every 20 minutes for five days (2026-08-07..12), then four more
(2026-09-17..21).

Two independent guarantees keep that from recurring:
  * the flags relocate to a writable fallback, so dedupe keeps working; and
  * even if writing fails anyway, the exception does not escape ``save()``.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

import flags_manager


@pytest.fixture(autouse=True)
def _clear_warn_sets():
    flags_manager._unreadable_warned.clear()
    flags_manager._fallback_warned.clear()
    yield
    flags_manager._unreadable_warned.clear()
    flags_manager._fallback_warned.clear()


@pytest.fixture
def dead_disk(tmp_path, monkeypatch):
    """A capture root that exists but cannot be written to -- exactly the
    shape of an unmounted /mnt/data: present, owned by root, mode 755."""
    monkeypatch.setattr(Path, 'home', lambda: tmp_path / 'home')
    monkeypatch.setattr(flags_manager, '_FALLBACK_STATE_ROOT',
                        tmp_path / 'home' / 'rovimen_scripts' / 'upload_state')
    root = tmp_path / 'mnt' / 'data' / 'color_capture'
    root.mkdir(parents=True)
    root.chmod(stat.S_IRUSR | stat.S_IXUSR)   # r-x, no write
    yield {'cfg': {'videocapture_path': str(root)}, 'root': root, 'tmp': tmp_path}
    root.chmod(0o755)


class TestStateRootFallback:

    def test_writable_root_is_used_unchanged(self, tmp_path):
        root = tmp_path / 'color_capture'
        root.mkdir()
        cfg = {'videocapture_path': str(root)}
        assert flags_manager._state_root(cfg) == root

    def test_missing_but_creatable_root_is_still_used(self, tmp_path):
        """A fresh station has no capture dir yet. That is creatable, not the
        failure this guards against, and must not divert the flags."""
        root = tmp_path / 'not_yet'
        cfg = {'videocapture_path': str(root)}
        assert flags_manager._state_root(cfg) == root
        assert root.is_dir()

    @pytest.mark.skipif(os.geteuid() == 0, reason='root ignores directory permissions')
    def test_unwritable_root_diverts_to_the_fallback(self, dead_disk):
        cfg = {**dead_disk['cfg'], 'capture_fallback': {'enabled': False}}
        assert flags_manager._state_root(cfg) == flags_manager._FALLBACK_STATE_ROOT

    @pytest.mark.skipif(os.geteuid() == 0, reason='root ignores directory permissions')
    def test_flags_follow_colour_onto_its_fallback_root(self, dead_disk):
        """With colour capture diverted to ~/color_capture (rovimen_config.
        capture_root), a night's flags sit beside its video there."""
        home_cc = dead_disk['tmp'] / 'home' / 'color_capture'
        assert flags_manager._state_root(dead_disk['cfg']) == home_cc

    @pytest.mark.skipif(os.geteuid() == 0, reason='root ignores directory permissions')
    def test_flags_round_trip_through_the_fallback(self, dead_disk):
        """The point of the fallback: with the capture disk gone, uploads
        still remember what they already sent, so the archive sweep does not
        re-upload the same night forever."""
        cfg = dead_disk['cfg']
        flags_manager.mark_ready('RO000T', '20260920', 'chunk_color.mkv', cfg)
        state = flags_manager.load('RO000T', '20260920', cfg)
        assert state['chunks']['chunk_color.mkv']['ready'] is True


class TestSaveDoesNotUnwindTheCaller:

    @pytest.mark.skipif(os.geteuid() == 0, reason='root ignores directory permissions')
    def test_save_swallows_an_unwritable_state_dir(self, tmp_path, monkeypatch):
        """Even with the fallback unavailable too, save() must not raise: its
        callers are one-line "record that this uploaded" calls inside long
        sweeps, and an exception there stops the station archiving anything.
        A lost flag costs one redundant re-upload of an already-archived
        file."""
        monkeypatch.setattr(Path, 'home', lambda: tmp_path / 'home')
        blocked = tmp_path / 'blocked'
        blocked.mkdir()
        blocked.chmod(stat.S_IRUSR | stat.S_IXUSR)
        monkeypatch.setattr(flags_manager, '_FALLBACK_STATE_ROOT', blocked / 'fallback')
        monkeypatch.setattr(flags_manager, '_state_root', lambda cfg: blocked / 'nope')
        try:
            flags_manager.save({'chunks': {}}, 'RO000T', '20260920', {})
        finally:
            blocked.chmod(0o755)

    def test_a_successful_save_still_writes(self, tmp_path):
        cfg = {'videocapture_path': str(tmp_path / 'cap')}
        flags_manager.save({'chunks': {}, 'station': 'RO000T'}, 'RO000T', '20260920', cfg)
        assert (tmp_path / 'cap' / 'RO000T' / '20260920' / 'state.json').exists()
