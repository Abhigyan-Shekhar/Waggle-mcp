"""Tests for _ReadWriteLock - resolves issue #67.

File: tests/test_rw_lock.py

Run with:
    pytest tests/test_rw_lock.py -v

These tests verify:
1. Basic write-lock exclusivity across threads
2. Concurrent readers are allowed
3. Writer → reader re-entrance is allowed (writer shortcut)
4. Writer → writer re-entrance is allowed (recursive write)
5. Reader → reader re-entrance is allowed (per-thread depth)
6. [ISSUE #67] Reader → writer on same thread raises RuntimeError immediately
7. No thread hangs after a refused upgrade (core acceptance criterion)
8. Lock state is clean after a refused upgrade
"""

from __future__ import annotations

import threading
import time

import pytest

from waggle.graph import _ReadWriteLock


def _start(fn, *, daemon: bool = False) -> threading.Thread:
    """Start *fn* in a thread and return it (exceptions are re-raised on join)."""
    exc_box: list[BaseException] = []

    def wrapper() -> None:
        try:
            fn()
        except Exception as exc:
            exc_box.append(exc)

    t = threading.Thread(target=wrapper, daemon=daemon)
    t._exc_box = exc_box
    t.start()
    return t


def _join(t: threading.Thread, timeout: float = 2) -> None:
    """Join *t* and re-raise any exception it captured."""
    t.join(timeout=timeout)
    assert not t.is_alive(), f"Thread {t.name} is still alive after {timeout}s"
    exc_box = getattr(t, "_exc_box", [])
    if exc_box:
        raise exc_box[0]


class TestWriteLock:
    def test_exclusive_between_threads(self):
        """Two threads must not hold the write lock at the same time."""
        lock = _ReadWriteLock()
        log: list[str] = []

        def worker(label: str, hold: float) -> None:
            with lock:
                log.append(f"{label}_in")
                time.sleep(hold)
                log.append(f"{label}_out")

        a_inside = threading.Event()

        def worker_a(label: str, hold: float) -> None:
            with lock:
                a_inside.set()
                log.append(f"{label}_in")
                time.sleep(hold)
                log.append(f"{label}_out")

        t1 = _start(lambda: worker_a("A", 0.05))
        assert a_inside.wait(timeout=2), "thread_a never entered the lock"
        t2 = _start(lambda: worker("B", 0.05))
        _join(t1)
        _join(t2)

        assert log == ["A_in", "A_out", "B_in", "B_out"]

    def test_recursive_write_same_thread(self):
        """Same thread may re-enter the write lock."""
        lock = _ReadWriteLock()
        with lock, lock, lock:
            pass

    def test_writer_unblocks_waiter(self):
        """Once the writer releases, a waiting writer may proceed."""
        lock = _ReadWriteLock()
        acquired = threading.Event()

        lock._acquire_write()

        def waiter() -> None:
            with lock:
                acquired.set()

        t = _start(waiter)
        time.sleep(0.03)
        assert not acquired.is_set(), "Waiter should still be blocked"
        lock._release_write()
        assert acquired.wait(timeout=2), "Waiter never acquired the lock"
        _join(t)

    def test_release_by_non_owner_raises(self):
        """Releasing a write lock you don't own must raise RuntimeError."""
        lock = _ReadWriteLock()
        lock._acquire_write()
        error: list[Exception] = []

        def bad_release() -> None:
            try:
                lock._release_write()
            except RuntimeError as exc:
                error.append(exc)

        t = _start(bad_release)
        _join(t)
        assert len(error) == 1
        lock._release_write()  # clean up


class TestReadLock:
    def test_concurrent_readers_allowed(self):
        """Multiple threads may hold the read lock at the same time."""
        lock = _ReadWriteLock()
        barrier = threading.Barrier(4)
        inside: list[int] = []

        def reader() -> None:
            with lock.read():
                inside.append(1)
                barrier.wait(timeout=3)
                inside.append(-1)

        threads = [_start(reader) for _ in range(4)]
        for t in threads:
            _join(t, timeout=3)

        assert sum(inside) == 0

    def test_reader_blocked_by_writer(self):
        """A reader must wait while a writer holds the lock."""
        lock = _ReadWriteLock()
        read_acquired = threading.Event()

        lock._acquire_write()

        def reader() -> None:
            with lock.read():
                read_acquired.set()

        t = _start(reader)
        time.sleep(0.03)
        assert not read_acquired.is_set(), "Reader should be blocked by writer"
        lock._release_write()
        assert read_acquired.wait(timeout=2), "Reader never unblocked"
        _join(t)

    def test_writer_blocked_by_readers(self):
        """A writer must wait until all active readers have released."""
        lock = _ReadWriteLock()
        write_acquired = threading.Event()
        release_readers = threading.Event()

        def reader() -> None:
            with lock.read():
                release_readers.wait(timeout=3)

        readers = [_start(reader) for _ in range(3)]
        time.sleep(0.02)

        def writer() -> None:
            with lock:
                write_acquired.set()

        wt = _start(writer)
        time.sleep(0.03)
        assert not write_acquired.is_set(), "Writer should be blocked by readers"
        release_readers.set()
        assert write_acquired.wait(timeout=2), "Writer never unblocked"
        for t in readers:
            _join(t)
        _join(wt)

    def test_reentrant_reads_same_thread(self):
        """A thread may acquire the read lock multiple times."""
        lock = _ReadWriteLock()
        with lock.read(), lock.read():
            pass

    def test_release_by_non_holder_raises(self):
        """Releasing a read lock not held by this thread must raise RuntimeError."""
        lock = _ReadWriteLock()
        error: list[Exception] = []

        def bad_release() -> None:
            try:
                lock._release_read()
            except RuntimeError as exc:
                error.append(exc)

        t = _start(bad_release)
        _join(t)
        assert len(error) == 1


class TestReentrant:
    def test_writer_can_enter_read_context(self):
        """A thread holding the write lock may enter a read context freely."""
        lock = _ReadWriteLock()
        with lock, lock.read():  # writer shortcut — must not block
            pass

    def test_recursive_write_inside_read_from_writer(self):
        """Deep nesting: write → write → read → write."""
        lock = _ReadWriteLock()
        with lock, lock, lock.read(), lock:
            pass


class TestIssue67:
    """
    Acceptance criteria from issue #67:

    1. A test that attempts the read-then-write pattern raises RuntimeError
       immediately — it does NOT hang.
    2. No thread hangs forever.
    3. The behaviour is documented (see _ReadWriteLock docstring).
    """

    def test_read_then_write_raises_not_deadlocks(self):
        """
        Core regression test.
        Thread inside lock.read() that calls `with lock:` must get a
        RuntimeError immediately — never hang silently.
        """
        lock = _ReadWriteLock()
        outcome: list[str] = []

        def workload() -> None:
            with lock.read():
                try:
                    with lock:
                        outcome.append("deadlocked")
                except RuntimeError as exc:
                    outcome.append(f"raised:{exc}")

        t = _start(workload)
        _join(t)

        assert len(outcome) == 1
        assert outcome[0].startswith("raised:"), f"Expected RuntimeError, got: {outcome[0]!r}"

    def test_no_thread_hangs(self):
        """Variant: Event-based assertion that the thread finishes within 2 s."""
        lock = _ReadWriteLock()
        done = threading.Event()

        def workload() -> None:
            with lock.read():
                try:
                    with lock:
                        pass
                except RuntimeError:
                    pass
            done.set()

        t = _start(workload)
        assert done.wait(timeout=2), "Thread hung — deadlock still present"
        _join(t)

    def test_error_message_mentions_upgrade(self):
        """RuntimeError message must say 'upgrade' for debuggability."""
        lock = _ReadWriteLock()
        with lock.read(), pytest.raises(RuntimeError, match="upgrade"), lock:
            pass

    def test_lock_fully_usable_after_refused_upgrade(self):
        """
        After a refused upgrade the lock must not be in a broken state.
        Other threads must be able to acquire the write lock normally.
        """
        lock = _ReadWriteLock()

        def bad_thread() -> None:
            with lock.read():
                try:
                    with lock:
                        pass
                except RuntimeError:
                    pass

        t = _start(bad_thread)
        _join(t)

        result: list[str] = []

        def good_writer() -> None:
            with lock:
                result.append("ok")

        t2 = _start(good_writer)
        _join(t2)
        assert result == ["ok"]

    def test_read_depth_cleaned_up_after_normal_exit(self):
        """
        Per-thread read depth must be zero after a normal read context exit
        so a subsequent write on the same thread succeeds.
        """
        lock = _ReadWriteLock()
        with lock.read():
            pass
        with lock:
            pass

    def test_nested_read_depth_cleaned_up(self):
        """
        Nested reads (depth > 1) must all be released before a write
        is permitted; intermediate releases must not cause false upgrade errors.
        """
        lock = _ReadWriteLock()
        with lock.read(), lock.read():
            pass
        with lock:
            pass

    def test_multiple_threads_see_independent_depth(self):
        """
        Refused upgrade in thread A must not affect thread B's ability to
        acquire a plain read lock.
        """
        lock = _ReadWriteLock()
        b_read_ok = threading.Event()
        a_inside = threading.Event()

        def thread_a() -> None:
            with lock.read():
                a_inside.set()
                try:
                    with lock:
                        pass
                except RuntimeError:
                    pass

        def thread_b() -> None:
            with lock.read():
                b_read_ok.set()

        ta = _start(thread_a)
        assert a_inside.wait(timeout=2), "Thread A did not enter its read lock"

        tb = _start(thread_b)
        assert b_read_ok.wait(timeout=2), "Thread B could not acquire read lock"

        _join(ta)
        _join(tb)

    def test_reentrant_read_bypasses_waiting_writer(self):
        """
        A thread holding a read lock must be able to re-enter a read lock (nested read)
        even if a writer thread is waiting in the queue, preventing self-deadlock.
        """
        lock = _ReadWriteLock()
        a_has_first_read = threading.Event()
        b_waiting_for_write = threading.Event()
        a_nested_read_ok = threading.Event()

        def thread_a() -> None:
            with lock.read():
                a_has_first_read.set()
                assert b_waiting_for_write.wait(timeout=2)

                time.sleep(0.02)

                with lock.read():
                    a_nested_read_ok.set()

        def thread_b() -> None:
            b_waiting_for_write.set()
            with lock:
                pass

        ta = _start(thread_a)
        assert a_has_first_read.wait(timeout=2), "Thread A failed to acquire initial read"

        tb = _start(thread_b)

        assert a_nested_read_ok.wait(timeout=2), (
            "Thread A self-deadlocked on re-entrant read because a writer was waiting!"
        )

        _join(ta)
        _join(tb)
