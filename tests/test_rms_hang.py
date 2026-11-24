"""rms_hang: restart an RMS that hung on a full disk while dawn waits on it.

gmnro14 (Chiribiș), 2026-10-16: the disk filled at 22:08:50 and all three RMS
cameras logged "[Errno 28] No space left on device", then went silent with
their processes alive. Dawn logged "Waiting for RMS to finish" from 04:00
until a manual restart at 08:06, after which RMS reprocessed the night.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import flags_manager
import rms_hang

NOW = datetime(2026, 10, 17, 7, 0, tzinfo=timezone.utc)
NIGHT = '20261016'
ENOSPC = ('2026/10/16 22:08:50-ERROR-BufferedCapture-line:1803 - '
          'Error in capture process: [Errno 28] No space left on device')


class Station:
    """A fake /proc plus per-camera RMS configs and logs."""

    def __init__(self, tmp_path: Path):
        self.root = tmp_path
        self.proc = tmp_path / 'proc'
        self.proc.mkdir()
        (tmp_path / 'capture').mkdir()
        self.cfg = {'videocapture_path': str(tmp_path / 'capture'),
                    'log_path': str(tmp_path / 'logs'), 'stations': {}}
        self._pid = 1000

    def camera(self, code: str, n: int, *, idle_min: float, lines: tuple[str, ...] = (ENOSPC,),
               cgroup: str | None = None, auto_reprocess: bool = True, running: bool = True) -> Path:
        cam_dir = self.root / f'RMS_cam{n}'
        data = self.root / 'RMS_data' / f'cam{n}'
        (data / 'logs').mkdir(parents=True)
        config = cam_dir / '.config'
        cam_dir.mkdir()
        config.write_text(f'[System]\nstationID: {code}\n[Capture]\ndata_dir: {data}\nlog_dir: logs\n'
                          f'auto_reprocess: {str(auto_reprocess).lower()}\n')
        log = data / 'logs' / f'log_{code}_20261016_134539_001.log'
        log.write_text('\n'.join(lines) + '\n')
        mtime = (NOW - timedelta(minutes=idle_min)).timestamp()
        os.utime(log, (mtime, mtime))
        self.cfg['stations'][code] = {'rms_data_path': str(data)}
        if running:
            self._pid += 1
            d = self.proc / str(self._pid)
            d.mkdir()
            (d / 'cmdline').write_bytes(
                b'/home/gmn/RMS/venv/bin/python\0-m\0RMS.StartCapture\0-c\0' + str(config).encode() + b'\0')
            os.symlink(str(cam_dir), d / 'cwd')
            (d / 'cgroup').write_text(cgroup or f'0::/system.slice/rms-cam{n}.service\n')
        return log


class Restarts:
    def __init__(self, ok: bool = True):
        self.ok, self.calls = ok, []

    def __call__(self, unit: str, user_scope: bool):
        self.calls.append((unit, user_scope))
        return self.ok, '' if self.ok else 'Failed to restart rms-cam1.service: Access denied'


@pytest.fixture
def station(tmp_path, monkeypatch):
    monkeypatch.setattr(rms_hang.shutil, 'disk_usage',
                        lambda p: type('U', (), {'free': 96e9})())
    return Station(tmp_path)


def _recover(station: Station, restart: Restarts, waiting=('RO001B', 'RO001C', 'RO001D')):
    return rms_hang.recover(waiting, NIGHT, station.cfg, now=NOW, restart=restart, proc=station.proc)


def test_the_chiribis_hang_restarts_every_camera_once(station, caplog):
    for n, code in enumerate(('RO001B', 'RO001C', 'RO001D'), start=1):
        station.camera(code, n, idle_min=530)
    restart = Restarts()
    with caplog.at_level(logging.WARNING):
        assert _recover(station, restart) == ['RO001B', 'RO001C', 'RO001D']
    assert restart.calls == [('rms-cam1.service', False), ('rms-cam2.service', False),
                             ('rms-cam3.service', False)]
    assert 'No space left on device' in caplog.text
    # The next dawn tick, 15 minutes later, leaves them alone.
    restart2 = Restarts()
    assert _recover(station, restart2) == []
    assert restart2.calls == []
    assert flags_manager.peek('RO001B', NIGHT, station.cfg)[rms_hang.STATE_KEY]['unit'] == 'rms-cam1.service'


def test_an_rms_still_logging_is_left_alone(station):
    station.camera('RO001B', 1, idle_min=2, lines=('2026/10/17 06:58:00-INFO-DetectStarsAndMeteors - Running detection',))
    restart = Restarts()
    assert _recover(station, restart) == []
    assert restart.calls == []


def test_only_cameras_dawn_waits_on(station):
    station.camera('RO001B', 1, idle_min=530)
    restart = Restarts()
    assert _recover(station, restart, waiting=('RO001C',)) == []


def test_a_dead_rms_process_is_not_a_hang(station):
    # Nothing to restart: the unit's own Restart= or the operator handles a crash.
    station.camera('RO001B', 1, idle_min=530, running=False)
    assert _recover(station, Restarts()) == []


def test_a_still_full_disk_is_not_restarted_into(station, monkeypatch, caplog):
    station.camera('RO001B', 1, idle_min=530)
    monkeypatch.setattr(rms_hang.shutil, 'disk_usage', lambda p: type('U', (), {'free': 3.6e9})())
    restart = Restarts()
    with caplog.at_level(logging.WARNING):
        assert _recover(station, restart) == []
    assert restart.calls == [] and '3.6 GB free' in caplog.text
    assert not flags_manager.peek('RO001B', NIGHT, station.cfg).get(rms_hang.STATE_KEY)


def test_user_unit_is_restarted_in_the_user_scope(station):
    station.camera('RO001B', 1, idle_min=60,
                   cgroup='0::/user.slice/user-1000.slice/user@1000.service/app.slice/rms-capture.service\n')
    restart = Restarts()
    assert _recover(station, restart) == ['RO001B']
    assert restart.calls == [('rms-capture.service', True)]


def test_rms_started_by_hand_is_reported_not_restarted(station, caplog):
    station.camera('RO001B', 1, idle_min=60, cgroup='0::/user.slice/user-1000.slice/session-4.scope\n')
    restart = Restarts()
    with caplog.at_level(logging.WARNING):
        assert _recover(station, restart) == []
    assert restart.calls == [] and 'no systemd unit' in caplog.text


def test_failed_restart_is_logged_and_retried_next_tick(station, caplog):
    station.camera('RO001B', 1, idle_min=60)
    with caplog.at_level(logging.ERROR):
        assert _recover(station, Restarts(ok=False)) == []
    assert 'Access denied' in caplog.text
    assert _recover(station, Restarts()) == ['RO001B']


def test_report_mode_only_logs(station, caplog):
    station.camera('RO001B', 1, idle_min=60)
    station.cfg['services'] = {'rms_hang': {'mode': 'report'}}
    restart = Restarts()
    with caplog.at_level(logging.WARNING):
        assert _recover(station, restart) == []
    assert restart.calls == [] and 'would restart rms-cam1.service' in caplog.text


def test_disabled_does_nothing(station):
    station.camera('RO001B', 1, idle_min=60)
    station.cfg['services'] = {'rms_hang': {'enabled': False}}
    restart = Restarts()
    assert _recover(station, restart) == [] and restart.calls == []


def test_auto_reprocess_off_is_called_out(station, caplog):
    station.camera('RO001B', 1, idle_min=60, auto_reprocess=False)
    with caplog.at_level(logging.WARNING):
        assert _recover(station, Restarts()) == ['RO001B']
    assert 'auto_reprocess is off' in caplog.text


@pytest.mark.parametrize('text, unit, user', [
    ('0::/system.slice/rms-cam2.service\n', 'rms-cam2.service', False),
    ('0::/user.slice/user-1000.slice/user@1000.service/app.slice/rms-cam1.service\n', 'rms-cam1.service', True),
    ('0::/user.slice/user-1000.slice/user@1000.service/init.scope\n', None, False),
    ('0::/system.slice/cron.service\n', None, False),
    ('0::/system.slice/rms.service\n', 'rms.service', False),
])
def test_unit_of_reads_the_cgroup(tmp_path, text, unit, user):
    (tmp_path / '42').mkdir()
    (tmp_path / '42' / 'cgroup').write_text(text)
    assert rms_hang.unit_of(42, tmp_path) == (unit, user)


# ---------------------------------------------------------------------------
# A quiet log is a hang only when RMS also makes no CPU progress. Dawn now
# waits for RMS's post-processing too, whose quiet steps (timelapse prep took
# 47 min on gmnro09 (Sibiciu), bz2 archiving) keep a core busy without logging.
# ---------------------------------------------------------------------------

QUIET = ('2026/10/17 06:20:00-INFO-Reprocess-line:466 - Generating a timelapse...',)


def _stat(station: Station, pid: int, ticks: int, ppid: int = 1) -> None:
    fields = ['S', str(ppid)] + ['0'] * 9 + [str(ticks), '0', '0', '0'] + ['0'] * 30
    (station.proc / str(pid)).mkdir(exist_ok=True)
    (station.proc / str(pid) / 'stat').write_text(f'{pid} (python3 (rms)) ' + ' '.join(fields) + '\n')


def _at(station: Station, minutes: float, restart: Restarts) -> list:
    return rms_hang.recover(('RO001B',), NIGHT, station.cfg, now=NOW + timedelta(minutes=minutes),
                            restart=restart, proc=station.proc)


def test_quiet_but_busy_rms_is_not_restarted(station):
    station.camera('RO001B', 1, idle_min=40, lines=QUIET)
    pid = station._pid
    restart = Restarts()
    _stat(station, pid, 1000)
    assert _at(station, 0, restart) == []          # first sight: sample only
    _stat(station, pid, 1000 + 100 * 60 * 14)      # about one core for 15 min
    assert _at(station, 15, restart) == []
    assert restart.calls == []


def test_quiet_and_cpu_idle_rms_is_restarted_after_the_window(station):
    station.camera('RO001B', 1, idle_min=40, lines=QUIET)
    pid = station._pid
    _stat(station, pid, 5000)
    restart = Restarts()
    assert _at(station, 0, restart) == []
    _stat(station, pid, 5003)                      # idle, but the window is too short yet
    assert _at(station, 5, restart) == []
    assert _at(station, 15, restart) == ['RO001B']
    assert restart.calls == [('rms-cam1.service', False)]


def test_child_cpu_counts_as_progress(station):
    station.camera('RO001B', 1, idle_min=40, lines=QUIET)
    pid = station._pid
    _stat(station, pid, 5000)
    _stat(station, 9001, 0, ppid=pid)
    restart = Restarts()
    assert _at(station, 0, restart) == []
    _stat(station, 9001, 100 * 600, ppid=pid)       # the archiving worker
    assert _at(station, 15, restart) == []


def test_a_new_rms_pid_starts_a_new_window(station):
    station.camera('RO001B', 1, idle_min=40, lines=QUIET)
    pid = station._pid
    _stat(station, pid, 5000)
    old = {'pid': pid + 77, 'ticks': 5000, 'at': (NOW - timedelta(hours=1)).isoformat()}
    flags_manager.edit_state('RO001B', NIGHT, station.cfg,
                             lambda s: s.__setitem__(rms_hang.CPU_KEY, old))
    assert _at(station, 0, Restarts()) == []
    assert flags_manager.peek('RO001B', NIGHT, station.cfg)[rms_hang.CPU_KEY]['pid'] == pid


def test_hang_marker_restarts_even_while_cpu_moves(station):
    """After ENOSPC the capture process can keep spinning on frames."""
    station.camera('RO001B', 1, idle_min=530)
    _stat(station, station._pid, 10 ** 6)
    assert _at(station, 0, Restarts()) == ['RO001B']


COMPRESSOR_DIED = ('2026/10/16 21:02:11-ERROR-BufferedCapture-line:1612 - '
                   'The compressor has died, restarting the capture!')


def test_a_marker_in_the_last_line_is_still_a_hang(station):
    lines = tuple(f'2026/10/16 22:0{i % 10}:00-INFO-BufferedCapture-line:900 - block {i}'
                  for i in range(200)) + (ENOSPC,)
    station.camera('RO001B', 1, idle_min=530, lines=lines)
    _stat(station, station._pid, 10 ** 6)
    assert _at(station, 0, Restarts()) == ['RO001B']


def test_an_old_marker_followed_by_progress_is_not_a_hang(station):
    """RMS recovers from a dead compressor and keeps working: gmnro09
    (Sibiciu)'s 47-min timelapse step must not be restarted for a marker
    logged hours earlier."""
    lines = (COMPRESSOR_DIED,) + tuple(
        f'2026/10/17 0{i // 100}:{i % 60:02d}:00-INFO-Reprocess-line:466 - step {i}'
        for i in range(200)) + QUIET
    station.camera('RO001B', 1, idle_min=40, lines=lines)
    pid = station._pid
    restart = Restarts()
    _stat(station, pid, 1000)
    assert _at(station, 0, restart) == []          # first sight: a CPU sample only
    _stat(station, pid, 1000 + 100 * 60 * 14)      # about one core for 15 min
    assert _at(station, 15, restart) == []
    assert restart.calls == []


def test_process_tree_cpu_ticks(station):
    _stat(station, 10, 7)
    _stat(station, 11, 5, ppid=10)
    _stat(station, 12, 3, ppid=11)
    _stat(station, 13, 1000, ppid=1)
    assert rms_hang.process_tree_cpu_ticks(10, station.proc) == 15
    assert rms_hang.process_tree_cpu_ticks(99, station.proc) is None


# ---------------------------------------------------------------------------
# An RMS starved by a saturated disk is not hung. gmnro05 (Bistrița), dawn of
# 2026-11-16: five RMS post-processing on one HDD sat in D state, IO pressure
# "full" ~90%; RO0003's tree used 0.8% of a core while reading 3.3 MB/s, and
# dawn restarted RO0007, RO0004 and RO0005 one after another.
# ---------------------------------------------------------------------------

def _io(station: Station, pid: int, read: int, write: int = 0) -> None:
    (station.proc / str(pid)).mkdir(exist_ok=True)
    (station.proc / str(pid) / 'io').write_text(
        f'rchar: {read}\nwchar: {write}\nsyscr: 1\nsyscw: 1\n'
        f'read_bytes: {read}\nwrite_bytes: {write}\ncancelled_write_bytes: 0\n')


def test_rms_waiting_on_a_busy_disk_is_not_restarted(station, caplog):
    station.camera('RO0003', 1, idle_min=55, lines=QUIET)
    pid = station._pid
    _stat(station, pid, 5000)
    _io(station, pid, 10 ** 9)
    restart = Restarts()
    assert rms_hang.recover(('RO0003',), NIGHT, station.cfg, now=NOW, restart=restart,
                            proc=station.proc) == []
    _stat(station, pid, 5000 + 49 * 15)               # 0.8% of a core
    _io(station, pid, 10 ** 9 + 3_300_000 * 900)      # 3.3 MB/s for 15 min
    with caplog.at_level(logging.INFO):
        assert rms_hang.recover(('RO0003',), NIGHT, station.cfg, now=NOW + timedelta(minutes=15),
                                restart=restart, proc=station.proc) == []
    assert restart.calls == [] and 'waiting on the disk' in caplog.text


def test_cpu_and_io_idle_rms_is_still_restarted(station):
    """Stuck in D state on a dead disk: no bytes move."""
    station.camera('RO001B', 1, idle_min=40, lines=QUIET)
    pid = station._pid
    _stat(station, pid, 5000)
    _io(station, pid, 10 ** 9)
    restart = Restarts()
    assert _at(station, 0, restart) == []
    _io(station, pid, 10 ** 9 + 4096)                 # a few pages in 15 min
    assert _at(station, 15, restart) == ['RO001B']


def test_io_of_a_new_worker_counts_and_a_reaped_one_does_not_go_negative(station):
    station.camera('RO001B', 1, idle_min=40, lines=QUIET)
    pid = station._pid
    _stat(station, pid, 5000)
    _io(station, pid, 100)
    _stat(station, 9001, 0, ppid=pid)
    _io(station, 9001, 5 * 10 ** 9)                   # a worker that will exit
    restart = Restarts()
    assert _at(station, 0, restart) == []
    (station.proc / '9001' / 'stat').unlink()         # reaped
    _stat(station, 9002, 0, ppid=pid)
    _io(station, 9002, 50 * 10 ** 6)                  # a new worker reading
    assert _at(station, 15, restart) == []


def test_without_io_readings_cpu_decides_alone(station):
    station.camera('RO001B', 1, idle_min=40, lines=QUIET)
    _stat(station, station._pid, 5000)
    restart = Restarts()
    assert _at(station, 0, restart) == []
    assert _at(station, 15, restart) == ['RO001B']


def test_process_tree_io_bytes(station):
    _stat(station, 10, 7)
    _stat(station, 11, 5, ppid=10)
    _stat(station, 12, 3, ppid=1)
    _io(station, 10, 100, 1)
    _io(station, 11, 20)
    _io(station, 12, 999)
    assert rms_hang.process_tree_io_bytes(10, station.proc) == {10: 101, 11: 20}
    assert rms_hang.process_tree_io_bytes(99, station.proc) is None
    assert rms_hang.io_progress({'10': 50, 'x': 'y'}, {10: 101, 11: 20}) == 71
