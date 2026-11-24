"""Tests for resolve_rms_config_dir -- which directory holds a camera's
.config, platepar and the mask.bmp RMS actually loads.

The fleet runs two layouts and the old derivation only fitted one:

  * gmnro02: ``rms_data_path=~/RMS_data/cam1`` -> ``~/RMS_cam1``. Correct.
  * gmn0000: ``rms_data_path=~/RMS_data/DE001B`` -> ``~/RMS_DE001B``, which
    does not exist. That camera's config dir is ``~/RMS_cam1``, as its
    ``.config`` declares via ``stationID`` and as the capture log confirms
    ("Loaded mask: /home/gmn/RMS_cam1/mask.bmp").

The second case reached production: the mask write endpoint answered
``RMS config directory /home/gmn/RMS_DE001B does not exist`` for every
camera on that station. Resolution is now by stationID, which is the same
binding RMS itself runs on.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / 'rovimen-scripts'))
import platepar_autofit  # noqa: E402
import station_api as sa  # noqa: E402


def _config_dir(home: Path, name: str, station_id: str) -> Path:
    d = home / name
    d.mkdir(parents=True, exist_ok=True)
    (d / '.config').write_text(
        f'[System]\nstationID: {station_id}\nwidth: 1280\nheight: 720\n')
    return d


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(sa, 'BASE', tmp_path)
    return tmp_path


class TestCamNLayout:
    """gmnro02-style: the data path already ends in camN."""

    def test_resolves_by_station_id(self, home):
        want = _config_dir(home, 'RMS_cam1', 'RO000M')
        _config_dir(home, 'RMS_cam2', 'RO000N')
        scfg = {'rms_data_path': str(home / 'RMS_data' / 'cam1')}
        assert platepar_autofit.resolve_rms_config_dir('RO000M', scfg) == want

    def test_falls_back_to_the_camn_derivation_when_no_config_declares_it(self, home):
        """A station whose .config files are unreadable still resolves, as
        long as the directory follows the naming convention."""
        fallback = home / 'RMS_cam1'
        fallback.mkdir(parents=True)
        scfg = {'rms_data_path': str(home / 'RMS_data' / 'cam1')}
        assert platepar_autofit.resolve_rms_config_dir('RO000M', scfg) == fallback


class TestCameraCodeLayout:
    """gmn0000-style: the data path ends in the camera code, and the
    derivation lands on a directory that does not exist."""

    def test_station_id_wins_over_the_broken_derivation(self, home):
        want = _config_dir(home, 'RMS_cam1', 'DE001B')
        _config_dir(home, 'RMS_cam2', 'DE001C')
        _config_dir(home, 'RMS_cam3', 'DE001D')
        scfg = {'rms_data_path': str(home / 'RMS_data' / 'DE001B')}
        assert platepar_autofit.resolve_rms_config_dir('DE001B', scfg) == want

    def test_each_camera_gets_its_own_directory(self, home):
        _config_dir(home, 'RMS_cam1', 'DE001B')
        want = _config_dir(home, 'RMS_cam3', 'DE001D')
        scfg = {'rms_data_path': str(home / 'RMS_data' / 'DE001D')}
        assert platepar_autofit.resolve_rms_config_dir('DE001D', scfg) == want

    def test_the_old_derivation_would_have_produced_a_missing_directory(self, home):
        """Pins the actual production failure rather than only its fix."""
        _config_dir(home, 'RMS_cam1', 'DE001B')
        scfg = {'rms_data_path': str(home / 'RMS_data' / 'DE001B')}
        assert not (home / 'RMS_DE001B').exists()
        assert platepar_autofit.resolve_rms_config_dir('DE001B', scfg) == home / 'RMS_cam1'


class TestOverrideAndFailure:

    def test_explicit_override_wins_over_everything(self, home):
        _config_dir(home, 'RMS_cam1', 'DE001B')
        scfg = {'rms_config_path': str(home / 'elsewhere'),
                'rms_data_path': str(home / 'RMS_data' / 'DE001B')}
        assert platepar_autofit.resolve_rms_config_dir('DE001B', scfg) == home / 'elsewhere'

    def test_unresolvable_returns_none_rather_than_a_guess(self, home):
        """Returning a path that does not exist is worse than admitting it:
        callers write files, and a guess sends them somewhere nothing reads."""
        scfg = {'rms_data_path': str(home / 'RMS_data' / 'DE001B')}
        assert platepar_autofit.resolve_rms_config_dir('DE001B', scfg) is None

    def test_a_non_matching_station_id_is_not_accepted(self, home):
        _config_dir(home, 'RMS_cam1', 'DE001B')
        scfg = {'rms_data_path': str(home / 'RMS_data' / 'XX9999')}
        assert platepar_autofit.resolve_rms_config_dir('XX9999', scfg) is None

    def test_station_id_match_is_case_insensitive(self, home):
        want = _config_dir(home, 'RMS_cam1', 'de001b')
        scfg = {'rms_data_path': str(home / 'RMS_data' / 'DE001B')}
        assert platepar_autofit.resolve_rms_config_dir('DE001B', scfg) == want


class TestStationApiAgrees:
    """The mask endpoints and the auto-fit run must target the same
    directory; they had two copies of the derivation and only one was fixed
    the first time."""

    def test_station_api_delegates_to_the_same_resolver(self, home):
        want = _config_dir(home, 'RMS_cam1', 'DE001B')
        cfg = {'stations': {'DE001B': {'rms_data_path': str(home / 'RMS_data' / 'DE001B')}}}
        assert sa._rms_config_dir('DE001B', cfg) == want
        assert sa._mask_write_target('DE001B', cfg) == want / 'mask.bmp'

    def test_the_source_stations_decoy_is_never_the_write_target(self, home):
        """~/source/Stations/<cam>/mask.bmp exists on some stations and RMS
        never reads it -- writing there changes nothing the camera does."""
        want = _config_dir(home, 'RMS_cam1', 'DE001B')
        decoy = home / 'source' / 'Stations' / 'DE001B'
        decoy.mkdir(parents=True)
        (decoy / 'mask.bmp').write_bytes(b'BM-decoy')
        cfg = {'stations': {'DE001B': {'rms_data_path': str(home / 'RMS_data' / 'DE001B')}}}
        assert sa._mask_write_target('DE001B', cfg) == want / 'mask.bmp'

    def test_find_latest_mask_prefers_the_real_one_over_the_decoy(self, home):
        config_dir = _config_dir(home, 'RMS_cam1', 'DE001B')
        (config_dir / 'mask.bmp').write_bytes(b'BM-real')
        decoy = home / 'source' / 'Stations' / 'DE001B'
        decoy.mkdir(parents=True)
        (decoy / 'mask.bmp').write_bytes(b'BM-decoy')
        cfg = {'stations': {'DE001B': {'rms_data_path': str(home / 'RMS_data' / 'DE001B')}}}
        assert sa._find_latest_mask('DE001B', cfg) == config_dir / 'mask.bmp'


class TestLiveProcessLayout:
    """gmnro05 (Bistrița): every camera runs as
    ``RMS.StartCapture -c ~/source/Stations/<cam>/.config``, live-reading the
    mask and platepar from there. Confirmed on the real station via
    ``ps aux`` and ``/proc/<pid>/cmdline`` after resolve_rms_config_dir
    returned None for every Bistrița camera -- the directory-naming
    heuristics above never produce this path (it isn't under ~/RMS_*, and
    it isn't an ancestor/descendant of rms_data_path, which lives under the
    unrelated ~/RMS_data/ tree), and it was previously assumed to always be
    the inert decoy TestStationApiAgrees pins elsewhere. Whether it's live
    or inert is exactly what the process check has to settle -- these tests
    fake /proc rather than the filesystem-heuristic candidates above.
    """

    def _fake_proc(self, monkeypatch, entries: dict[str, list[str]]):
        """entries: {pid_str: argv_list}. Patches os.listdir('/proc') and
        Path('/proc', pid, 'cmdline').read_bytes() to serve them."""
        import os

        monkeypatch.setattr(os, 'listdir', lambda p: list(entries) if p == '/proc' else os.listdir(p))
        real_read_bytes = Path.read_bytes

        def fake_read_bytes(self):
            parts = self.parts
            if len(parts) >= 3 and parts[-3] == 'proc' and parts[-1] == 'cmdline':
                pid = parts[-2]
                if pid in entries:
                    return ('\0'.join(entries[pid]) + '\0').encode()
                raise OSError('no such process')
            return real_read_bytes(self)

        monkeypatch.setattr(Path, 'read_bytes', fake_read_bytes)

    def test_resolves_via_a_live_rms_startcapture_process(self, home, monkeypatch):
        config_dir = _config_dir(home, 'source/Stations/RO0003', 'RO0003')
        self._fake_proc(monkeypatch, {
            '849': ['/home/rmsuser/vRMS/bin/python', '-m', 'RMS.StartCapture',
                    '-c', str(config_dir / '.config')],
        })
        scfg = {'rms_data_path': str(home / 'RMS_data' / 'RO0003')}
        assert platepar_autofit.resolve_rms_config_dir('RO0003', scfg) == config_dir

    def test_the_walk_out_from_rms_data_path_does_not_reach_it(self, home, monkeypatch):
        """Sanity check that this genuinely needs the process check: the
        existing heuristics alone must still fail to find it."""
        _config_dir(home, 'source/Stations/RO0003', 'RO0003')
        scfg = {'rms_data_path': str(home / 'RMS_data' / 'RO0003')}
        assert platepar_autofit.resolve_rms_config_dir('RO0003', scfg) is None

    def test_directory_naming_candidates_still_win_when_present(self, home, monkeypatch):
        """The live-process check is tried last -- an ~/RMS_* match some
        other camera on the same box happens to have running shouldn't ever
        be preferred over this camera's own conventionally-named directory."""
        want = _config_dir(home, 'RMS_cam1', 'RO0003')
        decoy_live = _config_dir(home, 'source/Stations/RO0003', 'RO0003')
        self._fake_proc(monkeypatch, {
            '849': ['python', '-m', 'RMS.StartCapture', '-c', str(decoy_live / '.config')],
        })
        scfg = {'rms_data_path': str(home / 'RMS_data' / 'cam1')}
        assert platepar_autofit.resolve_rms_config_dir('RO0003', scfg) == want

    def test_a_process_for_a_different_camera_is_not_matched(self, home, monkeypatch):
        other = _config_dir(home, 'source/Stations/RO0004', 'RO0004')
        self._fake_proc(monkeypatch, {
            '850': ['python', '-m', 'RMS.StartCapture', '-c', str(other / '.config')],
        })
        scfg = {'rms_data_path': str(home / 'RMS_data' / 'RO0003')}
        assert platepar_autofit.resolve_rms_config_dir('RO0003', scfg) is None

    def test_a_non_rms_process_is_ignored(self, home, monkeypatch):
        config_dir = _config_dir(home, 'source/Stations/RO0003', 'RO0003')
        self._fake_proc(monkeypatch, {
            '1': ['/sbin/init'],
            '900': ['python', '-m', 'some.other.tool', '-c', str(config_dir / '.config')],
        })
        scfg = {'rms_data_path': str(home / 'RMS_data' / 'RO0003')}
        assert platepar_autofit.resolve_rms_config_dir('RO0003', scfg) is None

    def test_a_dead_pid_between_listdir_and_read_is_skipped_not_fatal(self, home, monkeypatch):
        import os
        monkeypatch.setattr(os, 'listdir', lambda p: ['999'] if p == '/proc' else os.listdir(p))

        def raising_read_bytes(self):
            if 'proc' in self.parts and self.parts[-1] == 'cmdline':
                raise OSError('process exited')
            return Path.read_bytes(self)
        monkeypatch.setattr(Path, 'read_bytes', raising_read_bytes)
        assert platepar_autofit._live_rms_config_dirs() == []


class TestNestedInstallLayout:
    """gmnde01: everything under ~/gmn_rms -- data in ~/gmn_rms/data/DE0018,
    live config in ~/gmn_rms/source/RMS. The home-directory RMS_* glob cannot
    see it, so the resolver has to walk out from rms_data_path."""

    @pytest.fixture
    def station(self, home):
        install = home / 'gmn_rms'
        (install / 'data' / 'DE0018').mkdir(parents=True)
        config_dir = install / 'source' / 'RMS'
        config_dir.mkdir(parents=True)
        (config_dir / '.config').write_text('stationID: DE0018\nwidth: 1280\nheight: 720\n')
        (install / 'venv_rms').mkdir()
        return {'config_dir': config_dir,
                'scfg': {'rms_data_path': str(install / 'data' / 'DE0018')}}

    def test_resolves_two_levels_out_from_the_data_path(self, station):
        assert platepar_autofit.resolve_rms_config_dir('DE0018', station['scfg']) \
            == station['config_dir']

    def test_a_per_night_config_copy_is_never_chosen(self, station, home):
        """RMS copies .config into every night directory, so they declare the
        right stationID and are all the wrong answer -- a mask written into
        one changes nothing the camera does."""
        session = home / 'gmn_rms' / 'data' / 'DE0018' / 'ArchivedFiles' / 'DE0018_20260920_180000_000000'
        session.mkdir(parents=True)
        (session / '.config').write_text('stationID: DE0018\n')
        assert platepar_autofit.resolve_rms_config_dir('DE0018', station['scfg']) \
            == station['config_dir']

    def test_captured_files_copies_are_skipped_too(self, station, home):
        session = home / 'gmn_rms' / 'data' / 'DE0018' / 'CapturedFiles' / 'DE0018_20260921_180000_000000'
        session.mkdir(parents=True)
        (session / '.config').write_text('stationID: DE0018\n')
        assert platepar_autofit.resolve_rms_config_dir('DE0018', station['scfg']) \
            == station['config_dir']

    def test_the_walk_does_not_escape_the_home_directory(self, station, monkeypatch, tmp_path):
        """The ancestors of rms_data_path run all the way to /. Scanning
        outside home would be both slow and none of our business."""
        outside = tmp_path.parent / 'outside_home_RMS'
        scanned = platepar_autofit._candidate_config_dirs(station['scfg'])
        assert all(str(d).startswith(str(tmp_path)) for d in scanned), scanned
        assert outside not in scanned

    def test_the_scan_is_bounded(self, station, home):
        wide = home / 'gmn_rms' / 'wide'
        for i in range(60):
            (wide / f'd{i}' / f'sub{i}').mkdir(parents=True)
        dirs = platepar_autofit._candidate_config_dirs(station['scfg'])
        assert len(dirs) <= platepar_autofit._CONFIG_SCAN_MAX_DIRS


class TestStockCheckoutLayout:
    """gmnro06 (Cluj): the stock single-camera install. RMS runs from
    ~/source/RMS without -c and reads ./.config there; data is ~/RMS_data,
    whose parent is home, so the walk out from rms_data_path never reaches
    the checkout. Before this, saving RO000R's mask answered "could not
    resolve this camera's RMS config directory"."""

    def test_resolves_the_source_rms_checkout(self, home, monkeypatch):
        want = _config_dir(home, 'source/RMS', 'RO000R')
        (home / 'RMS_data').mkdir()
        scfg = {'rms_data_path': str(home / 'RMS_data')}
        assert platepar_autofit.resolve_rms_config_dir('RO000R', scfg, live_dirs=[]) == want

    def test_mask_write_target_lands_in_the_checkout(self, home):
        want = _config_dir(home, 'source/RMS', 'RO000R')
        cfg = {'stations': {'RO000R': {'rms_data_path': str(home / 'RMS_data')}}}
        assert sa._mask_write_target('RO000R', cfg) == want / 'mask.bmp'

    def test_an_rms_camn_directory_still_wins_over_the_checkout(self, home):
        """Multi-camera stations keep a checkout whose .config can declare one
        camera's code; the per-camera directory is the one RMS loads."""
        want = _config_dir(home, 'RMS_cam1', 'RO000M')
        _config_dir(home, 'source/RMS', 'RO000M')
        scfg = {'rms_data_path': str(home / 'RMS_data' / 'cam1')}
        assert platepar_autofit.resolve_rms_config_dir('RO000M', scfg, live_dirs=[]) == want

    def test_a_live_process_without_c_resolves_through_its_cwd(self, home, monkeypatch):
        import os
        import rovimen_rms
        config_dir = _config_dir(home, 'elsewhere/RMS', 'RO000R')
        real_listdir, real_readlink = os.listdir, os.readlink
        monkeypatch.setattr(os, 'listdir', lambda p: ['777'] if p == '/proc' else real_listdir(p))
        monkeypatch.setattr(os, 'readlink',
                            lambda p: str(config_dir) if p == '/proc/777/cwd' else real_readlink(p))
        real_read_bytes = Path.read_bytes

        def fake_read_bytes(self):
            if self.parts[-3:] == ('proc', '777', 'cmdline'):
                return b'python\0-m\0RMS.StartCapture\0'
            return real_read_bytes(self)
        monkeypatch.setattr(Path, 'read_bytes', fake_read_bytes)
        assert rovimen_rms.live_rms_config_dirs() == [config_dir]
