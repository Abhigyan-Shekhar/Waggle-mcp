"""Verify that heavy ML imports (torch, sentence_transformers) do not
happen on simple CLI invocations like --help or doctor.

This is a regression guard for https://github.com/Abhigyan-Shekhar/Waggle-mcp/issues/255
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# Modules whose import at startup should be considered a bug.
# sentence_transformers transitively pulls in torch (~800 MB) and is not
# needed for --help, doctor, or any deterministic-mode operations.
HEAVY_MODULES = frozenset({"sentence_transformers", "torch"})


def _check_heavy_modules(code: str) -> set[str]:
    """Run *code* in a clean subprocess and return the set of heavy modules
    (sentence_transformers / torch) that were loaded after execution.

    The *code* should print ``repr(the_set)`` as the *last* line of stdout
    so we can safely eval it back.
    """
    src = str(Path(__file__).resolve().parents[1] / "src")
    wrapper = (
        "import sys\n"
        f"sys.path.insert(0, {src!r})\n"
        f"{code}\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", wrapper],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"subprocess failed (exit={result.returncode}):\n"
            f"--- stderr ---\n{result.stderr}\n"
            f"--- stdout ---\n{result.stdout}"
        )
    # The last non-empty line of stdout should be the repr(set)
    lines = [l for l in result.stdout.strip().split("\n") if l.strip()]
    if not lines:
        raise RuntimeError(f"No output from subprocess:\n{result.stderr}")
    return eval(lines[-1])  # safe: repr of a set of strings


def test_import_waggle_does_not_import_heavy_modules() -> None:
    """Simply importing the ``waggle`` package must NOT trigger
    ``sentence_transformers`` or ``torch`` to be loaded."""
    loaded = _check_heavy_modules(
        "import waggle\n"
        "heavy = {m for m in sys.modules\n"
        "        if m.startswith('sentence_transformers') or m.startswith('torch')}\n"
        "print(repr(heavy))"
    )
    assert not HEAVY_MODULES.intersection(loaded), (
        f"Heavy modules loaded by 'import waggle': {loaded}"
    )


def test_import_waggle_server_does_not_import_heavy_modules() -> None:
    """Importing the server module (entry-point) must not pull in heavy deps."""
    loaded = _check_heavy_modules(
        "from waggle.server import _build_parser\n"
        "heavy = {m for m in sys.modules\n"
        "        if m.startswith('sentence_transformers') or m.startswith('torch')}\n"
        "print(repr(heavy))"
    )
    assert not HEAVY_MODULES.intersection(loaded), (
        f"Heavy modules loaded by 'from waggle.server import _build_parser': {loaded}"
    )


def test_import_waggle_graph_does_not_import_heavy_modules() -> None:
    """Importing the graph module must not pull in heavy deps."""
    loaded = _check_heavy_modules(
        "from waggle.graph import MemoryGraph\n"
        "heavy = {m for m in sys.modules\n"
        "        if m.startswith('sentence_transformers') or m.startswith('torch')}\n"
        "print(repr(heavy))"
    )
    assert not HEAVY_MODULES.intersection(loaded), (
        f"Heavy modules loaded by 'from waggle.graph import MemoryGraph': {loaded}"
    )


def test_embedding_deterministic_mode_does_not_import_heavy_modules() -> None:
    """Instantiating ``EmbeddingModel`` with ``WAGGLE_MODEL=deterministic``
    must not import heavy modules until *embed()* is actually called."""
    loaded = _check_heavy_modules(
        "import os\n"
        "os.environ['WAGGLE_MODEL'] = 'deterministic'\n"
        "from waggle.embeddings import EmbeddingModel\n"
        "m = EmbeddingModel('deterministic')\n"
        "m.start_background_warmup()\n"
        "heavy = {m for m in sys.modules\n"
        "        if m.startswith('sentence_transformers') or m.startswith('torch')}\n"
        "print(repr(heavy))"
    )
    assert not HEAVY_MODULES.intersection(loaded), (
        f"Heavy modules loaded by deterministic EmbeddingModel setup: {loaded}"
    )


def test_server_forward_refs_do_not_leak_heavy_imports() -> None:
    """Type hints/forward references in server.py should not force eager
    imports of sentence_transformers or torch."""
    loaded = _check_heavy_modules(
        "from waggle.server import main\n"
        "heavy = {m for m in sys.modules\n"
        "        if m.startswith('sentence_transformers') or m.startswith('torch')}\n"
        "print(repr(heavy))"
    )
    assert not HEAVY_MODULES.intersection(loaded), (
        f"Heavy modules loaded by 'from waggle.server import main': {loaded}"
    )
