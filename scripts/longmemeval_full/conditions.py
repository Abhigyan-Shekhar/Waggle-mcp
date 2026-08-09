from __future__ import annotations

import time
import sys
import os
import re
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from scripts import run_longmemeval_waggle_phase as legacy

from .context_builder import (
    edge_to_context_item,
    enforce_context_budget,
    node_to_context_item,
    token_estimate,
    transcript_hit_to_context_item,
    trim_text_to_budget,
)
from .provenance import ConditionResult, ContextItem


FLAT_TRANSCRIPT_VECTOR = "flat_transcript_vector"
GRAPH_GUIDED_CONTEXT = "waggle_graph_guided_transcript_context"
PRODUCTION_CONTEXT = "waggle_production_context"
TEMPORAL_SLOT_CONTEXT = "waggle_temporal_slot_context"
AGENTIC_MCP = "waggle_agentic_mcp"
GRAPH_NODES_ONLY = "waggle_graph_nodes_only"
HYBRID_NO_EDGES = "waggle_hybrid_no_edges"
HYBRID_WITH_EDGES = "waggle_hybrid_with_edges"
CONTEXT_WITHOUT_TEMPORAL = "waggle_context_without_temporal_resolution"
CONTEXT_WITH_TEMPORAL = "waggle_context_with_temporal_resolution"
PRODUCTION_NO_PRIME = "waggle_production_context_no_prime"
PRODUCTION_WITH_PRIME = "waggle_production_context_with_prime"
ORACLE_SUPPORT_CONTEXT = "oracle_support_context"
ORACLE_ANSWER_TURN_CONTEXT = "oracle_answer_turn_context"
MEM0_CONTEXT = "mem0_context"
EXTERNAL_JSONL_PREFIX = "external_jsonl:"

ALL_CONDITIONS = [
    FLAT_TRANSCRIPT_VECTOR,
    GRAPH_GUIDED_CONTEXT,
    PRODUCTION_CONTEXT,
    TEMPORAL_SLOT_CONTEXT,
    AGENTIC_MCP,
    GRAPH_NODES_ONLY,
    HYBRID_NO_EDGES,
    HYBRID_WITH_EDGES,
    CONTEXT_WITHOUT_TEMPORAL,
    CONTEXT_WITH_TEMPORAL,
    PRODUCTION_NO_PRIME,
    PRODUCTION_WITH_PRIME,
    ORACLE_SUPPORT_CONTEXT,
    ORACLE_ANSWER_TURN_CONTEXT,
    MEM0_CONTEXT,
]


@dataclass(frozen=True)
class ConditionConfig:
    reader_context_budget: int = 4096
    retrieval_limit: int = 10
    max_tool_calls: int = 6
    related_depth: int = 1
    prime_budget: int = 512
    controller_factory: Callable[..., Any] | None = None
    external_context_path: Path | None = None
    mem0_context_path: Path | None = None


def normalize_condition(name: str) -> str:
    aliases = {
        "flat_vector": FLAT_TRANSCRIPT_VECTOR,
        "waggle_full": GRAPH_GUIDED_CONTEXT,
        "all": "all",
    }
    return aliases.get(name.strip(), name.strip())


def run_condition(
    condition: str,
    case: dict[str, Any],
    case_graph: dict[str, Any],
    *,
    config: ConditionConfig,
) -> ConditionResult:
    condition = normalize_condition(condition)
    if condition == FLAT_TRANSCRIPT_VECTOR:
        return flat_transcript_vector(case, case_graph, config=config)
    if condition == GRAPH_GUIDED_CONTEXT:
        return graph_guided_transcript_context(case, case_graph, config=config)
    if condition == PRODUCTION_CONTEXT:
        return production_context(
            case, case_graph, config=config, use_prime=False, temporal_resolution=True, condition_name=PRODUCTION_CONTEXT
        )
    if condition == TEMPORAL_SLOT_CONTEXT:
        return temporal_slot_context(case, case_graph, config=config)
    if condition == PRODUCTION_NO_PRIME:
        return production_context(
            case, case_graph, config=config, use_prime=False, temporal_resolution=True, condition_name=PRODUCTION_NO_PRIME
        )
    if condition == PRODUCTION_WITH_PRIME:
        return production_context(
            case, case_graph, config=config, use_prime=True, temporal_resolution=True, condition_name=PRODUCTION_WITH_PRIME
        )
    if condition == CONTEXT_WITHOUT_TEMPORAL:
        return production_context(
            case,
            case_graph,
            config=config,
            use_prime=False,
            temporal_resolution=False,
            condition_name=CONTEXT_WITHOUT_TEMPORAL,
        )
    if condition == CONTEXT_WITH_TEMPORAL:
        return production_context(
            case,
            case_graph,
            config=config,
            use_prime=False,
            temporal_resolution=True,
            condition_name=CONTEXT_WITH_TEMPORAL,
        )
    if condition == GRAPH_NODES_ONLY:
        return graph_nodes_only(case, case_graph, config=config)
    if condition == HYBRID_NO_EDGES:
        return hybrid_context(case, case_graph, config=config, include_edges=False)
    if condition == HYBRID_WITH_EDGES:
        return hybrid_context(case, case_graph, config=config, include_edges=True)
    if condition == AGENTIC_MCP:
        return agentic_mcp(case, case_graph, config=config)
    if condition == ORACLE_SUPPORT_CONTEXT:
        return oracle_support_context(case, case_graph, config=config)
    if condition == ORACLE_ANSWER_TURN_CONTEXT:
        return oracle_answer_turn_context(case, case_graph, config=config)
    if condition == MEM0_CONTEXT:
        return mem0_context(case, config=config)
    if condition.startswith(EXTERNAL_JSONL_PREFIX):
        return external_jsonl_context(condition, case, config=config)
    raise ValueError(f"unknown condition: {condition}")


_EXTERNAL_CONTEXT_CACHE: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}


def external_jsonl_context(condition: str, case: dict[str, Any], *, config: ConditionConfig) -> ConditionResult:
    """Use an externally-produced memory context while preserving this runner's QA path.

    JSONL rows must contain at least:
      {"case_id": "...", "system": "mem0", "context": "..."}

    Optional fields:
      context_items, retrieved_node_ids, retrieved_transcript_ids, retrieved_edge_ids,
      source_evidence_ids, retrieval_mode, adapter_notes, metadata
    """
    if config.external_context_path is None:
        raise ValueError("--external-context-path is required for external_jsonl:<system> conditions")
    system = condition[len(EXTERNAL_JSONL_PREFIX) :].strip()
    if not system:
        raise ValueError("external JSONL condition must be external_jsonl:<system>")
    case_id = _external_case_id(case)
    rows = _load_external_context_rows(config.external_context_path)
    payload = rows.get((case_id, system))
    if payload is None:
        raise KeyError(f"external context not found for case_id={case_id!r}, system={system!r}")
    return _context_from_external_payload(condition, system, case_id, payload, config=config)


def mem0_context(case: dict[str, Any], *, config: ConditionConfig) -> ConditionResult:
    """Use Mem0 OSS retrieval context as a first-class comparison condition."""
    if config.mem0_context_path is None:
        raise ValueError(
            "mem0_context requires a Mem0 context cache. Pass --mem0-context-cache-path, "
            "or let scripts.longmemeval_full.run create one for the current output dir."
        )
    case_id = _external_case_id(case)
    rows = _load_external_context_rows(config.mem0_context_path)
    payload = None
    for system in (MEM0_CONTEXT, "mem0", "mem0_oss_raw"):
        payload = rows.get((case_id, system))
        if payload is not None:
            return _context_from_external_payload(MEM0_CONTEXT, system, case_id, payload, config=config)
    raise KeyError(f"mem0 context not found for case_id={case_id!r} in {config.mem0_context_path}")


def _context_from_external_payload(
    condition: str,
    system: str,
    case_id: str,
    payload: dict[str, Any],
    *,
    config: ConditionConfig,
) -> ConditionResult:
    start = time.perf_counter()
    context = str(payload.get("context") or "")
    raw_items = payload.get("context_items")
    items: list[ContextItem]
    if isinstance(raw_items, list) and raw_items:
        items = [_external_context_item(raw_item, rank=index + 1, system=system) for index, raw_item in enumerate(raw_items)]
    else:
        items = [
            ContextItem(
                item_id=f"{system}:{case_id}:context",
                item_type="external_context",
                text=context,
                inclusion_reason=f"external_jsonl:{system}",
                token_count=token_estimate(context),
                rank=1,
                metadata=dict(payload.get("metadata") or {}),
            )
        ]
    context, items = enforce_context_budget("", items, config.reader_context_budget)
    return ConditionResult(
        condition=condition,
        context=context,
        context_items=items,
        retrieved_node_ids=[str(item) for item in payload.get("retrieved_node_ids") or []],
        retrieved_transcript_ids=[str(item) for item in payload.get("retrieved_transcript_ids") or []],
        retrieved_edge_ids=[str(item) for item in payload.get("retrieved_edge_ids") or []],
        source_evidence_ids=[str(item) for item in payload.get("source_evidence_ids") or []],
        retrieval_mode=str(payload.get("retrieval_mode") or f"external_jsonl:{system}"),
        latency_ms={"external_context_load": (time.perf_counter() - start) * 1000},
        adapter_notes=[
            f"Uses externally generated context for {system}; reader and judge are still controlled by this runner.",
            *[str(note) for note in payload.get("adapter_notes") or []],
        ],
    )


def _load_external_context_rows(path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    cache_key = str(path.resolve())
    if cache_key in _EXTERNAL_CONTEXT_CACHE:
        return _EXTERNAL_CONTEXT_CACHE[cache_key]
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            payload = json.loads(line)
            case_id = str(payload.get("case_id") or "").strip()
            system = str(payload.get("system") or "").strip()
            if not case_id or not system:
                raise ValueError(f"{path}:{line_number} must include case_id and system")
            if "context" not in payload and "context_items" not in payload:
                raise ValueError(f"{path}:{line_number} must include context or context_items")
            rows[(case_id, system)] = payload
    _EXTERNAL_CONTEXT_CACHE[cache_key] = rows
    return rows


def _external_case_id(case: dict[str, Any]) -> str:
    for key in ("question_id", "case_id", "id"):
        value = str(case.get(key) or "").strip()
        if value:
            return value
    raise ValueError("case is missing question_id/case_id/id")


def _external_context_item(raw_item: Any, *, rank: int, system: str) -> ContextItem:
    if not isinstance(raw_item, dict):
        text = str(raw_item)
        return ContextItem(
            item_id=f"{system}:item:{rank}",
            item_type="external_context_item",
            text=text,
            inclusion_reason=f"external_jsonl:{system}",
            token_count=token_estimate(text),
            rank=rank,
        )
    text = str(raw_item.get("text") or raw_item.get("content") or "")
    return ContextItem(
        item_id=str(raw_item.get("item_id") or raw_item.get("id") or f"{system}:item:{rank}"),
        item_type=str(raw_item.get("item_type") or "external_context_item"),
        text=text,
        inclusion_reason=str(raw_item.get("inclusion_reason") or f"external_jsonl:{system}"),
        source_node_id=str(raw_item.get("source_node_id") or ""),
        source_turn_id=str(raw_item.get("source_turn_id") or ""),
        token_count=int(raw_item.get("token_count") or token_estimate(text)),
        rank=int(raw_item.get("rank") or rank),
        metadata=dict(raw_item.get("metadata") or {}),
    )


def flat_transcript_vector(case: dict[str, Any], case_graph: dict[str, Any], *, config: ConditionConfig) -> ConditionResult:
    graph = case_graph["graph"]
    question = legacy._question(case)
    start = time.perf_counter()
    hits = graph.search_transcript_records(
        query=question,
        project=case_graph["project"],
        limit=max(1, config.retrieval_limit),
    )
    items = [
        transcript_hit_to_context_item(hit, rank=index + 1, reason="flat_transcript_vector")
        for index, hit in enumerate(hits)
    ]
    context, items = enforce_context_budget("", items, config.reader_context_budget)
    return ConditionResult(
        condition=FLAT_TRANSCRIPT_VECTOR,
        context=context,
        context_items=items,
        retrieved_transcript_ids=[item.item_id for item in items],
        retrieval_mode="transcript_vector",
        latency_ms={"retrieval": (time.perf_counter() - start) * 1000},
        adapter_notes=["Uses Waggle transcript store only; graph.query is intentionally not called."],
    )


def graph_guided_transcript_context(
    case: dict[str, Any], case_graph: dict[str, Any], *, config: ConditionConfig
) -> ConditionResult:
    start = time.perf_counter()
    context, session_ids, context_mode = legacy._context_from_waggle(
        case,
        condition="waggle_full",
        case_graph=case_graph,
        retrieval_limit=config.retrieval_limit,
    )
    item = ContextItem(
        item_id="legacy-context",
        item_type="legacy_context",
        text=context,
        inclusion_reason="historical_waggle_full_reproduction",
        token_count=token_estimate(context),
        metadata={"session_ids": session_ids, "legacy_context_mode": context_mode},
    )
    context, items = enforce_context_budget("", [item], config.reader_context_budget)
    return ConditionResult(
        condition=GRAPH_GUIDED_CONTEXT,
        context=context,
        context_items=items,
        retrieval_mode=f"graph_guided:{context_mode}",
        latency_ms={"retrieval_and_context": (time.perf_counter() - start) * 1000},
        adapter_notes=["Reproduces the historical waggle_full context path without renaming old artifacts."],
    )


def production_context(
    case: dict[str, Any],
    case_graph: dict[str, Any],
    *,
    config: ConditionConfig,
    use_prime: bool,
    temporal_resolution: bool,
    condition_name: str = PRODUCTION_CONTEXT,
) -> ConditionResult:
    graph = case_graph["graph"]
    question = legacy._question(case)
    project = case_graph["project"]
    agent_id = case_graph["agent_id"]
    start = time.perf_counter()
    tool_trace: list[dict[str, Any]] = []
    context_items: list[ContextItem] = []
    if use_prime:
        prime = graph.prime_context(project=project, agent_id=agent_id, max_nodes=max(1, config.retrieval_limit // 2))
        prime_nodes = list(getattr(prime, "nodes", []) or [])
        prime_items = [
            node_to_context_item(node, rank=index + 1, reason="prime_context")
            for index, node in enumerate(prime_nodes)
        ]
        prime_context, prime_items = enforce_context_budget("", prime_items, config.prime_budget)
        if prime_context:
            context_items.append(
                ContextItem(
                    item_id="prime_context",
                    item_type="prime",
                    text=f"Primed Memory:\n{prime_context}",
                    inclusion_reason="prime_context",
                    token_count=token_estimate(prime_context),
                    metadata={"node_count": len(prime_nodes)},
                )
            )
        tool_trace.append({"tool": "prime_context", "arguments": {"project": project}, "result_count": len(prime_nodes)})

    hybrid_debug: dict[str, Any] = {}
    try:
        hybrid_debug = graph.debug_retrieval(
            query=question,
            project=project,
            agent_id=agent_id,
            max_nodes=max(1, config.retrieval_limit),
            retrieval_mode="hybrid",
        )
    except Exception as exc:
        hybrid_debug = {"error": f"{type(exc).__name__}: {exc}"}
    tool_trace.append(
        {
            "tool": "debug_retrieval",
            "arguments": {"query": question, "project": project, "retrieval_mode": "hybrid"},
            "retrieval_layers": hybrid_debug.get("layers", {}),
            "hybrid_top_hits": hybrid_debug.get("hybrid_top_hits", []),
            "fused_top20": hybrid_debug.get("fused_top20", []),
            "error": hybrid_debug.get("error", ""),
        }
    )


    subgraph = graph.query(
        query=question,
        project=project,
        agent_id=agent_id,
        max_nodes=max(1, config.retrieval_limit),
        retrieval_mode="hybrid",
    )
    tool_trace.append(
        {
            "tool": "query_graph",
            "arguments": {"query": question, "project": project, "retrieval_mode": "hybrid"},
            "result_count": len(getattr(subgraph, "nodes", []) or []),
        }
    )

    controller_factory = config.controller_factory or _recursive_context_controller
    controller = controller_factory(graph)
    ablation = None if temporal_resolution else _ablation_config(conflict_resolve=False)
    built = controller.build_context(
        query=question,
        agent_id=agent_id,
        project=project,
        token_budget=config.reader_context_budget,
        depth=config.related_depth,
        include_evidence=True,
        mode="balanced",
        ablation=ablation,
    )
    built_context = str(getattr(built, "context_pack", "") or "")
    context_items.append(
        ContextItem(
            item_id="build_context",
            item_type="context_pack",
            text=built_context,
            inclusion_reason="build_context",
            token_count=token_estimate(built_context),
            metadata={
                "token_estimate": getattr(built, "token_estimate", 0),
                "debug": getattr(built, "debug", {}),
                "temporal_resolution": temporal_resolution,
            },
        )
    )
    nodes = list(getattr(built, "nodes_used", []) or []) or list(getattr(subgraph, "nodes", []) or [])
    edges = list(getattr(built, "edges_used", []) or []) or list(getattr(subgraph, "edges", []) or [])
    transcripts = list(getattr(built, "transcript_evidence", []) or [])
    context, context_items = enforce_context_budget("", context_items, config.reader_context_budget)
    return ConditionResult(
        condition=condition_name,
        context=context,
        context_items=context_items,
        retrieved_node_ids=[str(getattr(node, "id", "")) for node in nodes if getattr(node, "id", "")],
        retrieved_edge_ids=[str(getattr(edge, "id", "")) for edge in edges if getattr(edge, "id", "")],
        retrieved_transcript_ids=[
            str(getattr(hit, "record_id", "") or getattr(hit, "id", "")) for hit in transcripts if getattr(hit, "record_id", "") or getattr(hit, "id", "")
        ],
        source_evidence_ids=_evidence_ids(nodes),
        tool_trace=tool_trace,
        retrieval_mode="production_hybrid_build_context",
        latency_ms={"retrieval_and_context": (time.perf_counter() - start) * 1000},
        adapter_notes=[
            "Calls graph.query with retrieval_mode='hybrid' to match MCP query_graph default.",
            "Records graph.debug_retrieval layers so transcript, node, lexical, graph-expansion, and fused candidates can be audited.",
            "Uses RecursiveContextController.build_context as final context path.",
            "No large harness-selected session transcript append is added after build_context.",
        ],
    )


def temporal_slot_context(
    case: dict[str, Any],
    case_graph: dict[str, Any],
    *,
    config: ConditionConfig,
) -> ConditionResult:
    graph = case_graph["graph"]
    question = legacy._question(case)
    start = time.perf_counter()
    result = graph.temporal_slot_retriever().retrieve(
        query=question,
        project=case_graph["project"],
        agent_id=case_graph["agent_id"],
        max_context_tokens=config.reader_context_budget,
        reference_date=str(case.get("question_date") or case.get("questionDate") or ""),
    )
    evidence_items = [
        evidence
        for slot in result.plan.slots
        for evidence in result.assembled.per_slot.get(slot.name, [])
    ]
    compiled_text = trim_text_to_budget(result.context.text, config.reader_context_budget)
    context_items = [
        ContextItem(
            item_id=evidence.evidence_id,
            item_type="temporal_slot_evidence",
            text=evidence.content,
            inclusion_reason=f"required_slot:{evidence.slot}",
            source_node_id=evidence.node_ids[0] if evidence.node_ids else "",
            source_turn_id=evidence.turn_pair_id,
            token_count=token_estimate(evidence.content),
            rank=index + 1,
            metadata={
                "slot": evidence.slot,
                "score": evidence.score,
                "source": evidence.source,
                "node_ids": list(evidence.node_ids),
                "observed_at": evidence.observed_at.isoformat() if evidence.observed_at else "",
                "evidence_type": getattr(getattr(evidence, "evidence_type", None), "value", "fact"),
                "source_role": str(getattr(evidence, "source_role", "")),
                "structure": dict(getattr(evidence, "structure", {}) or {}),
            },
        )
        for index, evidence in enumerate(evidence_items)
    ]
    compiled_item = ContextItem(
        item_id="temporal_slot_compiled_context",
        item_type="compiled_evidence_context",
        text=compiled_text,
        inclusion_reason="compact_evidence_compiler",
        token_count=token_estimate(compiled_text),
        rank=1,
        metadata={
            "query_type": result.plan.query_type.value,
            "operation": result.plan.operation.value if result.plan.operation else "",
            "required_slots": [slot.name for slot in result.plan.slots if slot.required],
            "missing_slots": list(result.context.missing_slots),
            "calculation": (
                {
                    "operation": result.assembled.calculation.operation.value,
                    "operands": list(result.assembled.calculation.operands),
                    "result": result.assembled.calculation.result,
                    "unit": result.assembled.calculation.unit,
                    "expression": result.assembled.calculation.expression,
                }
                if result.assembled.calculation
                else {}
            ),
        },
    )
    node_ids = list(
        dict.fromkeys(node_id for evidence in evidence_items for node_id in evidence.node_ids)
    )
    turn_ids = list(
        dict.fromkeys(evidence.turn_pair_id for evidence in evidence_items if evidence.turn_pair_id)
    )
    return ConditionResult(
        condition=TEMPORAL_SLOT_CONTEXT,
        context=compiled_text,
        context_items=[compiled_item, *context_items],
        retrieved_node_ids=node_ids,
        retrieved_transcript_ids=turn_ids,
        tool_trace=[
            {
                "tool": "temporal_slot_retriever",
                "arguments": {
                    "query": question,
                    "project": case_graph["project"],
                    "max_context_tokens": config.reader_context_budget,
                    "reference_date": str(case.get("question_date") or case.get("questionDate") or ""),
                },
                "query_plan": {
                    "query_type": result.plan.query_type.value,
                    "operation": result.plan.operation.value if result.plan.operation else "",
                    "temporal_scope": result.plan.temporal_scope,
                    "confidence": result.plan.confidence,
                    "slots": [
                        {
                            "name": slot.name,
                            "query": slot.query,
                            "required": slot.required,
                            "collect_all": slot.collect_all,
                            "max_items": slot.max_items,
                            "min_items": getattr(slot, "min_items", 1),
                            "evidence_type": getattr(getattr(slot, "evidence_type", None), "value", "fact"),
                            "required_role": getattr(slot, "required_role", ""),
                            "target_index": getattr(slot, "target_index", None),
                            "target_key": getattr(slot, "target_key", ""),
                            "row_key": getattr(slot, "row_key", ""),
                            "required_terms": list(getattr(slot, "required_terms", ())),
                        }
                        for slot in result.plan.slots
                    ],
                },
                "retrieval_per_slot": result.retrieval_trace,
                "selected_per_slot": {
                    slot: [item.evidence_id for item in items]
                    for slot, items in result.assembled.per_slot.items()
                },
                "missing_slots": list(result.assembled.missing_slots),
                "dropped_duplicates": list(result.assembled.dropped_duplicates),
                "fallback_used": bool(getattr(result, "fallback_used", False)),
                "validation_issues": list(getattr(result, "validation_issues", ())),
                "active_set_members": list(getattr(result.assembled, "active_set_members", ())),
            }
        ],
        retrieval_mode="temporal_slot_hybrid_compact",
        latency_ms={"retrieval_and_context": (time.perf_counter() - start) * 1000},
        adapter_notes=[
            "Uses deterministic query planning and independent retrieval capacity per evidence slot.",
            "Uses local calculations only when all operands are complete and unambiguous.",
            "Legacy production context assembly is not appended after the compact compiler.",
        ],
    )


def graph_nodes_only(case: dict[str, Any], case_graph: dict[str, Any], *, config: ConditionConfig) -> ConditionResult:
    graph = case_graph["graph"]
    question = legacy._question(case)
    start = time.perf_counter()
    subgraph = graph.query(
        query=question,
        project=case_graph["project"],
        agent_id=case_graph["agent_id"],
        max_nodes=max(1, config.retrieval_limit),
        retrieval_mode="graph",
    )
    nodes = list(getattr(subgraph, "nodes", []) or [])
    items = [node_to_context_item(node, rank=index + 1, reason="graph_node_only") for index, node in enumerate(nodes)]
    context, items = enforce_context_budget("", items, config.reader_context_budget)
    return ConditionResult(
        condition=GRAPH_NODES_ONLY,
        context=context,
        context_items=items,
        retrieved_node_ids=[item.item_id for item in items],
        source_evidence_ids=_evidence_ids(nodes),
        retrieval_mode="graph_nodes_only",
        latency_ms={"retrieval": (time.perf_counter() - start) * 1000},
        adapter_notes=["No raw transcript recovery is included."],
    )


def hybrid_context(
    case: dict[str, Any], case_graph: dict[str, Any], *, config: ConditionConfig, include_edges: bool
) -> ConditionResult:
    graph = case_graph["graph"]
    question = legacy._question(case)
    start = time.perf_counter()
    hybrid_debug: dict[str, Any] = {}
    try:
        hybrid_debug = graph.debug_retrieval(
            query=question,
            project=case_graph["project"],
            agent_id=case_graph["agent_id"],
            max_nodes=max(1, config.retrieval_limit),
            retrieval_mode="hybrid",
        )
    except Exception as exc:
        hybrid_debug = {"error": f"{type(exc).__name__}: {exc}"}
    subgraph = graph.query(
        query=question,
        project=case_graph["project"],
        agent_id=case_graph["agent_id"],
        max_nodes=max(1, config.retrieval_limit),
        expand_depth=config.related_depth if include_edges else 0,
        retrieval_mode="hybrid",
    )
    nodes = list(getattr(subgraph, "nodes", []) or [])
    hits = list(getattr(subgraph, "hybrid_hits", []) or getattr(subgraph, "fusion_hits", []) or [])
    edges = list(getattr(subgraph, "edges", []) or []) if include_edges else []
    items: list[ContextItem] = []
    for index, node in enumerate(nodes):
        items.append(node_to_context_item(node, rank=index + 1, reason="hybrid_node"))
    offset = len(items)
    for index, hit in enumerate(hits):
        content = str(getattr(hit, "content", "") or "")
        node_ids = [str(value) for value in getattr(hit, "node_ids", []) or [] if value]
        item_id = str(getattr(hit, "id", "") or getattr(hit, "node_id", "") or getattr(hit, "turn_pair_id", "") or ",".join(node_ids))
        items.append(
            ContextItem(
                item_id=item_id,
                item_type="hybrid_hit",
                text=f"Hybrid hit [{item_id}]:\n{content}",
                inclusion_reason="hybrid_fusion",
                rank=offset + index + 1,
                metadata={
                    "source": str(getattr(hit, "source", "") or ""),
                    "turn_pair_id": str(getattr(hit, "turn_pair_id", "") or ""),
                    "node_ids": node_ids,
                    "score": float(getattr(hit, "score", 0.0) or 0.0),
                    "layer_scores": getattr(hit, "layer_scores", {}) or {},
                    "traceable": bool(item_id),
                },
            )
        )
    offset = len(items)
    for index, edge in enumerate(edges):
        items.append(edge_to_context_item(edge, rank=offset + index + 1))
    context, items = enforce_context_budget("", items, config.reader_context_budget)
    condition = HYBRID_WITH_EDGES if include_edges else HYBRID_NO_EDGES
    return ConditionResult(
        condition=condition,
        context=context,
        context_items=items,
        retrieved_node_ids=[str(getattr(node, "id", "")) for node in nodes if getattr(node, "id", "")],
        retrieved_edge_ids=[str(getattr(edge, "id", "")) for edge in edges if getattr(edge, "id", "")],
        tool_trace=[
            {
                "tool": "debug_retrieval",
                "arguments": {"query": question, "retrieval_mode": "hybrid"},
                "retrieval_layers": hybrid_debug.get("layers", {}),
                "hybrid_top_hits": hybrid_debug.get("hybrid_top_hits", []),
                "fused_top20": hybrid_debug.get("fused_top20", []),
                "error": hybrid_debug.get("error", ""),
            }
        ],
        retrieval_mode="hybrid_with_edges" if include_edges else "hybrid_no_edges",
        latency_ms={"retrieval": (time.perf_counter() - start) * 1000},
        adapter_notes=[
            "Uses graph.query retrieval_mode='hybrid'.",
            "Important validation gap: production HybridRetriever still computes graph_expansion internally; no-edge currently suppresses returned edge context rather than disabling that internal layer.",
        ],
    )


def agentic_mcp(case: dict[str, Any], case_graph: dict[str, Any], *, config: ConditionConfig) -> ConditionResult:
    graph = case_graph["graph"]
    question = legacy._question(case)
    project = case_graph["project"]
    agent_id = case_graph["agent_id"]
    start = time.perf_counter()
    calls = 0
    tool_trace: list[dict[str, Any]] = []
    items: list[ContextItem] = []

    if calls < config.max_tool_calls:
        prime = graph.prime_context(project=project, agent_id=agent_id, max_nodes=3)
        calls += 1
        nodes = list(getattr(prime, "nodes", []) or [])
        tool_trace.append({"tool": "prime_context", "arguments": {"project": project}, "result_count": len(nodes)})
        items.extend(node_to_context_item(node, rank=len(items) + 1, reason="agentic_prime") for node in nodes[:3])

    if calls < config.max_tool_calls:
        subgraph = graph.query(
            query=question,
            project=project,
            agent_id=agent_id,
            max_nodes=max(1, config.retrieval_limit),
            retrieval_mode="hybrid",
        )
        calls += 1
        nodes = list(getattr(subgraph, "nodes", []) or [])
        tool_trace.append({"tool": "query_graph", "arguments": {"query": question, "retrieval_mode": "hybrid"}, "result_count": len(nodes)})
        items.extend(node_to_context_item(node, rank=len(items) + 1, reason="agentic_query_graph") for node in nodes)
        if nodes and calls < config.max_tool_calls:
            related = graph.get_related(node_id=nodes[0].id, max_depth=config.related_depth)
            calls += 1
            related_nodes = list(getattr(related, "nodes", []) or [])
            related_edges = list(getattr(related, "edges", []) or [])
            tool_trace.append(
                {
                    "tool": "get_related",
                    "arguments": {"node_id": nodes[0].id, "max_depth": config.related_depth},
                    "result_count": len(related_nodes),
                }
            )
            items.extend(node_to_context_item(node, rank=len(items) + 1, reason="agentic_get_related") for node in related_nodes)
            items.extend(edge_to_context_item(edge, rank=len(items) + 1, reason="agentic_get_related") for edge in related_edges)

    context, items = enforce_context_budget("", items, config.reader_context_budget)
    return ConditionResult(
        condition=AGENTIC_MCP,
        context=context,
        context_items=items,
        retrieved_node_ids=[item.source_node_id for item in items if item.source_node_id],
        retrieved_edge_ids=[item.item_id for item in items if item.item_type == "edge"],
        tool_trace=tool_trace,
        retrieval_mode="bounded_agentic_mcp_simulation",
        latency_ms={"tool_use_and_context": (time.perf_counter() - start) * 1000},
        adapter_notes=[
            "Deterministic bounded MCP-equivalent tool runner for dry runs; no gold support IDs or category labels are supplied.",
            f"Maximum tool calls enforced: {config.max_tool_calls}.",
            "get_related production API is node-scoped; project/agent scope is inherited from the seed node selected by query_graph.",
        ],
    )


def oracle_support_context(case: dict[str, Any], case_graph: dict[str, Any], *, config: ConditionConfig) -> ConditionResult:
    gold_support_ids = set(legacy._gold_support_ids(case))
    records = []
    for session_id in gold_support_ids:
        records.extend(
            case_graph["graph"].list_transcript_records(
                project=case_graph["project"],
                session_id=session_id,
                limit=20,
            )
        )
    items = []
    for index, record in enumerate(records):
        item_id = str(getattr(record, "id", "") or f"oracle-{index + 1}")
        text = str(getattr(record, "transcript_text", "") or "")
        role = str(getattr(record, "role", "") or "")
        items.append(
            ContextItem(
                item_id=item_id,
                item_type="transcript",
                text=f"Oracle support transcript [{item_id}] role={role}:\n{text}",
                inclusion_reason="oracle_gold_support",
                source_turn_id=str(getattr(record, "turn_pair_id", "") or ""),
                rank=index + 1,
            )
        )
    context, items = enforce_context_budget("", items, config.reader_context_budget)
    return ConditionResult(
        condition=ORACLE_SUPPORT_CONTEXT,
        context=context,
        context_items=items,
        retrieved_transcript_ids=[item.item_id for item in items],
        retrieval_mode="oracle_gold_support",
        adapter_notes=["Uses gold support IDs and must be excluded from non-oracle headline comparisons."],
    )


def oracle_answer_turn_context(case: dict[str, Any], case_graph: dict[str, Any], *, config: ConditionConfig) -> ConditionResult:
    """Gold-support diagnostic that centers answer-bearing turns instead of raw session prefixes."""
    question = legacy._question(case)
    gold_support_ids = set(legacy._gold_support_ids(case))
    session_ids = list(case.get("haystack_session_ids") or [])
    sessions = list(case.get("haystack_sessions") or [])
    items: list[ContextItem] = []
    rank = 1

    for session_id, session in zip(session_ids, sessions):
        if str(session_id) not in gold_support_ids:
            continue
        turns = list(session or [])
        answer_indexes = [
            index
            for index, turn in enumerate(turns)
            if isinstance(turn, dict) and bool(turn.get("has_answer"))
        ]
        if not answer_indexes:
            answer_indexes = _oracle_query_relevant_turn_indexes(question, turns)
        for index in answer_indexes:
            item = _oracle_answer_turn_item(
                question=question,
                session_id=str(session_id),
                turns=turns,
                turn_index=index,
                rank=rank,
                reason="oracle_answer_turn" if bool(turns[index].get("has_answer")) else "oracle_query_centered_support",
            )
            items.append(item)
            rank += 1

    if not items:
        return oracle_support_context(case, case_graph, config=config)

    context, items = enforce_context_budget("", items, config.reader_context_budget)
    return ConditionResult(
        condition=ORACLE_ANSWER_TURN_CONTEXT,
        context=context,
        context_items=items,
        retrieved_transcript_ids=[item.item_id for item in items],
        retrieval_mode="oracle_answer_turn_gold_support",
        adapter_notes=[
            "Uses gold support IDs and must be excluded from non-oracle headline comparisons.",
            "Centers has_answer=True turns from gold support sessions before applying the context budget.",
            "Falls back to query-centered support turns only when no has_answer marker exists in a gold session.",
        ],
    )


def _oracle_answer_turn_item(
    *,
    question: str,
    session_id: str,
    turns: list[Any],
    turn_index: int,
    rank: int,
    reason: str,
) -> ContextItem:
    start = max(0, turn_index - 1)
    end = min(len(turns), turn_index + 2)
    lines = [f"Oracle answer-bearing evidence session={session_id} turn={turn_index}:"]
    ordered_indexes = [turn_index, *[index for index in range(start, end) if index != turn_index]]
    for index in ordered_indexes:
        turn = turns[index]
        if not isinstance(turn, dict):
            continue
        role = str(turn.get("role") or "")
        marker = " has_answer=true" if bool(turn.get("has_answer")) else ""
        content = _focused_oracle_turn_text(question, str(turn.get("content") or ""))
        lines.append(f"- turn {index} role={role}{marker}: {content}")
    text = "\n".join(lines).strip()
    item_id = f"{session_id}:{turn_index}"
    return ContextItem(
        item_id=item_id,
        item_type="oracle_answer_turn",
        text=text,
        inclusion_reason=reason,
        source_turn_id=item_id,
        rank=rank,
        metadata={"session_id": session_id, "turn_index": turn_index, "gold_oracle": True},
    )


def _oracle_query_relevant_turn_indexes(question: str, turns: list[Any], *, limit: int = 3) -> list[int]:
    terms = _oracle_query_terms(question)
    scored: list[tuple[int, int]] = []
    for index, turn in enumerate(turns):
        if not isinstance(turn, dict):
            continue
        text = str(turn.get("content") or "").lower()
        overlap = len(terms.intersection(set(re.findall(r"[a-z0-9]+", text))))
        if overlap:
            scored.append((overlap, index))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [index for _score, index in scored[:limit]]


def _oracle_query_terms(question: str) -> set[str]:
    stop = {
        "a",
        "an",
        "about",
        "and",
        "are",
        "did",
        "do",
        "does",
        "for",
        "how",
        "i",
        "is",
        "it",
        "me",
        "my",
        "of",
        "or",
        "the",
        "to",
        "was",
        "what",
        "when",
        "where",
        "which",
        "with",
    }
    return {term for term in re.findall(r"[a-z0-9]+", question.lower()) if len(term) > 2 and term not in stop}


def _compact_oracle_turn_text(text: str, *, max_chars: int = 1400) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 1].rstrip()}…"


def _focused_oracle_turn_text(question: str, text: str, *, max_chars: int = 700) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return text
    lower = text.lower()
    focus_position = -1
    for term in sorted(_oracle_query_terms(question), key=lambda value: (-len(value), value)):
        position = lower.find(term)
        if position >= 0:
            focus_position = position
            break
    if focus_position < 0:
        return _compact_oracle_turn_text(text, max_chars=max_chars)
    center = focus_position
    start = max(0, center - max_chars // 8)
    end = min(len(text), start + max_chars)
    start = max(0, end - max_chars)
    snippet = text[start:end].strip()
    if start > 0:
        snippet = f"…{snippet}"
    if end < len(text):
        snippet = f"{snippet}…"
    return snippet


def _evidence_ids(nodes: list[Any]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for node in nodes:
        for record in getattr(node, "evidence_records", []) or []:
            evidence_id = str(getattr(record, "evidence_id", "") or "")
            if evidence_id and evidence_id not in seen:
                seen.add(evidence_id)
                output.append(evidence_id)
    return output


def _recursive_context_controller(graph: Any) -> Any:
    os.environ.setdefault("PYDANTIC_DISABLE_PLUGINS", "1")
    from waggle.recursive_context import RecursiveContextController

    return RecursiveContextController(graph)


def _ablation_config(**kwargs: Any) -> Any:
    os.environ.setdefault("PYDANTIC_DISABLE_PLUGINS", "1")
    from waggle.recursive_context import AblationConfig

    return AblationConfig(**kwargs)
