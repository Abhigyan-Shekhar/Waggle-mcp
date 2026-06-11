#!/usr/bin/env python3
"""
Waggle Claude Code hook: PreCompact (before context compression).

Triggered before Claude compresses the context window.
Calls ingest_transcript_handoff to preserve durable info before compaction.

Protocol: reads JSON from stdin, writes JSON to stdout.
Always exits 0 — never blocks the user's session.
Timeout: 5 seconds.
"""

from __future__ import annotations

import json
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


def _timeout_handler(signum: int, frame: Any) -> None:
    raise TimeoutError("Waggle pre_compact hook timed out")


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
        transcript: list[dict[str, Any]] = payload.get("transcript", []) or []

        if not transcript:
            _silent_exit()

        from waggle.config import AppConfig
        from waggle.embeddings import EmbeddingModel
        from waggle.graph import MemoryGraph
        from waggle.hooks.claude_code.common import checkpoint_stem, resolve_scope, write_checkpoint_manifest
        from waggle.models import TranscriptIngestionInput, TranscriptMessage

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

        # Convert transcript to TranscriptMessage list
        messages: list[TranscriptMessage] = []
        for entry in transcript:
            role = str(entry.get("role", "")).lower()
            content = str(entry.get("content", "") or "")
            if role in ("user", "assistant") and content.strip():
                messages.append(TranscriptMessage(role=role, content=content[:4000]))

        if not messages:
            _silent_exit()

        output_stem = checkpoint_stem(
            config=config,
            project=scope["project"],
            session_id=scope["session_id"],
        )
        payload_model = TranscriptIngestionInput(
            messages=messages,
            project=scope["project"],
            agent_id=scope["agent_id"],
            session_id=scope["session_id"],
        )
        result = graph.ingest_transcript_handoff(
            payload_model,
            output_path=str(output_stem),
        )
        if result.checkpoint_path:
            write_checkpoint_manifest(
                config=config,
                project=scope["project"],
                agent_id=scope["agent_id"],
                session_id=scope["session_id"],
                checkpoint_path=result.checkpoint_path,
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
