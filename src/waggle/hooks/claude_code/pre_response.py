#!/usr/bin/env python3
"""
Waggle Claude Code hook: UserPromptSubmit (pre-response).

Triggered before Claude responds to a user prompt.

Routing logic:
  - Concrete task / question  → build_context (recursive context assembly)
  - Session start / no query  → prime_context
  - Any failure               → fallback to query_graph, then silent exit

Protocol: reads JSON from stdin, writes JSON to stdout.
Always exits 0 — never blocks the user's session.
Timeout: 5 seconds.
"""

from __future__ import annotations

import json
import re
import signal
import sys
from pathlib import Path
from typing import Any

# Ensure waggle src is importable when run as a script
_HERE = Path(__file__).resolve()
for _candidate in [
    _HERE.parents[4] / "src",  # repo layout: src/waggle/hooks/claude_code/
    _HERE.parents[3],  # installed package
]:
    if (_candidate / "waggle").exists() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))
        break

_TIMEOUT_SECONDS = 5

# Heuristic: prompts that look like concrete tasks benefit from build_context
_TASK_PATTERN = re.compile(
    r"\b(build|implement|continue|finish|fix|debug|add|create|update|deploy|"
    r"explain|how|what|why|where|when|show|list|find|get|run|test|review|"
    r"refactor|optimize|help|write|generate|analyse|analyze)\b",
    re.IGNORECASE,
)


def _timeout_handler(signum: int, frame: Any) -> None:
    raise TimeoutError("Waggle pre_response hook timed out")


def _silent_exit() -> None:
    """Exit 0 with empty output — never block the user."""
    print(json.dumps({}))
    sys.exit(0)


def _is_concrete_task(prompt: str) -> bool:
    """Return True if the prompt looks like a concrete task or question."""
    return bool(_TASK_PATTERN.search(prompt)) and len(prompt.split()) >= 3


def _has_recursive_signal(result: Any) -> bool:
    return bool(
        getattr(result, "nodes_used", None)
        or getattr(result, "transcript_evidence", None)
        or getattr(result, "edges_used", None)
        or getattr(result, "conflicts", None)
    )


def main() -> None:
    previous_handler: Any = None
    previous_alarm = 0
    has_sigalrm = hasattr(signal, "SIGALRM")
    if has_sigalrm:
        previous_handler = signal.getsignal(signal.SIGALRM)
        previous_alarm = signal.alarm(0)
        signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(_TIMEOUT_SECONDS)

    try:
        raw = sys.stdin.read()
        if not raw.strip():
            _silent_exit()

        payload: dict[str, Any] = json.loads(raw)
        prompt: str = payload.get("prompt", "") or ""
        explicit_checkpoint_path: str = str(payload.get("checkpoint_path", "") or "")

        if not prompt.strip():
            _silent_exit()

        # Import waggle in-process for low latency
        from waggle.config import AppConfig
        from waggle.embeddings import EmbeddingModel
        from waggle.graph import MemoryGraph
        from waggle.hooks.claude_code.common import checkpoint_path, read_checkpoint_manifest, resolve_scope
        from waggle.recursive_context import RECURSIVE_CONTEXT_ENABLED, RecursiveContextController

        config = AppConfig.from_env()
        if config.backend != "sqlite":
            _silent_exit()

        scope = resolve_scope(payload, config)

        graph = MemoryGraph(
            config.db_path,
            EmbeddingModel(
                config.model_name,
                embedding_backend=config.embedding_backend,
            ),
            tenant_id=config.default_tenant_id,
        )

        restored_checkpoint_path = read_checkpoint_manifest(
            config=config,
            project=scope["project"],
            agent_id=scope["agent_id"],
            session_id=scope["session_id"],
        )
        active_checkpoint_path = restored_checkpoint_path or checkpoint_path(
            config=config,
            project=scope["project"],
            session_id=scope["session_id"],
            explicit_path=explicit_checkpoint_path,
        )
        context_text = ""

        def load_from_db(*, include_query_fallback: bool) -> str:
            local_context_text = ""
            if RECURSIVE_CONTEXT_ENABLED and _is_concrete_task(prompt):
                try:
                    controller = RecursiveContextController(graph=graph)
                    ctx_result = controller.build_context(
                        query=prompt[:500],
                        project=scope["project"],
                        agent_id=scope["agent_id"],
                        session_id=scope["session_id"],
                        token_budget=800,
                        depth=1,
                        max_subqueries=4,
                        mode="fast",
                    )
                    if _has_recursive_signal(ctx_result):
                        local_context_text = ctx_result.context_pack or ""
                except Exception:
                    local_context_text = ""

            if not local_context_text:
                try:
                    result = graph.prime_context(
                        project=scope["project"],
                        agent_id=scope["agent_id"],
                        session_id=scope["session_id"],
                    )
                    local_context_text = result.summary if result.summary and result.nodes else ""
                except Exception:
                    local_context_text = ""

            if not local_context_text and include_query_fallback:
                try:
                    qr = graph.query(
                        query=prompt[:500],
                        max_nodes=8,
                        max_depth=1,
                        project=scope["project"],
                        agent_id=scope["agent_id"],
                        session_id=scope["session_id"],
                    )
                    if qr.nodes:
                        local_context_text = "\n".join(
                            f"[{n.node_type.value}] {n.label}: {n.content[:200]}" for n in qr.nodes[:5]
                        )
                except Exception:
                    local_context_text = ""
            return local_context_text

        context_text = load_from_db(include_query_fallback=False)

        if not context_text and active_checkpoint_path is not None and active_checkpoint_path.exists():
            try:
                graph.import_abhi(
                    input_path=active_checkpoint_path,
                    merge_strategy="skip-existing",
                )
                context_text = load_from_db(include_query_fallback=True)
            except Exception:
                context_text = ""
        elif not context_text:
            context_text = load_from_db(include_query_fallback=True)

        if context_text:
            print(
                json.dumps(
                    {
                        "type": "system_reminder",
                        "content": f"[Waggle memory context]\n{context_text}",
                    }
                )
            )
        else:
            print(json.dumps({}))

    except (TimeoutError, Exception):
        _silent_exit()
    finally:
        if has_sigalrm:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous_handler)
            if previous_alarm > 0:
                signal.alarm(previous_alarm)


if __name__ == "__main__":
    main()
