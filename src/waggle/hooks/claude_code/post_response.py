#!/usr/bin/env python3
"""
Waggle Claude Code hook: Stop (post-response).

Triggered after Claude finishes responding.
Calls observe_conversation to persist the last user/assistant turn when the
turn matches Waggle's durable-ingest policy.

Protocol: reads JSON from stdin, writes JSON to stdout.
Always exits 0 — never blocks the user's session.
Timeout: 5 seconds.
Skips capture when likely secrets are detected in the turn text.
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
    _HERE.parents[4] / "src",
    _HERE.parents[3],
]:
    if (_candidate / "waggle").exists() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))
        break

_TIMEOUT_SECONDS = 5

# Secret patterns — mirrors _EXPORT_SECRET_PATTERNS in server.py
_SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9._-]{8,}\.[A-Za-z0-9._-]{8,}\b"),
    re.compile(r"(?i)\b(password|passwd|pwd)\b\s*[:=]\s*['\"]?\S+"),
    re.compile(r"(?i)\b(api[_ -]?key|secret[_ -]?key|access[_ -]?token)\b\s*[:=]\s*['\"]?\S+"),
]


def _contains_secret(text: str) -> bool:
    return any(p.search(text) for p in _SECRET_PATTERNS)


def _timeout_handler(signum: int, frame: Any) -> None:
    raise TimeoutError("Waggle post_response hook timed out")


def _silent_exit() -> None:
    print(json.dumps({}))
    sys.exit(0)


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

        # Extract the last user/assistant turn from the transcript
        transcript: list[dict[str, Any]] = payload.get("transcript", []) or []
        session_id: str = str(payload.get("session_id", "") or "")

        user_msg = ""
        assistant_msg = ""

        # Walk transcript in reverse to find the last user→assistant pair
        for i in range(len(transcript) - 1, -1, -1):
            entry = transcript[i]
            role = str(entry.get("role", "")).lower()
            content = str(entry.get("content", "") or "")
            if role == "assistant" and not assistant_msg:
                assistant_msg = content
            elif role == "user" and not user_msg and assistant_msg:
                user_msg = content
                break

        if not user_msg or not assistant_msg:
            _silent_exit()

        # Skip if secrets detected
        if _contains_secret(user_msg) or _contains_secret(assistant_msg):
            _silent_exit()

        from waggle.config import AppConfig
        from waggle.embeddings import EmbeddingModel
        from waggle.graph import MemoryGraph
        from waggle.hooks.claude_code.common import resolve_scope
        from waggle.orchestrator import ConversationTurn, MemoryPolicy, MemoryScope

        config = AppConfig.from_env()
        if config.backend != "sqlite":
            _silent_exit()

        scope_data = resolve_scope(payload, config)
        scope = MemoryScope(
            tenant_id=scope_data["tenant_id"],
            project=scope_data["project"],
            agent_id=scope_data["agent_id"],
            session_id=scope_data["session_id"],
        )
        plan = MemoryPolicy().plan_ingest(
            ConversationTurn(
                user_message=user_msg[:4000],
                assistant_response=assistant_msg[:4000],
            ),
            scope,
        )
        if not plan.should_ingest:
            _silent_exit()

        graph = MemoryGraph(
            config.db_path,
            EmbeddingModel(
                config.model_name,
                embedding_backend=config.embedding_backend,
            ),
            tenant_id=config.default_tenant_id,
        )

        graph.observe_conversation(
            user_message=user_msg[:4000],
            assistant_response=assistant_msg[:4000],
            project=scope.project,
            agent_id=scope.agent_id,
            session_id=session_id,
        )

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
