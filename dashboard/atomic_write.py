"""Atomic file writes: a temp file beside the target, then ``os.replace``.

Every dashboard store, cache and seed file is written this way, so a reader
(another gunicorn worker, nginx, the next restart) sees the old file or the
new one, never a partial one. Callers keep their own temp names: several are
per process or thread so concurrent writers cannot interleave, and a few
readers skip names by pattern (``network_stats`` ignores ``*.tmp`` seeds).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def write_atomic(
    path: Path,
    data: bytes | str,
    *,
    tmp: Path | None = None,
    encoding: str | None = None,
    chmod: int | None = None,
    mkdir: bool = False,
) -> None:
    """Write ``data`` to ``path`` through a temp file and ``os.replace``.

    ``tmp`` None: a private ``mkstemp`` file beside ``path``
    (``.<name>.XXXXXXXX``), so the new file is mode 0600. ``tmp`` given: that
    path, created like ``Path.write_bytes`` (mode from the process umask).
    ``str`` data is written in text mode with ``encoding`` (None: the locale
    default, as ``Path.write_text``). ``chmod`` sets the temp's mode before the
    rename, best effort. ``mkdir`` creates the parent directory first. On any
    failure the temp is removed and the error re-raised.
    """
    if mkdir:
        path.parent.mkdir(parents=True, exist_ok=True)
    mode = "w" if isinstance(data, str) else "wb"
    enc = encoding if isinstance(data, str) else None
    fd: int | None = None
    if tmp is None:
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        tmp = Path(name)
    try:
        with open(tmp if fd is None else fd, mode, encoding=enc) as fh:
            fh.write(data)
        if chmod is not None:
            try:
                os.chmod(tmp, chmod)
            except OSError:
                pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
