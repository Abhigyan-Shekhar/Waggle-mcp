"""Tests for :mod:`waggle.connection_pool` and its use by ``MemoryGraph``.

Covers the acceptance criteria from issue #126 plus the hardening from review:

* connections are reused across operations (the factory is not re-invoked and no
  fresh PRAGMA round happens per checkout),
* the pool size stays bounded under repeated checkout/return,
* two threads checking out connections at the same time do not crash,
* a failed construction closes the connections it already opened,
* a failed ``commit()`` rolls back before the connection returns to the pool,
* ``close()`` wakes waiting checkouts and never closes a leased connection
  underneath its borrower,
* ``MemoryGraph`` routes its operations through the pool and tears it down on
  ``close()`` while tenant clones safely share it.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import ExitStack
from pathlib import Path

import numpy as np
import pytest

from waggle.connection_pool import (
    DEFAULT_POOL_SIZE,
    ConnectionPoolClosedError,
    SQLiteConnectionPool,
)
from waggle.graph import MemoryGraph
from waggle.models import NodeType


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
class _CountingFactory:
    """A connection factory that records how many connections it has created."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.calls = 0

    def __call__(self) -> sqlite3.Connection:
        self.calls += 1
        connection = sqlite3.connect(str(self.db_path), check_same_thread=False)
        connection.row_factory = sqlite3.Row
        # The same PRAGMAs MemoryGraph._connect applies, paid once per connection.
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection


class _CommitControlConnection(sqlite3.Connection):
    """Connection whose commit can be made to fail, for transaction tests."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.fail_commit = False
        self.rolled_back = False

    def commit(self) -> None:
        if self.fail_commit:
            raise sqlite3.OperationalError("commit boom")
        super().commit()

    def rollback(self) -> None:
        self.rolled_back = True
        super().rollback()


class FakeEmbeddingModel:
    """Deterministic, dependency-free embedding model for graph integration tests."""

    model_name = "fake-model"
    model_id = "fake-model:deterministic-v1"

    def embed(self, text: str) -> np.ndarray:
        vector = np.zeros(8, dtype=np.float32)
        for token in text.lower().split():
            index = sum(ord(character) for character in token) % len(vector)
            vector[index] += 1.0
        norm = np.linalg.norm(vector)
        return vector if norm == 0.0 else vector / norm

    def to_bytes(self, embedding: np.ndarray) -> bytes:
        return embedding.astype(np.float32).tobytes()

    def from_bytes(self, data: bytes) -> np.ndarray:
        return np.frombuffer(data, dtype=np.float32)

    def cosine_similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        a_norm = np.linalg.norm(a)
        b_norm = np.linalg.norm(b)
        if a_norm == 0.0 or b_norm == 0.0:
            return 0.0
        return float(np.dot(a, b) / (a_norm * b_norm))


@pytest.fixture
def pool(tmp_path: Path):
    factory = _CountingFactory(tmp_path / "pool.db")
    created = SQLiteConnectionPool(factory, size=3, checkout_timeout=1.0)
    created.factory = factory  # expose for assertions
    try:
        yield created
    finally:
        created.close()


# --------------------------------------------------------------------------- #
# Construction and configuration
# --------------------------------------------------------------------------- #
def test_factory_called_once_per_connection_at_creation(tmp_path: Path) -> None:
    factory = _CountingFactory(tmp_path / "db.sqlite")
    created = SQLiteConnectionPool(factory, size=4)
    try:
        assert factory.calls == 4
        assert created.size == 4
        assert created.available() == 4
    finally:
        created.close()


def test_default_size_is_small(tmp_path: Path) -> None:
    factory = _CountingFactory(tmp_path / "db.sqlite")
    created = SQLiteConnectionPool(factory)
    try:
        assert created.size == DEFAULT_POOL_SIZE
        assert factory.calls == DEFAULT_POOL_SIZE
    finally:
        created.close()


def test_invalid_size_rejected(tmp_path: Path) -> None:
    factory = _CountingFactory(tmp_path / "db.sqlite")
    with pytest.raises(ValueError):
        SQLiteConnectionPool(factory, size=0)


def test_negative_checkout_timeout_rejected(tmp_path: Path) -> None:
    factory = _CountingFactory(tmp_path / "db.sqlite")
    with pytest.raises(ValueError):
        SQLiteConnectionPool(factory, checkout_timeout=-1.0)


def test_negative_drain_timeout_rejected(tmp_path: Path) -> None:
    factory = _CountingFactory(tmp_path / "db.sqlite")
    created = SQLiteConnectionPool(factory, size=1)
    try:
        with pytest.raises(ValueError):
            created.close(drain_timeout=-1.0)
    finally:
        created.close()


def test_pragmas_applied_to_pooled_connections(pool: SQLiteConnectionPool) -> None:
    with pool.checkout() as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    assert journal_mode.lower() == "wal"
    assert foreign_keys == 1


def test_failed_construction_closes_already_created_connections(tmp_path: Path) -> None:
    created: list[sqlite3.Connection] = []
    calls = {"n": 0}

    def factory() -> sqlite3.Connection:
        calls["n"] += 1
        if calls["n"] == 3:  # fail partway through construction
            raise sqlite3.OperationalError("cannot open database")
        connection = sqlite3.connect(str(tmp_path / "fail.db"), check_same_thread=False)
        created.append(connection)
        return connection

    with pytest.raises(sqlite3.OperationalError):
        SQLiteConnectionPool(factory, size=4)

    # The two connections opened before the failure must have been closed.
    assert len(created) == 2
    for connection in created:
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")


# --------------------------------------------------------------------------- #
# Reuse: no new connection / PRAGMA per checkout
# --------------------------------------------------------------------------- #
def test_connections_are_reused_without_new_factory_calls(pool: SQLiteConnectionPool) -> None:
    calls_after_construction = pool.factory.calls
    seen: set[int] = set()
    for _ in range(50):
        with pool.checkout() as connection:
            seen.add(id(connection))
    assert pool.factory.calls == calls_after_construction
    assert len(seen) <= pool.size


# --------------------------------------------------------------------------- #
# Bounded size
# --------------------------------------------------------------------------- #
def test_pool_size_stays_bounded(pool: SQLiteConnectionPool) -> None:
    with ExitStack() as stack:
        connections = [stack.enter_context(pool.checkout()) for _ in range(pool.size)]
        assert len(connections) == pool.size
        assert pool.available() == 0
        # No extra connection exists; a further checkout times out rather than
        # silently growing the pool.
        with pytest.raises(TimeoutError), pool.checkout():
            pass
    assert pool.available() == pool.size


def test_checkout_returns_connection_to_pool(pool: SQLiteConnectionPool) -> None:
    assert pool.available() == pool.size
    with pool.checkout():
        assert pool.available() == pool.size - 1
    assert pool.available() == pool.size


# --------------------------------------------------------------------------- #
# Transaction semantics mirror sqlite3.Connection context manager
# --------------------------------------------------------------------------- #
def test_commit_on_clean_exit(pool: SQLiteConnectionPool) -> None:
    with pool.checkout() as connection:
        connection.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        connection.execute("INSERT INTO t (v) VALUES ('kept')")
    with pool.checkout() as connection:
        rows = connection.execute("SELECT v FROM t").fetchall()
    assert [row[0] for row in rows] == ["kept"]


def test_rollback_on_error(pool: SQLiteConnectionPool) -> None:
    with pool.checkout() as connection:
        connection.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    with pytest.raises(RuntimeError), pool.checkout() as connection:
        connection.execute("INSERT INTO t (v) VALUES ('discarded')")
        raise RuntimeError("boom")
    with pool.checkout() as connection:
        count = connection.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    assert count == 0


def test_commit_failure_rolls_back_before_returning(tmp_path: Path) -> None:
    db_path = tmp_path / "commitfail.db"

    def factory() -> sqlite3.Connection:
        connection = sqlite3.connect(str(db_path), factory=_CommitControlConnection, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        return connection

    created = SQLiteConnectionPool(factory, size=1)
    try:
        with created.checkout() as connection:
            connection.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")

        target = created._all_connections[0]
        target.fail_commit = True
        target.rolled_back = False

        # Clean body, but the commit on exit fails: expect rollback + propagate.
        with pytest.raises(sqlite3.OperationalError), created.checkout() as connection:
            connection.execute("INSERT INTO t (id) VALUES (1)")

        assert target.rolled_back is True
        assert created.available() == 1  # still returned to the pool

        target.fail_commit = False
        with created.checkout() as connection:
            assert connection.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
    finally:
        created.close()


# --------------------------------------------------------------------------- #
# Thread safety
# --------------------------------------------------------------------------- #
def test_concurrent_checkout_does_not_crash(tmp_path: Path) -> None:
    factory = _CountingFactory(tmp_path / "concurrent.db")
    created = SQLiteConnectionPool(factory, size=4, checkout_timeout=5.0)
    with created.checkout() as connection:
        connection.execute("CREATE TABLE counter (n INTEGER)")
        connection.execute("INSERT INTO counter (n) VALUES (0)")

    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        try:
            barrier.wait()
            for _ in range(40):
                with created.checkout() as conn:
                    conn.execute("SELECT n FROM counter").fetchone()
        except BaseException as exc:  # record for the assertion
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    try:
        assert not any(thread.is_alive() for thread in threads), "a worker thread hung"
        assert errors == []
        assert created.available() == created.size
        assert factory.calls == created.size
    finally:
        created.close()


# --------------------------------------------------------------------------- #
# close()
# --------------------------------------------------------------------------- #
def test_close_blocks_further_checkout(tmp_path: Path) -> None:
    factory = _CountingFactory(tmp_path / "closed.db")
    created = SQLiteConnectionPool(factory, size=2)
    created.close()
    assert created.closed is True
    with pytest.raises(ConnectionPoolClosedError), created.checkout():
        pass


def test_close_is_idempotent(tmp_path: Path) -> None:
    factory = _CountingFactory(tmp_path / "closed.db")
    created = SQLiteConnectionPool(factory, size=2)
    created.close()
    created.close()  # must not raise


def test_close_closes_underlying_connections(tmp_path: Path) -> None:
    factory = _CountingFactory(tmp_path / "closed.db")
    created = SQLiteConnectionPool(factory, size=1)
    leaked = created._all_connections[0]
    created.close()
    with pytest.raises(sqlite3.ProgrammingError):
        leaked.execute("SELECT 1")


def test_close_wakes_a_waiting_checkout(tmp_path: Path) -> None:
    factory = _CountingFactory(tmp_path / "wake.db")
    created = SQLiteConnectionPool(factory, size=1, checkout_timeout=None)

    # Fire an Event the instant the waiter parks in the pool's condition wait,
    # so close() runs only once the waiter has definitely reached the blocking
    # point -- no fixed sleeps. The waiter holds the condition lock until wait()
    # atomically releases it, so close() (which must take that same lock to
    # notify) cannot wake the waiter before it is genuinely parked.
    waiting = threading.Event()
    original_wait = created._condition.wait

    def signaling_wait(*args: object, **kwargs: object) -> bool:
        waiting.set()
        return original_wait(*args, **kwargs)

    created._condition.wait = signaling_wait  # type: ignore[method-assign]

    held = created.checkout()
    held.__enter__()  # occupy the only connection so the waiter must block

    outcome: dict[str, str] = {}

    def waiter() -> None:
        try:
            with created.checkout():
                outcome["result"] = "acquired"
        except ConnectionPoolClosedError:
            outcome["result"] = "closed"
        except BaseException as exc:  # pragma: no cover - unexpected
            outcome["result"] = f"error:{type(exc).__name__}"

    thread = threading.Thread(target=waiter)
    thread.start()
    assert waiting.wait(timeout=5), "waiter never reached the blocking wait"

    created.close()  # must wake the blocked waiter, not strand it
    held.__exit__(None, None, None)
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert outcome["result"] == "closed"


def test_close_does_not_close_a_leased_connection(tmp_path: Path) -> None:
    factory = _CountingFactory(tmp_path / "lease.db")
    created = SQLiteConnectionPool(factory, size=2)
    with created.checkout() as connection:
        created.close()  # closes idle connections, but not this leased one
        assert connection.execute("SELECT 1").fetchone()[0] == 1
    assert created.closed is True


def test_close_with_drain_timeout_waits_for_leases(tmp_path: Path) -> None:
    factory = _CountingFactory(tmp_path / "drain.db")
    created = SQLiteConnectionPool(factory, size=2)

    # Fire an Event when close() enters its drain wait. The borrower never waits
    # on the pool condition (size=2, a connection is free), so this only signals
    # for close()'s drain loop -- letting us release the lease exactly when close
    # is provably blocking, with no fixed sleeps.
    close_is_draining = threading.Event()
    original_wait = created._condition.wait

    def signaling_wait(*args: object, **kwargs: object) -> bool:
        close_is_draining.set()
        return original_wait(*args, **kwargs)

    created._condition.wait = signaling_wait  # type: ignore[method-assign]

    acquired = threading.Event()
    may_release = threading.Event()
    released = threading.Event()

    def borrower() -> None:
        with created.checkout():
            acquired.set()  # the lease is now held
            assert may_release.wait(timeout=5), "borrower was never released"
        released.set()  # connection returned to the pool

    borrower_thread = threading.Thread(target=borrower)
    borrower_thread.start()
    assert acquired.wait(timeout=5), "borrower never leased a connection"

    # Run close() off the main thread so we can confirm it actually blocks on the
    # outstanding lease before we hand the connection back.
    closed = threading.Event()

    def closer() -> None:
        created.close(drain_timeout=5.0)
        closed.set()

    closer_thread = threading.Thread(target=closer)
    closer_thread.start()

    # close() is now provably blocked in its drain wait while the lease is held.
    assert close_is_draining.wait(timeout=5), "close() did not block on the lease"
    assert not closed.is_set()
    assert not released.is_set()

    may_release.set()  # let the borrower return the connection
    assert closed.wait(timeout=5), "close() did not return after the lease drained"
    assert released.is_set()  # close waited for the lease to drain

    borrower_thread.join(timeout=5)
    closer_thread.join(timeout=5)


def test_context_manager_closes_pool(tmp_path: Path) -> None:
    factory = _CountingFactory(tmp_path / "ctx.db")
    with SQLiteConnectionPool(factory, size=2) as created:
        assert created.available() == 2
    assert created.closed is True


# --------------------------------------------------------------------------- #
# MemoryGraph integration
# --------------------------------------------------------------------------- #
def _make_graph(tmp_path: Path) -> MemoryGraph:
    return MemoryGraph(tmp_path / "memory.db", FakeEmbeddingModel())


def test_memory_graph_builds_a_bounded_pool(tmp_path: Path) -> None:
    graph = _make_graph(tmp_path)
    try:
        assert graph._owns_pool is True
        assert graph._pool.size == DEFAULT_POOL_SIZE
        assert len(graph._pool._all_connections) == DEFAULT_POOL_SIZE
    finally:
        graph.close()


def test_memory_graph_operations_do_not_open_new_connections(tmp_path: Path, monkeypatch) -> None:
    graph = _make_graph(tmp_path)
    try:

        def _fail() -> sqlite3.Connection:  # pragma: no cover - only on regression
            raise AssertionError("_connect was called after pool construction")

        monkeypatch.setattr(graph, "_connect", _fail)

        for index in range(10):
            graph.add_node(
                label=f"node-{index}",
                content=f"content about topic number {index}",
                node_type=NodeType.ENTITY,
            )
        # query() exercises the HybridRetriever, which also borrows from the pool.
        result = graph.query(query="topic", max_nodes=5, max_depth=1)

        assert result is not None
        assert graph._pool.available() == graph._pool.size
    finally:
        graph.close()


def test_retrieval_modes_reuse_pooled_connections(tmp_path: Path, monkeypatch) -> None:
    # Exercises the HybridRetriever hot paths (hybrid + verbatim), which the
    # default "graph" mode does not reach. Seed transcript + node data first so
    # those layers actually run instead of short-circuiting on an empty store.
    graph = _make_graph(tmp_path)
    try:
        graph.observe_conversation(
            user_message="The launch codeword is saffron-badger.",
            assistant_response="Understood, the codeword saffron-badger is recorded.",
            project="alpha",
            session_id="sess-1",
        )

        # Spy (rather than raise) so the check holds even if a retrieval layer
        # swallows exceptions: any fresh _connect() after pool construction is a
        # regression, regardless of whether it surfaces as an error.
        connect_calls = {"n": 0}
        real_connect = graph._connect

        def _spy(*args: object, **kwargs: object) -> sqlite3.Connection:
            connect_calls["n"] += 1
            return real_connect(*args, **kwargs)

        monkeypatch.setattr(graph, "_connect", _spy)

        for mode in ("hybrid", "verbatim"):
            result = graph.query(
                query="what is the launch codeword",
                project="alpha",
                retrieval_mode=mode,
                max_nodes=5,
            )
            assert result is not None
            assert result.retrieval_mode == mode

        assert connect_calls["n"] == 0, "retrieval opened a fresh connection instead of using the pool"
        assert graph._pool.available() == graph._pool.size
    finally:
        graph.close()


def test_memory_graph_is_a_context_manager(tmp_path: Path) -> None:
    with MemoryGraph(tmp_path / "cm.db", FakeEmbeddingModel()) as graph:
        graph.add_node(label="inside", content="within the with block", node_type=NodeType.ENTITY)
        pool = graph._pool
    assert pool.closed is True


def test_for_tenant_clone_keeps_pool_alive_after_owner_is_dropped(tmp_path: Path) -> None:
    import gc

    graph = _make_graph(tmp_path)
    clone = graph.for_tenant("tenant-x")
    pool = clone._pool
    assert pool.closed is False

    # Drop the only external reference to the owner. Because the clone roots the
    # owner (clone._pool_owner), the owner is not collected and its __del__ does
    # not close the still-shared pool.
    del graph
    gc.collect()

    assert pool.closed is False
    # The clone can still use the pool after the owner reference is gone.
    clone.add_node(label="after-owner-drop", content="clone still works", node_type=NodeType.ENTITY)
    assert clone._pool.available() == clone._pool.size

    clone.close()  # clone is a non-owner; pool is torn down when the owner is finalized


def test_for_tenant_shares_pool_without_owning_it(tmp_path: Path) -> None:
    graph = _make_graph(tmp_path)
    try:
        clone = graph.for_tenant("tenant-b")
        assert clone._pool is graph._pool
        assert clone._owns_pool is False

        # Closing the clone must not tear down the shared pool.
        clone.close()
        assert graph._pool.closed is False
        graph.add_node(label="still-alive", content="owner still works", node_type=NodeType.ENTITY)
    finally:
        graph.close()


def test_memory_graph_close_closes_pool(tmp_path: Path) -> None:
    graph = _make_graph(tmp_path)
    graph.add_node(label="before-close", content="written before close", node_type=NodeType.ENTITY)
    graph.close()
    assert graph._pool.closed is True
    graph.close()  # idempotent
