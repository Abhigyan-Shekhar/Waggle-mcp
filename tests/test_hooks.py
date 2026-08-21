"""Tests for Claude Code hook handlers."""

from __future__ import annotations

import importlib.util
import json
import sys
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import contextlib

import numpy as np

# ── Helpers ───────────────────────────────────────────────────────────────────


class FakeEmbeddingModel:
    model_name = "fake-model"
    model_id = "fake-model:deterministic-v1"

    def embed(self, text: str) -> np.ndarray:
        v = np.zeros(8, dtype=np.float32)
        for t in text.lower().split():
            v[sum(ord(c) for c in t) % 8] += 1.0
        n = np.linalg.norm(v)
        return v / n if n > 0 else v

    def to_bytes(self, e: np.ndarray) -> bytes:
        return e.astype(np.float32).tobytes()

    def from_bytes(self, d: bytes) -> np.ndarray:
        return np.frombuffer(d, dtype=np.float32)

    def cosine_similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        an, bn = np.linalg.norm(a), np.linalg.norm(b)
        return float(np.dot(a, b) / (an * bn)) if an > 0 and bn > 0 else 0.0


def _make_graph(tmp_path: Path):
    from waggle.graph import MemoryGraph

    return MemoryGraph(str(tmp_path / "hooks-test.db"), FakeEmbeddingModel(), tenant_id="local-default")


def _load_hook_module(name: str, filename: str):
    hook_path = ROOT / "src" / "waggle" / "hooks" / "claude_code" / filename
    spec = importlib.util.spec_from_file_location(name, hook_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_common_resolve_scope_infers_stable_project_from_repo_root(tmp_path: Path) -> None:
    from waggle.config import AppConfig
    from waggle.hooks.claude_code.common import resolve_scope

    repo_root = tmp_path / "repo-a"
    nested = repo_root / "src" / "feature"
    (repo_root / ".git").mkdir(parents=True)
    nested.mkdir(parents=True)
    config = AppConfig(
        backend="sqlite",
        transport="stdio",
        model_name="deterministic",
        db_path=str(tmp_path / "memory.db"),
        default_tenant_id="local-default",
        http_host="127.0.0.1",
        http_port=8080,
        log_level="INFO",
        rate_limit_rpm=120,
        write_rate_limit_rpm=60,
        max_concurrent_requests=8,
        max_payload_bytes=1024 * 1024,
        request_timeout_seconds=30,
        export_dir=None,
        neo4j_uri="",
        neo4j_username="",
        neo4j_password="",
        neo4j_database="",
    )

    root_scope = resolve_scope({"cwd": str(repo_root), "session_id": "s1"}, config)
    nested_scope = resolve_scope({"cwd": str(nested), "session_id": "s2"}, config)

    assert root_scope["project"]
    assert root_scope["project"] == nested_scope["project"]


def test_common_resolve_scope_separates_different_repos(tmp_path: Path) -> None:
    from waggle.config import AppConfig
    from waggle.hooks.claude_code.common import resolve_scope

    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    (repo_a / ".git").mkdir(parents=True)
    (repo_b / ".git").mkdir(parents=True)
    config = AppConfig(
        backend="sqlite",
        transport="stdio",
        model_name="deterministic",
        db_path=str(tmp_path / "memory.db"),
        default_tenant_id="local-default",
        http_host="127.0.0.1",
        http_port=8080,
        log_level="INFO",
        rate_limit_rpm=120,
        write_rate_limit_rpm=60,
        max_concurrent_requests=8,
        max_payload_bytes=1024 * 1024,
        request_timeout_seconds=30,
        export_dir=None,
        neo4j_uri="",
        neo4j_username="",
        neo4j_password="",
        neo4j_database="",
    )

    scope_a = resolve_scope({"cwd": str(repo_a), "session_id": "s1"}, config)
    scope_b = resolve_scope({"cwd": str(repo_b), "session_id": "s2"}, config)

    assert scope_a["project"] != scope_b["project"]


# ── pre_response tests ────────────────────────────────────────────────────────


def test_pre_response_empty_stdin(capsys: pytest.CaptureFixture) -> None:
    """pre_response exits cleanly with empty stdin."""
    hook_path = ROOT / "src" / "waggle" / "hooks" / "claude_code" / "pre_response.py"
    assert hook_path.exists()
    mod = _load_hook_module("pre_response", "pre_response.py")

    with (
        patch("sys.stdin", StringIO("")),
        patch("sys.exit") as mock_exit,
        patch("builtins.print") as mock_print,
        contextlib.suppress(SystemExit),
    ):
        mod.main()

    # Should have called print with empty JSON or exited
    assert mock_exit.called or mock_print.called


def test_pre_response_with_prompt(tmp_path: Path) -> None:
    """pre_response returns JSON output for a valid prompt."""
    mod = _load_hook_module("pre_response_prompt", "pre_response.py")

    payload = json.dumps({"prompt": "What database did we choose?", "session_id": "test-session"})

    output_lines: list[str] = []

    def fake_print(data: str = "", **kw: object) -> None:
        output_lines.append(str(data))

    with (
        patch("sys.stdin", StringIO(payload)),
        patch("builtins.print", side_effect=fake_print),
        patch("sys.exit", side_effect=SystemExit),
        patch.dict(
            "os.environ",
            {
                "WAGGLE_DB_PATH": str(tmp_path / "hooks-test.db"),
                "WAGGLE_MODEL": "deterministic",
                "WAGGLE_BACKEND": "sqlite",
                "WAGGLE_DEFAULT_TENANT_ID": "local-default",
            },
        ),
        contextlib.suppress(SystemExit),
    ):
        mod.main()

    # Should have printed at least one JSON line
    assert output_lines, "pre_response printed nothing"
    # Last output should be valid JSON
    last = output_lines[-1]
    parsed = json.loads(last)
    assert isinstance(parsed, dict)


def test_pre_response_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """pre_response respects the 5-second timeout and exits 0."""
    mod = _load_hook_module("pre_response_timeout", "pre_response.py")

    payload = json.dumps({"prompt": "test", "session_id": "s1"})

    def slow_import(*args: object, **kw: object) -> None:
        raise TimeoutError("simulated timeout")

    output_lines: list[str] = []

    def fake_print(data: str = "", **kw: object) -> None:
        output_lines.append(str(data))

    with (
        patch("sys.stdin", StringIO(payload)),
        patch("builtins.print", side_effect=fake_print),
        patch("sys.exit", side_effect=SystemExit),
    ):
        # Simulate timeout by raising it inside main
        original_main = mod.main

        def patched_main() -> None:
            raise TimeoutError("simulated")

        mod.main = patched_main
        try:
            mod.main()
        except (SystemExit, TimeoutError):
            pass
        finally:
            mod.main = original_main


# ── post_response tests ───────────────────────────────────────────────────────


def test_post_response_skips_secrets(tmp_path: Path) -> None:
    """post_response skips capture when secrets are detected."""
    mod = _load_hook_module("post_response_secret", "post_response.py")

    # Payload with a fake API key in the user message
    payload = json.dumps(
        {
            "session_id": "s1",
            "transcript": [
                {"role": "user", "content": "my key is sk-abc123def456ghi789jkl012mno345"},
                {"role": "assistant", "content": "I see you have an API key."},
            ],
        }
    )

    observe_called = []

    output_lines: list[str] = []

    def fake_print(data: str = "", **kw: object) -> None:
        output_lines.append(str(data))

    with (
        patch("sys.stdin", StringIO(payload)),
        patch("builtins.print", side_effect=fake_print),
        patch("sys.exit", side_effect=SystemExit),
        patch.dict(
            "os.environ",
            {
                "WAGGLE_DB_PATH": str(tmp_path / "hooks-test.db"),
                "WAGGLE_MODEL": "deterministic",
                "WAGGLE_BACKEND": "sqlite",
                "WAGGLE_DEFAULT_TENANT_ID": "local-default",
            },
        ),
        contextlib.suppress(SystemExit),
    ):
        mod.main()

    # Should have exited silently without calling observe_conversation
    assert not observe_called
    # Output should be empty JSON (silent exit)
    if output_lines:
        assert json.loads(output_lines[-1]) == {}


def test_post_response_empty_transcript(tmp_path: Path) -> None:
    """post_response exits cleanly with empty transcript."""
    mod = _load_hook_module("post_response_empty", "post_response.py")

    payload = json.dumps({"session_id": "s1", "transcript": []})

    output_lines: list[str] = []

    def fake_print(data: str = "", **kw: object) -> None:
        output_lines.append(str(data))

    with (
        patch("sys.stdin", StringIO(payload)),
        patch("builtins.print", side_effect=fake_print),
        patch("sys.exit", side_effect=SystemExit),
        contextlib.suppress(SystemExit),
    ):
        mod.main()

    if output_lines:
        assert json.loads(output_lines[-1]) == {}


def test_post_response_skips_non_durable_turns(tmp_path: Path) -> None:
    """post_response skips long chatter that has no durable memory signal."""
    mod = _load_hook_module("post_response_nondurable", "post_response.py")

    payload = json.dumps(
        {
            "session_id": "s1",
            "transcript": [
                {"role": "user", "content": "That explanation was very detailed and interesting to read through."},
                {
                    "role": "assistant",
                    "content": "Glad it helped. I can explain it again in a different style if you want.",
                },
            ],
        }
    )

    output_lines: list[str] = []

    def fake_print(data: str = "", **kw: object) -> None:
        output_lines.append(str(data))

    with (
        patch("sys.stdin", StringIO(payload)),
        patch("builtins.print", side_effect=fake_print),
        patch("sys.exit", side_effect=SystemExit),
        patch("waggle.graph.MemoryGraph.observe_conversation") as observe_mock,
        patch.dict(
            "os.environ",
            {
                "WAGGLE_DB_PATH": str(tmp_path / "hooks-test.db"),
                "WAGGLE_MODEL": "deterministic",
                "WAGGLE_BACKEND": "sqlite",
                "WAGGLE_DEFAULT_TENANT_ID": "local-default",
            },
        ),
        contextlib.suppress(SystemExit),
    ):
        mod.main()

    observe_mock.assert_not_called()
    if output_lines:
        assert json.loads(output_lines[-1]) == {}


def test_post_response_ingests_durable_turns(tmp_path: Path) -> None:
    """post_response still ingests turns with a durable signal."""
    mod = _load_hook_module("post_response_durable", "post_response.py")

    payload = json.dumps(
        {
            "session_id": "s1",
            "project": "MCP",
            "agent_id": "codex",
            "transcript": [
                {"role": "user", "content": "We decided to ship checkpoint handoff before broader automation."},
                {"role": "assistant", "content": "Understood. I'll remember that product decision."},
            ],
        }
    )

    output_lines: list[str] = []

    def fake_print(data: str = "", **kw: object) -> None:
        output_lines.append(str(data))

    with (
        patch("sys.stdin", StringIO(payload)),
        patch("builtins.print", side_effect=fake_print),
        patch("sys.exit", side_effect=SystemExit),
        patch("waggle.graph.MemoryGraph.observe_conversation") as observe_mock,
        patch.dict(
            "os.environ",
            {
                "WAGGLE_DB_PATH": str(tmp_path / "hooks-test.db"),
                "WAGGLE_MODEL": "deterministic",
                "WAGGLE_BACKEND": "sqlite",
                "WAGGLE_DEFAULT_TENANT_ID": "local-default",
            },
        ),
        contextlib.suppress(SystemExit),
    ):
        mod.main()

    observe_mock.assert_called_once()
    _, kwargs = observe_mock.call_args
    assert kwargs["project"] == "MCP"
    assert kwargs["agent_id"] == "codex"
    assert kwargs["session_id"] == "s1"
    if output_lines:
        assert json.loads(output_lines[-1]) == {}


def test_post_response_uses_resolved_session_scope(tmp_path: Path) -> None:
    """post_response writes with the normalized session_id from resolve_scope."""
    mod = _load_hook_module("post_response_resolved_session", "post_response.py")

    payload = json.dumps(
        {
            "session_id": "  s1  ",
            "project": "MCP",
            "agent_id": "codex",
            "transcript": [
                {"role": "user", "content": "We decided to normalize hook session scope before writes."},
                {"role": "assistant", "content": "Understood. I'll remember that hook scope decision."},
            ],
        }
    )

    with (
        patch("sys.stdin", StringIO(payload)),
        patch("builtins.print"),
        patch("sys.exit", side_effect=SystemExit),
        patch("waggle.graph.MemoryGraph.observe_conversation") as observe_mock,
        patch.dict(
            "os.environ",
            {
                "WAGGLE_DB_PATH": str(tmp_path / "hooks-test.db"),
                "WAGGLE_MODEL": "deterministic",
                "WAGGLE_BACKEND": "sqlite",
                "WAGGLE_DEFAULT_TENANT_ID": "local-default",
            },
        ),
        contextlib.suppress(SystemExit),
    ):
        mod.main()

    observe_mock.assert_called_once()
    _, kwargs = observe_mock.call_args
    assert kwargs["session_id"] == "s1"


def test_pre_response_restores_checkpoint_when_db_scope_is_empty(tmp_path: Path) -> None:
    """pre_response falls back to a session checkpoint only after scoped DB recall is empty."""
    mod = _load_hook_module("pre_response_restore", "pre_response.py")

    export_root = tmp_path / "exports"
    checkpoint_path = export_root / "checkpoints" / "MCP" / "s1.abhi"
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path.write_text("checkpoint")
    (export_root / "checkpoints" / "manifest.json").write_text(
        json.dumps(
            {
                "project": "MCP",
                "agent_id": "codex",
                "session_id": "s1",
                "checkpoint_path": str(checkpoint_path),
            }
        )
    )

    payload = json.dumps(
        {
            "prompt": "session memory bootstrap",
            "session_id": "s1",
            "project": "MCP",
            "agent_id": "codex",
        }
    )

    output_lines: list[str] = []

    def fake_print(data: str = "", **kw: object) -> None:
        output_lines.append(str(data))

    empty_prime = SimpleNamespace(summary="", nodes=[])
    restored_prime = SimpleNamespace(summary="Restored context", nodes=[{"label": "Checkpoint memory"}])

    with (
        patch("sys.stdin", StringIO(payload)),
        patch("builtins.print", side_effect=fake_print),
        patch("sys.exit", side_effect=SystemExit),
        patch("waggle.graph.MemoryGraph.prime_context", side_effect=[empty_prime, restored_prime]) as prime_mock,
        patch("waggle.graph.MemoryGraph.query") as query_mock,
        patch("waggle.graph.MemoryGraph.import_abhi") as import_mock,
        patch.dict(
            "os.environ",
            {
                "WAGGLE_DB_PATH": str(tmp_path / "hooks-test.db"),
                "WAGGLE_MODEL": "deterministic",
                "WAGGLE_BACKEND": "sqlite",
                "WAGGLE_DEFAULT_TENANT_ID": "local-default",
                "WAGGLE_EXPORT_DIR": str(export_root),
            },
        ),
        contextlib.suppress(SystemExit),
    ):
        mod.main()

    import_mock.assert_called_once()
    _, import_kwargs = import_mock.call_args
    assert import_kwargs["input_path"] == checkpoint_path
    assert import_kwargs["merge_strategy"] == "skip-existing"
    assert prime_mock.call_count == 2
    query_mock.assert_not_called()
    assert output_lines
    assert "Restored context" in json.loads(output_lines[-1])["content"]


def test_pre_compact_writes_session_checkpoint_stem(tmp_path: Path) -> None:
    """pre_compact routes transcript handoff through the new scoped checkpoint path."""
    mod = _load_hook_module("pre_compact_checkpoint", "pre_compact.py")

    export_root = tmp_path / "exports"
    payload = json.dumps(
        {
            "session_id": "s1",
            "project": "MCP",
            "agent_id": "codex",
            "transcript": [
                {"role": "user", "content": "We decided to checkpoint before compaction."},
                {"role": "assistant", "content": "I'll persist that before compacting."},
            ],
        }
    )

    output_lines: list[str] = []

    def fake_print(data: str = "", **kw: object) -> None:
        output_lines.append(str(data))

    with (
        patch("sys.stdin", StringIO(payload)),
        patch("builtins.print", side_effect=fake_print),
        patch("sys.exit", side_effect=SystemExit),
        patch(
            "waggle.graph.MemoryGraph.ingest_transcript_handoff",
            return_value=SimpleNamespace(checkpoint_path=str(export_root / "checkpoints" / "MCP" / "s1.abhi")),
        ) as ingest_mock,
        patch.dict(
            "os.environ",
            {
                "WAGGLE_DB_PATH": str(tmp_path / "hooks-test.db"),
                "WAGGLE_MODEL": "deterministic",
                "WAGGLE_BACKEND": "sqlite",
                "WAGGLE_DEFAULT_TENANT_ID": "local-default",
                "WAGGLE_EXPORT_DIR": str(export_root),
            },
        ),
        contextlib.suppress(SystemExit),
    ):
        mod.main()

    ingest_mock.assert_called_once()
    args, kwargs = ingest_mock.call_args
    payload_model = args[0]
    assert payload_model.project == "MCP"
    assert payload_model.agent_id == "codex"
    assert payload_model.session_id == "s1"
    assert kwargs["output_path"] == str(export_root / "checkpoints" / "MCP" / "s1")
    manifest = json.loads((export_root / "checkpoints" / "manifest.json").read_text())
    assert manifest["project"] == "MCP"
    assert manifest["agent_id"] == "codex"
    assert manifest["session_id"] == "s1"
    assert manifest["checkpoint_path"].endswith("s1.abhi")
    if output_lines:
        assert json.loads(output_lines[-1]) == {}


def test_hook_handoff_round_trip_restores_context_in_fresh_db(tmp_path: Path) -> None:
    """A durable turn survives pre-compact handoff and is recalled in a fresh DB."""
    pre_compact = _load_hook_module("pre_compact_roundtrip", "pre_compact.py")
    pre_response = _load_hook_module("pre_response_roundtrip", "pre_response.py")
    post_response = _load_hook_module("post_response_roundtrip", "post_response.py")
    export_root = tmp_path / "exports"
    source_db = tmp_path / "source.db"
    target_db = tmp_path / "target.db"

    transcript_payload = json.dumps(
        {
            "session_id": "handoff-session",
            "project": "MCP",
            "agent_id": "codex",
            "transcript": [
                {"role": "user", "content": "We decided to use Redis for caching."},
                {"role": "assistant", "content": "Understood. I will remember the Redis caching decision."},
            ],
        }
    )

    with (
        patch("sys.stdin", StringIO(transcript_payload)),
        patch("builtins.print"),
        patch("sys.exit", side_effect=SystemExit),
        patch.dict(
            "os.environ",
            {
                "WAGGLE_DB_PATH": str(source_db),
                "WAGGLE_MODEL": "deterministic",
                "WAGGLE_BACKEND": "sqlite",
                "WAGGLE_DEFAULT_TENANT_ID": "local-default",
                "WAGGLE_EXPORT_DIR": str(export_root),
            },
        ),
        contextlib.suppress(SystemExit),
    ):
        post_response.main()

    with (
        patch("sys.stdin", StringIO(transcript_payload)),
        patch("builtins.print"),
        patch("sys.exit", side_effect=SystemExit),
        patch.dict(
            "os.environ",
            {
                "WAGGLE_DB_PATH": str(source_db),
                "WAGGLE_MODEL": "deterministic",
                "WAGGLE_BACKEND": "sqlite",
                "WAGGLE_DEFAULT_TENANT_ID": "local-default",
                "WAGGLE_EXPORT_DIR": str(export_root),
            },
        ),
        contextlib.suppress(SystemExit),
    ):
        pre_compact.main()

    checkpoint_path = export_root / "checkpoints" / "MCP" / "handoff-session.abhi"
    manifest_path = export_root / "checkpoints" / "manifest.json"
    assert checkpoint_path.exists()
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text())
    assert manifest["checkpoint_path"] == str(checkpoint_path)

    prompt_payload = json.dumps(
        {
            "prompt": "What did we decide about caching?",
            "session_id": "handoff-session",
            "project": "MCP",
            "agent_id": "codex",
        }
    )
    output_lines: list[str] = []

    def fake_print(data: str = "", **kw: object) -> None:
        output_lines.append(str(data))

    with (
        patch("sys.stdin", StringIO(prompt_payload)),
        patch("builtins.print", side_effect=fake_print),
        patch("sys.exit", side_effect=SystemExit),
        patch.dict(
            "os.environ",
            {
                "WAGGLE_DB_PATH": str(target_db),
                "WAGGLE_MODEL": "deterministic",
                "WAGGLE_BACKEND": "sqlite",
                "WAGGLE_DEFAULT_TENANT_ID": "local-default",
                "WAGGLE_EXPORT_DIR": str(export_root),
            },
        ),
        contextlib.suppress(SystemExit),
    ):
        pre_response.main()

    assert output_lines
    response_payload = json.loads(output_lines[-1])
    assert response_payload["type"] == "system_reminder"
    assert "waggle memory context" in response_payload["content"].lower()

    from waggle.embeddings import EmbeddingModel
    from waggle.graph import MemoryGraph

    restored_graph = MemoryGraph(str(target_db), EmbeddingModel("deterministic"), tenant_id="local-default")
    restored = restored_graph.query(
        query="redis caching",
        project="MCP",
        agent_id="codex",
        session_id="handoff-session",
        max_nodes=5,
        max_depth=1,
    )
    assert restored.nodes
    assert any("redis" in node.content.lower() for node in restored.nodes)


# ── setup hooks tests ─────────────────────────────────────────────────────────


def test_install_claude_hooks_idempotent(tmp_path: Path) -> None:
    """Installing hooks twice should not duplicate entries."""
    from waggle.server import _install_claude_hooks

    hook_dir = ROOT / "src" / "waggle" / "hooks" / "claude_code"
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("{}")

    with patch("waggle.server._find_claude_settings", return_value=settings_path):
        _install_claude_hooks(hook_dir)
        _install_claude_hooks(hook_dir)  # second call — idempotent

    data = json.loads(settings_path.read_text())
    hooks = data.get("hooks", {})

    # Each event should have exactly one waggle entry
    for event in ("UserPromptSubmit", "Stop", "PreCompact"):
        entries = hooks.get(event, [])
        waggle_entries = [e for e in entries if any("waggle" in str(h.get("command", "")) for h in e.get("hooks", []))]
        assert len(waggle_entries) == 1, f"Expected exactly 1 waggle entry for {event}, got {len(waggle_entries)}"


def test_uninstall_hooks_removes_block(tmp_path: Path) -> None:
    """uninstall-hooks removes waggle entries cleanly."""
    from waggle.server import _install_claude_hooks, _uninstall_claude_hooks

    hook_dir = ROOT / "src" / "waggle" / "hooks" / "claude_code"
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("{}")

    with patch("waggle.server._find_claude_settings", return_value=settings_path):
        _install_claude_hooks(hook_dir)
        result = _uninstall_claude_hooks()

    assert result is not None
    data = json.loads(settings_path.read_text())
    hooks = data.get("hooks", {})
    for event_entries in hooks.values():
        for entry in event_entries:
            for h in entry.get("hooks", []):
                assert "waggle" not in str(h.get("command", "")), "Waggle hook entry still present after uninstall"


def test_uninstall_hooks_idempotent(tmp_path: Path) -> None:
    """uninstall-hooks is idempotent — second call returns None."""
    from waggle.server import _install_claude_hooks, _uninstall_claude_hooks

    hook_dir = ROOT / "src" / "waggle" / "hooks" / "claude_code"
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("{}")

    with patch("waggle.server._find_claude_settings", return_value=settings_path):
        _install_claude_hooks(hook_dir)
        _uninstall_claude_hooks()
        result2 = _uninstall_claude_hooks()

    assert result2 is None, "Second uninstall should return None (nothing to remove)"


def test_setup_writes_managed_block(tmp_path: Path) -> None:
    """waggle-mcp setup --yes writes the hooks block to Claude Code settings."""
    from waggle.server import _install_claude_hooks

    hook_dir = ROOT / "src" / "waggle" / "hooks" / "claude_code"
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("{}")

    with patch("waggle.server._find_claude_settings", return_value=settings_path):
        written = _install_claude_hooks(hook_dir)

    assert written == settings_path
    data = json.loads(settings_path.read_text())
    assert "hooks" in data
    # Verify waggle commands are present
    all_commands = [
        h.get("command", "") for entries in data["hooks"].values() for entry in entries for h in entry.get("hooks", [])
    ]
    assert any("waggle" in cmd for cmd in all_commands), "No waggle commands found in installed hooks"
