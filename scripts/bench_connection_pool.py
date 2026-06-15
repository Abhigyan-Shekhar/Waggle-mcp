"""Ad-hoc before/after benchmark for issue #126.

Measures two things, reporting the median over several runs:

1. Micro: the raw cost of acquiring a usable connection 100 times, the old way
   (sqlite3.connect + 7 PRAGMAs every time) vs. the new way (pool checkout).
2. End-to-end: 100 MemoryGraph.add_node operations with the pool vs. with the
   pool patched to open a fresh connection per checkout (the pre-PR behavior).
"""

from __future__ import annotations

import gc
import statistics
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

# Locate the repo's src/ directory regardless of where this script lives
# (repo root, scripts/, etc.) by walking up until we find src/waggle.
_here = Path(__file__).resolve()
for _candidate in (_here.parent, *_here.parents):
    if (_candidate / "src" / "waggle").is_dir():
        sys.path.insert(0, str(_candidate / "src"))
        break

import numpy as np  # noqa: E402

from waggle.graph import MemoryGraph  # noqa: E402
from waggle.models import NodeType  # noqa: E402

N = 100
RUNS = 5


class FakeEmbeddingModel:
    model_name = "fake-model"
    model_id = "fake-model:deterministic-v1"

    def embed(self, text: str) -> np.ndarray:
        vector = np.zeros(8, dtype=np.float32)
        for token in text.lower().split():
            vector[sum(ord(c) for c in token) % len(vector)] += 1.0
        norm = np.linalg.norm(vector)
        return vector if norm == 0.0 else vector / norm

    def to_bytes(self, e: np.ndarray) -> bytes:
        return e.astype(np.float32).tobytes()

    def from_bytes(self, d: bytes) -> np.ndarray:
        return np.frombuffer(d, dtype=np.float32)

    def cosine_similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        na, nb = np.linalg.norm(a), np.linalg.norm(b)
        return 0.0 if na == 0 or nb == 0 else float(np.dot(a, b) / (na * nb))


def micro_once(graph: MemoryGraph) -> tuple[float, float]:
    # OLD: fresh connection + full PRAGMA round every acquisition.
    start = time.perf_counter()
    for _ in range(N):
        conn = graph._connect()
        conn.execute("SELECT 1").fetchone()
        conn.commit()
        conn.close()
    old = time.perf_counter() - start

    # NEW: borrow a pre-configured connection from the pool.
    start = time.perf_counter()
    for _ in range(N):
        with graph._pool.checkout() as conn:
            conn.execute("SELECT 1").fetchone()
    new = time.perf_counter() - start
    return old, new


def e2e_once() -> tuple[float, float]:
    embedder = FakeEmbeddingModel()

    # BEFORE: pool patched so each checkout opens a brand-new connection,
    # exactly reproducing the pre-PR `with self._connect() as connection:` path.
    with TemporaryDirectory() as tmp:
        graph = MemoryGraph(Path(tmp) / "before.db", embedder)

        @contextmanager
        def fresh_checkout():
            conn = graph._connect() # noqa: F821
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()

        original_checkout = graph._pool.checkout
        graph._pool.checkout = fresh_checkout  # type: ignore[method-assign]
        start = time.perf_counter()
        for i in range(N):
            graph.add_node(label=f"n{i}", content=f"content number {i}", node_type=NodeType.ENTITY)
        before = time.perf_counter() - start

        # Break the reference cycle
        graph._pool.checkout = original_checkout
        graph.close()

        # Important: Force garbage collection INSIDE the 'with' block  before the temp dir attempts to delete the locked file.
        del graph
        gc.collect()

    # AFTER: real pooled connections.
    with TemporaryDirectory() as tmp:
        graph = MemoryGraph(Path(tmp) / "after.db", embedder)
        start = time.perf_counter()
        for i in range(N):
            graph.add_node(label=f"n{i}", content=f"content number {i}", node_type=NodeType.ENTITY)
        after = time.perf_counter() - start
        graph.close()

        # Apply the same gc cleanup here as well
        del graph
        gc.collect()

    return before, after


def report(name: str, before: list[float], after: list[float]) -> None:
    b = statistics.median(before)
    a = statistics.median(after)
    print(
        f"  {name:<16} before = {b * 1e3:8.2f} ms   after = {a * 1e3:8.2f} ms   "
        f"speedup = {b / a:5.2f}x   (median of {RUNS} runs)"
    )


if __name__ == "__main__":
    print(f"SQLite connection pooling benchmark (N={N} ops/run, {RUNS} runs)\n")

    micro_old: list[float] = []
    micro_new: list[float] = []
    with TemporaryDirectory() as tmp:
        g = MemoryGraph(Path(tmp) / "micro.db", FakeEmbeddingModel())
        for _ in range(RUNS):
            o, n = micro_once(g)
            micro_old.append(o)
            micro_new.append(n)
        g.close()
    report("acquisition x100", micro_old, micro_new)

    e2e_before: list[float] = []
    e2e_after: list[float] = []
    for _ in range(RUNS):
        b, a = e2e_once()
        e2e_before.append(b)
        e2e_after.append(a)
    report("add_node x100", e2e_before, e2e_after)
