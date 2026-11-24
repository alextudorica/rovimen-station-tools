"""Regression guard: 0-byte thumbnail cache files must be treated as misses.

A prefetch copy that hits an EOF-empty SSHFS read (stalled storage-box mount)
or a 0-byte source could persist an empty ``.webp`` in the local thumb cache.
``api_archive_file`` serves any cache file that ``exists()`` with a 30-day
immutable Cache-Control (via nginx X-Accel), so an empty file became a
permanently black, un-refetchable thumbnail. Both the serve and the cache-write
now gate on ``_is_nonempty_file``.
"""

from __future__ import annotations


def test_is_nonempty_file(tmp_path):
    from cache_store import _is_nonempty_file

    missing = tmp_path / "missing.webp"
    assert _is_nonempty_file(missing) is False

    empty = tmp_path / "empty.webp"
    empty.write_bytes(b"")
    assert _is_nonempty_file(empty) is False

    good = tmp_path / "good.webp"
    good.write_bytes(b"RIFF....WEBP")
    assert _is_nonempty_file(good) is True

    # A directory is not a servable file.
    d = tmp_path / "adir"
    d.mkdir()
    assert _is_nonempty_file(d) is False
