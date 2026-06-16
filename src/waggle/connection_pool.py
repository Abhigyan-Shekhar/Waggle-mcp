"""A small, thread-safe pool of pre-configured SQLite connections.

Every graph operation in :mod:`waggle.graph` used to call
``MemoryGraph._connect()``, which opened a brand-new :class:`sqlite3.Connection`,
set ``row_factory``, and executed seven ``PRAGMA`` statements (WAL,
``synchronous``, ``busy_timeout``, ``foreign_keys``, ``mmap_size``,
``temp_store``, ``cache_size``).  With more than 80 call sites, that meant a
fresh connection and a fresh round of ``PRAGMA`` execution on every read and
write.

Under WAL mode SQLite supports many concurrent readers plus a single writer, so
connections can safely be reused.  :class:`SQLiteConnectionPool` pre-creates a
small, fixed number of connections, configures the ``PRAGMA`` statements exactly
once per connection at creation time, and hands them out through a context
manager that returns the connection to the pool on exit.

The pool is deliberately small.  WAL permits only one writer at a time
regardless of how many connections exist, so a large pool would only waste file
handles.  The default size of four is comfortable for the read-mostly workload
``MemoryGraph`` produces while leaving headroom for concurrent readers.
"""

from __future__ import annotations

import collections
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress

__all__ = [
    "DEFAULT_CHECKOUT_TIMEOUT",
    "DEFAULT_POOL_SIZE",
    "ConnectionPoolClosedError",
    "SQLiteConnectionPool",
]

#: Default number of connections kept in the pool.  WAL allows a single writer
#: regardless of pool size, so this stays small on purpose.
DEFAULT_POOL_SIZE = 4

#: Default number of seconds :meth:`SQLiteConnectionPool.checkout` waits for a
#: free connection before giving up.  Mirrors the SQLite ``busy_timeout`` used
#: elsewhere so legitimate contention waits, while a genuine exhaustion surfaces
#: as an error instead of hanging forever.
DEFAULT_CHECKOUT_TIMEOUT = 30.0


class ConnectionPoolClosedError(RuntimeError):
    """Raised when a connection is requested from a pool that is closed."""


class SQLiteConnectionPool:
    """A bounded, thread-safe pool of pre-configured SQLite connections.

    Args:
        connection_factory: A zero-argument callable that returns a fully
            configured :class:`sqlite3.Connection` (``row_factory`` set and all
            ``PRAGMA`` statements applied).  The factory is invoked exactly
            ``size`` times when the pool is constructed, so the per-connection
            ``PRAGMA`` cost is paid once up front rather than on every checkout.
        size: Number of connections to pre-create.  Must be at least 1.
        checkout_timeout: Seconds to wait for a free connection before raising
            :class:`TimeoutError`.  ``None`` waits indefinitely.

    Thread safety:
        All mutable state -- the idle deque, the leased counter, and the closed
        flag -- is guarded by a single :class:`threading.Condition`.  Checkout
        acquisition and the closed-state transition therefore share one
        synchronization strategy: a checkout that has to wait blocks on the
        condition and is woken either when a connection is returned or when the
        pool is closed, so a closing pool never strands a waiter.  The number of
        connections handed out can never exceed ``size``.
    """

    def __init__(
        self,
        connection_factory: Callable[[], sqlite3.Connection],
        *,
        size: int = DEFAULT_POOL_SIZE,
        checkout_timeout: float | None = DEFAULT_CHECKOUT_TIMEOUT,
    ) -> None:
        if size < 1:
            raise ValueError("Connection pool size must be at least 1.")
        if checkout_timeout is not None and checkout_timeout < 0:
            raise ValueError("checkout_timeout must be non-negative or None.")
        self._size = size
        self._checkout_timeout = checkout_timeout

        # Every mutable field below is protected by self._condition.
        self._condition = threading.Condition()
        self._idle: collections.deque[sqlite3.Connection] = collections.deque()
        self._all_connections: list[sqlite3.Connection] = []
        self._leased = 0
        self._closed = False

        # Build the connections up front.  If a later factory call fails, close
        # the ones already created so a failed startup neither leaks file
        # handles nor leaves the database locked.  The factory is intentionally
        # *not* retained afterwards: the pool is fixed-size and never creates
        # more connections, and keeping the factory would pin whatever its
        # closure captures (e.g. the owning MemoryGraph) for the pool's lifetime.
        created: list[sqlite3.Connection] = []
        try:
            for _ in range(size):
                created.append(connection_factory())
        except BaseException:
            for connection in created:
                with suppress(sqlite3.Error):
                    connection.close()
            raise
        self._all_connections = created
        self._idle.extend(created)

    @property
    def size(self) -> int:
        """The fixed number of connections managed by the pool."""
        return self._size

    @property
    def closed(self) -> bool:
        """Whether :meth:`close` has been called."""
        with self._condition:
            return self._closed

    def available(self) -> int:
        """Number of connections currently idle in the pool.

        Intended for tests and introspection; in a concurrent setting the value
        may be stale the instant it is read.
        """
        with self._condition:
            return len(self._idle)

    def _acquire(self) -> sqlite3.Connection:
        """Block until a connection is free, then lease it. Caller must release."""
        with self._condition:
            if self._closed:
                raise ConnectionPoolClosedError("Cannot check out a connection from a closed pool.")
            deadline = None if self._checkout_timeout is None else time.monotonic() + self._checkout_timeout
            while not self._idle:
                if self._closed:
                    raise ConnectionPoolClosedError("Connection pool was closed while waiting for a connection.")
                if deadline is None:
                    self._condition.wait()
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(
                            f"Timed out after {self._checkout_timeout}s waiting for a pooled SQLite connection."
                        )
                    self._condition.wait(timeout=remaining)
            connection = self._idle.popleft()
            self._leased += 1
            return connection

    def _release(self, connection: sqlite3.Connection) -> None:
        """Return a leased connection to the pool, or close it if shutting down."""
        with self._condition:
            self._leased -= 1
            if self._closed:
                # The pool was closed while this connection was leased.  Close it
                # now, on the borrower's thread, rather than returning it to the
                # idle set -- and never while it was still in use.
                with suppress(sqlite3.Error):
                    connection.close()
                if self._leased == 0:
                    self._condition.notify_all()  # let a draining close() proceed
            else:
                self._idle.append(connection)
                self._condition.notify()

    @contextmanager
    def checkout(self) -> Iterator[sqlite3.Connection]:
        """Borrow a connection, returning it to the pool on exit.

        The context manager mirrors the transaction semantics of using a
        :class:`sqlite3.Connection` directly as a context manager: the
        transaction is committed on a clean exit and rolled back if the body
        raises.  If the commit itself fails the connection is rolled back before
        the error propagates, matching :meth:`sqlite3.Connection.__exit__` on
        Python 3.12.  Unlike the bare connection context manager, the connection
        is *not* closed afterwards -- it is returned to the pool for reuse.

        Raises:
            ConnectionPoolClosedError: If the pool is closed (or is closed while
                this call is waiting for a connection).
            TimeoutError: If no connection becomes available within
                ``checkout_timeout`` seconds.
        """
        connection = self._acquire()
        try:
            yield connection
        except BaseException:
            # Match sqlite3.Connection.__exit__: roll back on error.
            with suppress(sqlite3.Error):
                connection.rollback()
            raise
        else:
            # Match sqlite3.Connection.__exit__: commit on success.  If the
            # commit fails, roll back before propagating so a clean connection
            # goes back to the pool.
            try:
                connection.commit()
            except BaseException:
                with suppress(sqlite3.Error):
                    connection.rollback()
                raise
        finally:
            self._release(connection)

    def close(self, *, drain_timeout: float | None = None) -> None:
        """Close the pool. Idempotent and safe to call twice.

        Idle connections are closed immediately.  Connections that are currently
        leased are *not* closed underneath their borrower; each is closed by
        :meth:`_release` when it is returned.  Any thread waiting in
        :meth:`checkout` is woken so it raises
        :class:`ConnectionPoolClosedError` instead of blocking forever.

        Args:
            drain_timeout: If given, block up to this many seconds for
                outstanding leases to be returned before this call returns.  By
                default the call does not wait; leased connections still close on
                return.
        """
        if drain_timeout is not None and drain_timeout < 0:
            raise ValueError("drain_timeout must be non-negative or None.")
        with self._condition:
            if not self._closed:
                self._closed = True
                # Wake every waiter so it observes the closed state and stops.
                self._condition.notify_all()
                idle = list(self._idle)
                self._idle.clear()
            else:
                idle = []
            if drain_timeout is not None:
                deadline = time.monotonic() + drain_timeout
                while self._leased > 0:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._condition.wait(timeout=remaining)

        # Close idle connections outside the lock; they are no longer reachable
        # by other threads (removed from the idle set under a closed pool).
        for connection in idle:
            with suppress(sqlite3.Error):
                connection.close()

    def __enter__(self) -> SQLiteConnectionPool:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
