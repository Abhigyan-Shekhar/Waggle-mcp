#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import plan_longmemeval_run
import run_longmemeval_waggle_phase as waggle_runner


def _case_id(case: dict[str, Any], index: int) -> str:
    return plan_longmemeval_run._case_id(case, index)


def _find_case(dataset: Path, case_id: str) -> dict[str, Any]:
    cases = plan_longmemeval_run._load_cases(dataset)
    for index, case in enumerate(cases, start=1):
        if _case_id(case, index) == case_id:
            return case
    raise ValueError(f"case_id {case_id!r} not found in {dataset}")


def _json_list(raw: str | None) -> list[Any]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []


def _node_rows(db_path: Path, *, project: str) -> list[dict[str, Any]]:
    with sqlite3.connect(str(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT id, session_id, label, content, node_type, tags, source_prompt, evidence_records
            FROM nodes
            WHERE project = ?
            ORDER BY session_id ASC, node_type ASC, label ASC
            """,
            (project,),
        ).fetchall()
    output: list[dict[str, Any]] = []
    for row in rows:
        output.append(
            {
                "id": row["id"],
                "session_id": row["session_id"],
                "label": row["label"],
                "content": row["content"],
                "node_type": row["node_type"],
                "tags": _json_list(row["tags"]),
                "source_prompt": row["source_prompt"],
                "evidence_records": _json_list(row["evidence_records"]),
            }
        )
    return output


def _shorten(text: str, limit: int = 500) -> str:
    normalized = " ".join(str(text).split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."


def _node_payload(node: Any) -> dict[str, Any]:
    return {
        "id": node.id,
        "session_id": node.session_id,
        "node_type": str(node.node_type),
        "label": node.label,
        "content": node.content,
        "tags": list(node.tags),
        "evidence_session_ids": [
            record.session_id for record in getattr(node, "evidence_records", []) if getattr(record, "session_id", "")
        ],
    }


def _edge_payload(edge: Any) -> dict[str, Any]:
    return {
        "source_id": edge.source_id,
        "target_id": edge.target_id,
        "relationship": edge.relationship,
        "weight": edge.weight,
        "metadata": edge.metadata,
    }


def _flat_payload(case: dict[str, Any], case_graph: dict[str, Any], *, retrieval_limit: int) -> dict[str, Any]:
    graph = case_graph["graph"]
    hits = graph.search_transcript_records(
        query=waggle_runner._question(case),
        project=case_graph["project"],
        limit=max(1, retrieval_limit * 4),
    )
    rendered_context, retrieved_ids, context_mode = waggle_runner._context_from_waggle(
        case,
        condition="flat_vector",
        case_graph=case_graph,
        retrieval_limit=retrieval_limit,
    )
    return {
        "retrieved_support_ids": retrieved_ids,
        "context_mode": context_mode,
        "hits": [
            {
                "session_id": hit.session_id,
                "role": hit.role,
                "score": hit.score,
                "turn_index": hit.turn_index,
                "turn_pair_id": hit.turn_pair_id,
                "snippet": _shorten(hit.transcript_text, 700),
            }
            for hit in hits[:retrieval_limit]
        ],
        "context": rendered_context,
        "prompt": waggle_runner._build_prompt(case, "flat_vector", rendered_context),
    }


def _waggle_payload(case: dict[str, Any], case_graph: dict[str, Any], *, retrieval_limit: int) -> dict[str, Any]:
    graph = case_graph["graph"]
    subgraph = graph.query(
        query=waggle_runner._question(case),
        project=case_graph["project"],
        agent_id=case_graph["agent_id"],
        max_nodes=max(1, retrieval_limit * 2),
        retrieval_mode="graph",
    )
    rendered_context, retrieved_ids, context_mode = waggle_runner._context_from_waggle(
        case,
        condition="waggle_full",
        case_graph=case_graph,
        retrieval_limit=retrieval_limit,
    )
    return {
        "retrieved_support_ids": retrieved_ids,
        "context_mode": context_mode,
        "nodes": [_node_payload(node) for node in subgraph.nodes],
        "edges": [_edge_payload(edge) for edge in subgraph.edges],
        "context": rendered_context,
        "prompt": waggle_runner._build_prompt(case, "waggle_full", rendered_context),
    }


def _write_markdown(path: Path, trace: dict[str, Any]) -> None:
    node_counts = trace["ingestion"]["node_type_counts"]
    lines = [
        f"# LongMemEval Waggle Trace: {trace['case_id']}",
        "",
        f"Question: {trace['question']}",
        "",
        f"Question type: `{trace['question_type']}`",
        "",
        f"Gold support IDs: `{trace['gold_support_ids']}`",
        "",
        "## Ingestion",
        "",
        f"Node type counts: `{node_counts}`",
        "",
        "Gold-session nodes:",
    ]
    for node in trace["ingestion"]["gold_session_nodes"]:
        lines.append(f"- `{node['node_type']}` `{node['session_id']}` {node['label']}: {_shorten(node['content'], 260)}")
    lines.extend(["", "Preference-like nodes:"])
    for node in trace["ingestion"]["preference_like_nodes"]:
        lines.append(f"- `{node['node_type']}` `{node['session_id']}` {node['label']}: {_shorten(node['content'], 260)}")
    lines.extend(["", "## Flat Payload", ""])
    lines.append(f"Retrieved support IDs: `{trace['flat_vector']['retrieved_support_ids']}`")
    for hit in trace["flat_vector"]["hits"]:
        lines.append(f"- `{hit['session_id']}` score={hit['score']:.4f} {hit['role']}: {_shorten(hit['snippet'], 260)}")
    lines.extend(["", "## Waggle Payload", ""])
    lines.append(f"Retrieved support IDs: `{trace['waggle_full']['retrieved_support_ids']}`")
    lines.append("Retrieved nodes:")
    for node in trace["waggle_full"]["nodes"]:
        lines.append(f"- `{node['node_type']}` `{node['session_id']}` {node['label']}: {_shorten(node['content'], 260)}")
    lines.extend(["", "## Final Prompt Shape", ""])
    lines.append("Flat prompt uses source chunks only. Waggle prompt preserves structured memory sections plus source chunks.")
    lines.append("")
    lines.append("### Flat Prompt Excerpt")
    lines.append("```text")
    lines.append(_shorten(trace["flat_vector"]["prompt"], 1800))
    lines.append("```")
    lines.append("")
    lines.append("### Waggle Prompt Excerpt")
    lines.append("```text")
    lines.append(_shorten(trace["waggle_full"]["prompt"], 1800))
    lines.append("```")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def trace_case(
    *,
    dataset: Path,
    case_id: str,
    output_dir: Path,
    embedding_model_name: str,
    retrieval_limit: int,
) -> dict[str, Any]:
    case = _find_case(dataset, case_id)
    EmbeddingModel, _ = waggle_runner._load_waggle_classes()
    case_graph = waggle_runner._build_case_graph(
        case,
        embedding_model=EmbeddingModel(embedding_model_name),
        agent_id=waggle_runner.DEFAULT_AGENT_ID,
    )
    try:
        all_nodes = _node_rows(Path(case_graph["graph"].db_path), project=case_graph["project"])
        gold_ids = waggle_runner._gold_support_ids(case)
        gold_nodes = [node for node in all_nodes if node["session_id"] in set(gold_ids)]
        preference_like_nodes = [
            node
            for node in all_nodes
            if node["node_type"] == "preference"
            or "preference" in " ".join(str(tag).lower() for tag in node["tags"])
            or any(token in f"{node['label']} {node['content']}".lower() for token in ("prefer", "preference", "would like"))
        ]
        trace = {
            "case_id": case_id,
            "question": waggle_runner._question(case),
            "question_type": str(case.get("question_type") or case.get("category") or ""),
            "gold_answer": waggle_runner._gold_answer(case),
            "gold_support_ids": gold_ids,
            "embedding_model": embedding_model_name,
            "retrieval_limit": retrieval_limit,
            "ingestion": {
                "node_count": len(all_nodes),
                "node_type_counts": dict(Counter(node["node_type"] for node in all_nodes)),
                "gold_session_nodes": gold_nodes,
                "preference_like_nodes": preference_like_nodes,
            },
            "flat_vector": _flat_payload(case, case_graph, retrieval_limit=retrieval_limit),
            "waggle_full": _waggle_payload(case, case_graph, retrieval_limit=retrieval_limit),
        }
    finally:
        case_graph["graph"].close()
        case_graph["tmpdir"].cleanup()
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{case_id}-trace.json"
    md_path = output_dir / f"{case_id}-trace.md"
    json_path.write_text(json.dumps(trace, indent=2), encoding="utf-8")
    _write_markdown(md_path, trace)
    return {"json_path": str(json_path), "markdown_path": str(md_path), **trace}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Trace one LongMemEval case through Waggle ingestion and retrieval.")
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("runs/longmemeval/traces"))
    parser.add_argument("--embedding-model", default="all-MiniLM-L6-v2")
    parser.add_argument("--retrieval-limit", type=int, default=5)
    args = parser.parse_args(argv)
    result = trace_case(
        dataset=args.dataset.resolve(),
        case_id=args.case_id,
        output_dir=args.output_dir.resolve(),
        embedding_model_name=args.embedding_model,
        retrieval_limit=args.retrieval_limit,
    )
    print(f"Wrote {result['json_path']}")
    print(f"Wrote {result['markdown_path']}")
    print(f"Node type counts: {result['ingestion']['node_type_counts']}")
    print(f"Gold-session nodes: {len(result['ingestion']['gold_session_nodes'])}")
    print(f"Preference-like nodes: {len(result['ingestion']['preference_like_nodes'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
