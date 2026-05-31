import threading
import time
from pathlib import Path

import pytest

from waggle.locks import ProcessLock


def test_context_manager(tmp_path: Path):
    """Verify ProcessLock works as a context manager."""
    lock_path = tmp_path / "test.lock"
    lock = ProcessLock(lock_path)

    with lock:
        assert lock._fd is not None
        assert lock_path.exists()

    assert lock._fd is None
    assert lock_path.exists()


def test_reentrant_guard(tmp_path: Path):
    """Verify acquiring the same lock twice raises RuntimeError."""
    lock_path = tmp_path / "test.lock"
    lock = ProcessLock(lock_path)
    lock.acquire()
    try:
        with pytest.raises(RuntimeError):
            lock.acquire()
    finally:
        lock.release()


def test_double_release(tmp_path: Path):
    """Verify release() is idempotent."""
    lock_path = tmp_path / "test.lock"
    lock = ProcessLock(lock_path)
    lock.acquire()
    lock.release()
    # Should not raise an error
    lock.release()


def test_lock_file_creation(tmp_path: Path):
    """Verify ProcessLock creates the lock file and its parent directory."""
    lock_dir = tmp_path / "non_existent_dir"
    lock_path = lock_dir / "test.lock"

    assert not lock_dir.exists()

    with ProcessLock(lock_path):
        assert lock_dir.exists()
        assert lock_path.exists()


def test_cross_thread_exclusion(tmp_path: Path):
    """Verify the lock prevents concurrent access from different threads."""
    lock_path = tmp_path / "test.lock"
    lock1 = ProcessLock(lock_path)
    lock2 = ProcessLock(lock_path)
    event = threading.Event()
    second_thread_acquired = False

    def thread_one_task():
        with lock1:
            event.set()  # Signal that thread 1 has the lock
            time.sleep(0.2)

    def thread_two_task():
        nonlocal second_thread_acquired
        # This should block until thread_one_task releases the lock
        with lock2:
            second_thread_acquired = True

    t1 = threading.Thread(target=thread_one_task)
    t1.start()

    # Wait for thread 1 to acquire the lock
    event.wait(timeout=1)
    assert event.is_set()

    t2 = threading.Thread(target=thread_two_task)
    t2.start()

    # Give thread 2 a moment to try to acquire the lock
    t2.join(timeout=0.1)

    # Thread 2 should be blocked, so it shouldn't have acquired the lock yet
    assert not second_thread_acquired
    assert t2.is_alive()

    # Wait for both threads to complete
    t1.join(timeout=2)
    t2.join(timeout=2)

    # After t1 finishes, t2 should have been able to acquire the lock
    assert second_thread_acquired
    assert not t1.is_alive()
    assert not t2.is_alive()
