from __future__ import annotations

import re
from typing import Any

from .provenance import ContextItem


def token_estimate(text: str) -> int:
    tokens = re.findall(r"\w+|[^\w\s]", text or "", flags=re.UNICODE)
    return max(0, int(len(tokens) * 1.25))


def trim_text_to_budget(text: str, budget: int) -> str:
    if budget <= 0:
        return ""
    if token_estimate(text) <= budget:
        return text
    words = re.findall(r"\S+", text or "")
    # Conservative binary search using the same estimator.
    lo, hi = 0, len(words)
    best = ""
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = " ".join(words[:mid])
        if token_estimate(candidate) <= budget:
            best = candidate
            lo = mid + 1
        else:
            hi = mid - 1
    return best.rstrip()


def enforce_context_budget(context: str, items: list[ContextItem], budget: int) -> tuple[str, list[ContextItem]]:
    if budget <= 0:
        return "", []
    output_parts: list[str] = []
    output_items: list[ContextItem] = []
    used = 0
    for item in items:
        text = item.text.strip()
        if not text:
            continue
        prefix = "" if not output_parts else "\n\n"
        candidate = f"{prefix}{text}"
        candidate_tokens = token_estimate(candidate)
        remaining = budget - used
        if candidate_tokens <= remaining:
            copied = ContextItem(**item.to_dict())
            copied.token_count = candidate_tokens
            output_parts.append(text)
            output_items.append(copied)
            used += candidate_tokens
            continue
        trimmed = trim_text_to_budget(candidate, remaining)
        if trimmed:
            copied = ContextItem(**item.to_dict())
            copied.text = trimmed.strip()
            copied.token_count = token_estimate(trimmed)
            output_parts.append(copied.text)
            output_items.append(copied)
        break
    return "\n\n".join(output_parts).strip(), output_items


def object_id(value: Any) -> str:
    return str(getattr(value, "id", "") or getattr(value, "node_id", "") or getattr(value, "evidence_id", "") or "")


def node_to_context_item(node: Any, *, rank: int, reason: str = "semantic_hit") -> ContextItem:
    node_id = object_id(node)
    label = str(getattr(node, "label", "") or "")
    content = str(getattr(node, "content", "") or "")
    node_type = str(getattr(getattr(node, "type", ""), "value", getattr(node, "type", "")) or "")
    evidence_records = list(getattr(node, "evidence_records", []) or [])
    source_turn_id = str(getattr(node, "source_turn_pair_id", "") or "")
    evidence_ids = [str(getattr(record, "evidence_id", "") or "") for record in evidence_records]
    text = f"Memory node [{node_id}] ({node_type}): {label}\n{content}".strip()
    return ContextItem(
        item_id=node_id,
        item_type="node",
        text=text,
        inclusion_reason=reason,
        source_node_id=node_id,
        source_turn_id=source_turn_id,
        rank=rank,
        metadata={"node_type": node_type, "evidence_ids": [item for item in evidence_ids if item]},
    )


def transcript_hit_to_context_item(hit: Any, *, rank: int, reason: str = "semantic_hit") -> ContextItem:
    session_id = str(getattr(hit, "session_id", "") or "")
    role = str(getattr(hit, "role", "") or "")
    turn_index = str(getattr(hit, "turn_index", "") if getattr(hit, "turn_index", "") is not None else "")
    hit_id = str(getattr(hit, "record_id", "") or getattr(hit, "id", "") or getattr(hit, "turn_pair_id", "") or "")
    if not hit_id and session_id:
        hit_id = f"{session_id}:{turn_index}:{role}".strip(":")
    text_value = str(getattr(hit, "transcript_text", "") or getattr(hit, "transcript_snippet", "") or "")
    text = f"Transcript [{hit_id}]"
    if session_id:
        text += f" session={session_id}"
    if role:
        text += f" role={role}"
    text += f":\n{text_value}"
    return ContextItem(
        item_id=hit_id,
        item_type="transcript",
        text=text,
        inclusion_reason=reason,
        source_turn_id=str(getattr(hit, "turn_pair_id", "") or ""),
        rank=rank,
        metadata={
            "session_id": session_id,
            "role": role,
            "turn_index": turn_index,
            "score": float(getattr(hit, "score", 0.0) or 0.0),
            "traceable": bool(hit_id),
        },
    )


def edge_to_context_item(edge: Any, *, rank: int, reason: str = "edge_expansion") -> ContextItem:
    relation = str(getattr(getattr(edge, "relationship", ""), "value", getattr(edge, "relationship", "")) or "")
    source_id = str(getattr(edge, "source_id", "") or "")
    target_id = str(getattr(edge, "target_id", "") or "")
    edge_id = object_id(edge) or f"{source_id}:{relation}:{target_id}"
    return ContextItem(
        item_id=edge_id,
        item_type="edge",
        text=f"Graph edge [{edge_id}]: {source_id} --{relation}--> {target_id}",
        inclusion_reason=reason,
        rank=rank,
        metadata={"relationship": relation, "source_id": source_id, "target_id": target_id},
    )
