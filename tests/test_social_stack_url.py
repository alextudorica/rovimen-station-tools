"""The Social report's highlight image must point at a served archive file.

It used to build /api/archive/<cam>/<ymd>/<subdir>/<name> (no such route) from
the detection time, which also missed the FF-timestamped and time-range stack
names the archive actually holds, so the image never loaded.
"""

from __future__ import annotations

from routes import social


def _stacks(tmp_path, cam, ymd, subdir, names):
    d = tmp_path / cam / ymd / subdir
    d.mkdir(parents=True)
    for n in names:
        (d / n).write_bytes(b"webp")


def test_url_uses_the_archive_file_route_and_the_real_file_name(tmp_path, monkeypatch):
    monkeypatch.setattr(social, "ARCHIVE_PATH", tmp_path)
    # FF timestamp 5 s before the detection, as a time-range stack.
    _stacks(tmp_path, "RO000W", "20260920", "meteors",
            ["RO000W_20260920_213205_213225_stack.webp", "RO000W_20260920_220000_stack.webp"])
    url = social._stack_url("RO000W", "20260920", "2026-09-20T21:32:10")
    assert url == "/api/archive/file/RO000W/20260920/meteors/RO000W_20260920_213205_213225_stack.webp"


def test_no_stack_within_the_window_gives_no_url(tmp_path, monkeypatch):
    monkeypatch.setattr(social, "ARCHIVE_PATH", tmp_path)
    _stacks(tmp_path, "RO000W", "20260920", "stacks", ["RO000W_20260920_220000_stack.webp"])
    assert social._stack_url("RO000W", "20260920", "2026-09-20T21:32:10") is None
    assert social._stack_url("RO000W", "20260920", None) is None
