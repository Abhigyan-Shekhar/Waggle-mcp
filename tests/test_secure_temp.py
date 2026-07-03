from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from waggle import secure_temp
from waggle.secure_temp import secure_temp_path, write_secure_temp


def test_write_secure_temp_writes_exact_bytes():
    data = b"abhi-payload-\x00\x01\x02"
    path = write_secure_temp(data, suffix=".abhi")
    try:
        assert path.exists()
        assert path.suffix == ".abhi"
        assert path.read_bytes() == data
    finally:
        path.unlink(missing_ok=True)


def test_write_secure_temp_handles_large_payload_without_truncation():
    # os.write can do partial writes; the helper loops over a memoryview, so a
    # payload larger than a single write must still land in full.
    data = os.urandom(5 * 1024 * 1024)
    path = write_secure_temp(data)
    try:
        assert path.read_bytes() == data
    finally:
        path.unlink(missing_ok=True)


def test_write_secure_temp_cleans_up_on_write_failure(monkeypatch):
    # Acceptance criterion (#64): the temp file must not be left behind if the
    # write raises. Force os.write to fail after the file has been created.
    created: list[str] = []
    real_mkstemp = secure_temp.tempfile.mkstemp

    def tracking_mkstemp(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        created.append(name)
        return fd, name

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(secure_temp.tempfile, "mkstemp", tracking_mkstemp)
    monkeypatch.setattr(secure_temp.os, "write", boom)

    with pytest.raises(OSError, match="disk full"):
        write_secure_temp(b"never lands", suffix=".json")

    assert created, "mkstemp should have been called"
    assert not Path(created[0]).exists(), "partial temp file must be removed on failure"


def test_write_secure_temp_does_not_follow_a_preexisting_symlink():
    # The original bug: the path was opened a second time by name, so a symlink
    # planted at that name redirected the write. mkstemp creates a fresh unique
    # name with O_EXCL and we only ever write through that fd, so a write can
    # never land on an attacker-chosen target.
    path = write_secure_temp(b"safe", suffix=".abhi")
    try:
        assert not path.is_symlink()
        assert path.read_bytes() == b"safe"
    finally:
        path.unlink(missing_ok=True)


def test_secure_temp_path_directory_is_private_and_cleaned_up():
    captured: Path | None = None
    with secure_temp_path(suffix=".json") as temp_path:
        captured = temp_path
        parent = temp_path.parent
        if os.name != "nt":
            # POSIX: owner-only directory (0o700) so no other user can plant a
            # symlink alongside the file a downstream exporter writes by path.
            # Windows reports 0o777 in st_mode regardless of the mkdtemp mode;
            # there isolation comes from the per-user temp root and directory
            # ACLs rather than POSIX permission bits, so the check is POSIX-only.
            mode = stat.S_IMODE(parent.stat().st_mode)
            assert mode == 0o700
        temp_path.write_bytes(b"exported")
        assert temp_path.read_bytes() == b"exported"
    assert captured is not None
    assert not captured.exists()
    assert not captured.parent.exists()


def test_secure_temp_path_cleans_up_on_exception():
    leaked: Path | None = None
    with pytest.raises(RuntimeError, match="boom"), secure_temp_path() as temp_path:
        leaked = temp_path.parent
        temp_path.write_text("partial")
        raise RuntimeError("boom")
    assert leaked is not None
    assert not leaked.exists()
