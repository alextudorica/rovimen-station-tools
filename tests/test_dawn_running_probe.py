"""rovimen_night.dawn_running: reads /proc/locks, never takes the dawn lock."""

from __future__ import annotations

import fcntl
import os
import sys

import pytest

import rovimen_night

linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"),
                                reason="/proc/locks is Linux-only")


@linux_only
def test_held_flock_is_seen(tmp_path):
    lock = tmp_path / "dawn.lock"
    with open(lock, "w") as fd:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert rovimen_night.dawn_running(lock) is True
    assert rovimen_night.dawn_running(lock) is False


@linux_only
def test_probe_does_not_take_the_lock(tmp_path):
    """A dawn run starting while the probe runs must still get its lock."""
    lock = tmp_path / "dawn.lock"
    lock.write_text("")
    rovimen_night.dawn_running(lock)
    with open(lock, "w") as fd:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_missing_lock_file_means_not_running(tmp_path):
    assert rovimen_night.dawn_running(tmp_path / "absent.lock") is False


def test_matches_device_and_inode_from_proc_locks(tmp_path):
    lock = tmp_path / "dawn.lock"
    lock.write_text("")
    st = lock.stat()
    key = "%02x:%02x:%d" % (os.major(st.st_dev), os.minor(st.st_dev), st.st_ino)
    other = "%02x:%02x:%d" % (os.major(st.st_dev), os.minor(st.st_dev), st.st_ino + 1)
    locks = tmp_path / "locks"
    locks.write_text(f"1: FLOCK  ADVISORY  WRITE 4242 {other} 0 EOF\n")
    assert rovimen_night.dawn_running(lock, locks) is False
    locks.write_text(f"1: FLOCK  ADVISORY  WRITE 4242 {other} 0 EOF\n"
                     f"2: FLOCK  ADVISORY  WRITE 314567 {key} 0 EOF\n")
    assert rovimen_night.dawn_running(lock, locks) is True
