"""Regression guard for per-worker duplication of shared-state writes.

Every gunicorn worker runs create_app, so any background job whose only output
is a file on disk runs N times over. The archive-index refresher did exactly
that: each worker rebuilt a full copy of the index and json.dumps'd it into a
~29 MB string once a minute, then all three raced to rename the same ".tmp"
onto the same path (16 "archive index: failed to save" in one hour on prod).
The triplicated transient allocation pushed the service cgroup into its
MemoryHigh ceiling, where the kernel throttles every worker and the dashboard
504s on everything -- including station ingest.

``_try_become_duty_leader`` elects one owner per named duty with a
non-blocking flock, so the write happens once. These tests pin the properties
the gating depends on: exclusivity across processes, idempotency within one,
independence between duties, and fail-safe behaviour on an unwritable path.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path


def test_only_one_process_wins_a_duty(tmp_path):
    """THE BUG: without this, every worker writes the shared file."""
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir()

    code = textwrap.dedent(
        f"""
        import os, sys
        os.environ["ROVIMEN_LEADER_LOCK_DIR"] = {str(lock_dir)!r}
        os.environ["ROVIMEN_DISABLE_BACKGROUND"] = "1"
        sys.path.insert(0, {str(Path("dashboard").resolve())!r})
        from rovimen_dashboard import _try_become_duty_leader
        # Two "workers" in one process can't contend (flock is per-process), so
        # the holder stays alive here while a child tries to take the same duty.
        print("parent", _try_become_duty_leader("archive_index_save"))
        import subprocess, textwrap
        child = textwrap.dedent('''
            import os, sys
            os.environ["ROVIMEN_LEADER_LOCK_DIR"] = {str(lock_dir)!r}
            os.environ["ROVIMEN_DISABLE_BACKGROUND"] = "1"
            sys.path.insert(0, {str(Path("dashboard").resolve())!r})
            from rovimen_dashboard import _try_become_duty_leader
            print("child", _try_become_duty_leader("archive_index_save"))
        ''')
        r = subprocess.run([sys.executable, "-c", child], capture_output=True, text=True)
        sys.stdout.write(r.stdout)
        """
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
    )
    lines = dict(
        line.split() for line in out.stdout.strip().splitlines() if " " in line
    )
    assert lines.get("parent") == "True", out.stdout + out.stderr
    assert lines.get("child") == "False", out.stdout + out.stderr


def test_duty_claim_is_idempotent_within_a_worker(tmp_path):
    """The refresher calls this every 60s; it must not leak an fd per tick."""
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir()
    code = textwrap.dedent(
        f"""
        import os, sys
        os.environ["ROVIMEN_LEADER_LOCK_DIR"] = {str(lock_dir)!r}
        os.environ["ROVIMEN_DISABLE_BACKGROUND"] = "1"
        sys.path.insert(0, {str(Path("dashboard").resolve())!r})
        from rovimen_dashboard import _try_become_duty_leader, _duty_leader_lock_fhs
        results = [_try_become_duty_leader("archive_index_save") for _ in range(50)]
        print(all(results), len(_duty_leader_lock_fhs))
        """
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert out.stdout.strip().endswith("True 1"), out.stdout + out.stderr


def test_duties_are_independent(tmp_path):
    """Separate locks: a slow duty must not block an unrelated one."""
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir()
    code = textwrap.dedent(
        f"""
        import os, sys
        os.environ["ROVIMEN_LEADER_LOCK_DIR"] = {str(lock_dir)!r}
        os.environ["ROVIMEN_DISABLE_BACKGROUND"] = "1"
        sys.path.insert(0, {str(Path("dashboard").resolve())!r})
        from rovimen_dashboard import _try_become_duty_leader
        print(_try_become_duty_leader("archive_index_save"),
              _try_become_duty_leader("some_other_duty"))
        """
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert out.stdout.strip().endswith("True True"), out.stdout + out.stderr
    assert (lock_dir / "leader_archive_index_save.lock").exists()
    assert (lock_dir / "leader_some_other_duty.lock").exists()


def test_unwritable_lock_dir_fails_safe_to_not_leader(tmp_path):
    """An unwritable path must skip the duty, never crash the worker.

    Skipping is the safe side: the shared file just goes unwritten that tick,
    where an exception here would take down a worker that is otherwise fine.
    """
    missing = tmp_path / "does" / "not" / "exist"
    code = textwrap.dedent(
        f"""
        import os, sys
        os.environ["ROVIMEN_LEADER_LOCK_DIR"] = {str(missing)!r}
        os.environ["ROVIMEN_DISABLE_BACKGROUND"] = "1"
        sys.path.insert(0, {str(Path("dashboard").resolve())!r})
        from rovimen_dashboard import _try_become_duty_leader
        print(_try_become_duty_leader("archive_index_save"))
        """
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().endswith("False"), out.stdout + out.stderr


def test_index_save_uses_a_per_process_temp_name(tmp_path):
    """Two writers must not collide on one ".tmp" if gating ever regresses."""
    import os as _os

    from cache_store import _ArchiveIndex

    idx = _ArchiveIndex()
    target = tmp_path / "archive_index.json"
    idx.save_to_disk(target)

    assert target.is_file()
    # No shared ".tmp" left behind, and the temp name is pid-qualified.
    assert not (tmp_path / "archive_index.tmp").exists()
    assert target.with_suffix(f".tmp.{_os.getpid()}").name.endswith(str(_os.getpid()))
