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

    # Test release on a never-acquired lock
    lock.release()  # Should not raise

    # Test double release on an acquired lock
    lock.acquire()
    lock.release()
    lock.release()  # Should not raise


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

    # Event to signal that thread 1 has acquired the lock
    t1_acquired_lock = threading.Event()
    # Event to signal that thread 2 is about to try acquiring the lock
    t2_about_to_acquire = threading.Event()

    second_thread_acquired = False

    def thread_one_task():
        with lock1:
            t1_acquired_lock.set()
            # Keep the lock for a bit to ensure thread 2 has to wait
            time.sleep(0.2)

    def thread_two_task():
        nonlocal second_thread_acquired
        # Signal that we are about to attempt to acquire the lock
        t2_about_to_acquire.set()
        with lock2:
            second_thread_acquired = True

    t1 = threading.Thread(target=thread_one_task)
    t2 = threading.Thread(target=thread_two_task)

    t1.start()

    # Wait until thread 1 has the lock
    t1_acquired_lock.wait(timeout=1)
    assert t1_acquired_lock.is_set(), "Thread 1 failed to acquire the lock in time"

    t2.start()

    # Wait until thread 2 is at the point of acquiring the lock
    t2_about_to_acquire.wait(timeout=1)
    assert t2_about_to_acquire.is_set(), "Thread 2 failed to signal before acquiring lock"

    # Give thread 2 a moment to get blocked
    time.sleep(0.01)

    # Thread 2 should be blocked, so it shouldn't have acquired the lock yet
    assert not second_thread_acquired, "Thread 2 acquired the lock while Thread 1 held it"
    assert t2.is_alive(), "Thread 2 did not block and finished prematurely"

    # Wait for both threads to complete
    t1.join(timeout=2)
    assert not t1.is_alive(), "Thread 1 did not finish in time"

    t2.join(timeout=2)
    assert not t2.is_alive(), "Thread 2 did not finish in time"

    # After t1 finishes, t2 should have been able to acquire the lock
    assert second_thread_acquired, "Thread 2 failed to acquire the lock after Thread 1 released it"
