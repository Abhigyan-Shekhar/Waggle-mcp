from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


def stable_json_sha(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except Exception:
        return "unknown"


@dataclass
class ContextItem:
    item_id: str
    item_type: str
    text: str
    inclusion_reason: str
    source_node_id: str = ""
    source_turn_id: str = ""
    token_count: int = 0
    rank: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ConditionResult:
    condition: str
    context: str
    context_items: list[ContextItem]
    retrieved_node_ids: list[str] = field(default_factory=list)
    retrieved_transcript_ids: list[str] = field(default_factory=list)
    retrieved_edge_ids: list[str] = field(default_factory=list)
    source_evidence_ids: list[str] = field(default_factory=list)
    tool_trace: list[dict[str, Any]] = field(default_factory=list)
    retrieval_mode: str = ""
    latency_ms: dict[str, float] = field(default_factory=dict)
    adapter_notes: list[str] = field(default_factory=list)

    def to_row_payload(self) -> dict[str, Any]:
        return {
            "condition": self.condition,
            "retrieval_mode": self.retrieval_mode,
            "retrieved_node_ids": self.retrieved_node_ids,
            "retrieved_transcript_ids": self.retrieved_transcript_ids,
            "retrieved_edge_ids": self.retrieved_edge_ids,
            "source_evidence_ids": self.source_evidence_ids,
            "tool_trace": self.tool_trace,
            "context_items": [item.to_dict() for item in self.context_items],
            "latency_ms": self.latency_ms,
            "adapter_notes": self.adapter_notes,
        }
