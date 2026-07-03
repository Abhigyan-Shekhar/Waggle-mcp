"""Temp-file helpers that avoid a write-by-path TOCTOU window.

See issue #64. The previous pattern opened a ``NamedTemporaryFile(delete=False)``
inside a ``with`` block, let the block close the handle, captured the bare path,
and then re-opened that path by name with ``write_text`` / ``write_bytes``.
Between the close and the re-open, the path exists on disk as an empty file whose
name the process knows. On a shared ``/tmp`` an attacker who can guess or observe
the name can swap it for a symlink, and the subsequent write follows the symlink
into an attacker-chosen target.

Two safe primitives are provided:

* :func:`write_secure_temp` — when the bytes are already in hand. It writes
  through the file descriptor returned by :func:`tempfile.mkstemp` and never
  re-opens the path by name, which closes the TOCTOU window entirely.
* :func:`secure_temp_path` — for the case where a downstream callee insists on
  writing to a path itself (e.g. an exporter that takes ``output_path``). It
  hands back a path inside a freshly created private directory
  (:func:`tempfile.mkdtemp`, mode ``0o700``), so no other user can plant a
  symlink alongside it, and removes the whole directory on exit.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def write_secure_temp(data: bytes, *, suffix: str = "") -> Path:
    """Create a temp file and write ``data`` through the mkstemp descriptor.

    The descriptor returned by :func:`tempfile.mkstemp` is the only handle ever
    used to write the file; the path is never opened a second time by name, so
    there is no window in which the on-disk name can be swapped for a symlink.
    The caller owns the returned path and is responsible for unlinking it. On
    any write error the descriptor is closed and the partially created file is
    removed before re-raising. (The close must precede the unlink: Windows
    refuses to delete a file that still has an open handle.)
    """
    fd, name = tempfile.mkstemp(suffix=suffix)
    path = Path(name)
    try:
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
        finally:
            os.close(fd)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


@contextmanager
def secure_temp_path(*, suffix: str = "", prefix: str = "waggle-") -> Iterator[Path]:
    """Yield a temp path inside a private 0o700 directory, cleaned up on exit.

    Use this when a downstream function must perform the write itself by path.
    Because the parent directory is created by :func:`tempfile.mkdtemp` with
    owner-only permissions, no other user can create a symlink at the target
    name, so the by-path write cannot be redirected. The directory and its
    contents are removed when the context exits, even on exception.
    """
    directory = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        yield directory / f"data{suffix}"
    finally:
        shutil.rmtree(directory, ignore_errors=True)
