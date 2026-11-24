"""The chunk-ready watcher must watch a new camera's date dir.

A new camera's ffmpeg creates ``<CAM>/<YYYYMMDD>`` in one go, so by the time
the watcher sees IN_CREATE for ``<CAM>`` the date dir already exists and its
own IN_CREATE is gone. Watching only ``<CAM>`` left RO22XC on gmnro22
(Ferigile) unstacked for a whole evening on 2026-10-18.
"""

from __future__ import annotations

import logging
from pathlib import Path

from color_capture import watch_tree


class _FakeInotify:
    def __init__(self) -> None:
        self.watched: list[str] = []

    def add_watch(self, path: str) -> None:
        self.watched.append(path)


def test_new_camera_dir_watches_its_existing_date_dir(tmp_path: Path) -> None:
    cam = tmp_path / "RO22XC"
    (cam / "20261018" / "stacks").mkdir(parents=True)
    inot = _FakeInotify()

    n = watch_tree(inot, str(cam), logging.getLogger("t"))

    assert n == 3
    assert str(cam / "20261018") in inot.watched


def test_unwatchable_dir_is_skipped(tmp_path: Path) -> None:
    (tmp_path / "a" / "b").mkdir(parents=True)

    class _Picky(_FakeInotify):
        def add_watch(self, path: str) -> None:
            if path.endswith("a"):
                raise OSError("EIO")
            super().add_watch(path)

    inot = _Picky()
    assert watch_tree(inot, str(tmp_path), logging.getLogger("t")) == 2
    assert str(tmp_path / "a" / "b") in inot.watched
