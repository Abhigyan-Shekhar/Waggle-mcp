#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import plan_longmemeval_run
import run_longmemeval_waggle_phase as waggle_runner


PERSONALIZATION_QUERY_SUFFIX = (
    " user's personal context stated preference prior detail owned item current setup plan constraint"
)
TYPE_PRIOR = {
    "preference": 0.08,
    "question": 0.05,
    "decision": 0.03,
    "note": 0.03,
}
FALLBACK_MIN_TOP_SCORE = 0.34
FALLBACK_MIN_MARGIN = 0.025


def _tokens(text: str) -> set[str]:
    return {token for token in waggle_runner._content_tokens(text) if len(token) > 2}


def _case_by_id(dataset: Path) -> dict[str, dict[str, Any]]:
    cases = plan_longmemeval_run._load_cases(dataset)
    return {plan_longmemeval_run._case_id(case, index): case for index, case in enumerate(cases, start=1)}


def _has_answer_texts(case: dict[str, Any]) -> list[str]:
    gold_sessions = set(waggle_runner._gold_support_ids(case))
    output: list[str] = []
    for session_id, messages in zip(case.get("haystack_session_ids") or [], case.get("haystack_sessions") or []):
        if session_id not in gold_sessions:
            continue
        for message in messages:
            if isinstance(message, dict) and message.get("has_answer"):
                text = waggle_runner._message_text(message)
                if text:
                    output.append(text)
    return output


def _looks_personalization_query(question: str) -> bool:
    normalized = question.lower().replace("’", "'").replace("‘", "'")
    lowered = f" {normalized} "
    advice_markers = (
        " any tips",
        " advice",
        " recommend",
        " recommendation",
        " suggestions",
        " what should i",
        " how should i",
        " how can i",
        " do you think",
    )
    personal_markers = (" i ", " my ", " me ", " i'm ", " i've ", " this weekend", " lately")
    return any(marker in lowered for marker in advice_markers) and any(marker in lowered for marker in personal_markers)


def _load_nodes(case_graph: dict[str, Any]) -> list[dict[str, Any]]:
    graph = case_graph["graph"]
    with sqlite3.connect(str(graph.db_path)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT id, agent_id, project, session_id, context_window_id, label, content, node_type, tags,
                   source_prompt, metadata, evidence_records, valid_from, valid_to, created_at,
                   updated_at, access_count, embedding, tenant_id
            FROM nodes
            WHERE tenant_id = ? AND project = ? AND embedding IS NOT NULL
            """,
            (graph.tenant_id, case_graph["project"]),
        ).fetchall()
    nodes: list[dict[str, Any]] = []
    for row in rows:
        node = graph._row_to_node(row)
        embedding = graph._decode_embedding(row["embedding"])
        if embedding is None:
            continue
        nodes.append({"node": node, "embedding": embedding})
    return nodes


def _lexical_score(query: str, node: Any) -> float:
    query_tokens = _tokens(query)
    node_tokens = _tokens(f"{getattr(node, 'label', '')} {getattr(node, 'content', '')}")
    if not query_tokens or not node_tokens:
        return 0.0
    overlap = len(query_tokens & node_tokens)
    return overlap / max(1, min(len(query_tokens), len(node_tokens)))


def _gold_like_node_ids(case: dict[str, Any], nodes: list[dict[str, Any]]) -> set[str]:
    gold_sessions = set(waggle_runner._gold_support_ids(case))
    answer_tokens = set()
    for text in _has_answer_texts(case):
        answer_tokens |= _tokens(text)
    if not answer_tokens:
        return set()
    output: set[str] = set()
    for item in nodes:
        node = item["node"]
        if getattr(node, "session_id", "") not in gold_sessions:
            continue
        node_tokens = _tokens(f"{getattr(node, 'label', '')} {getattr(node, 'content', '')}")
        overlap = len(answer_tokens & node_tokens)
        node_type = waggle_runner._node_type_value(node)
        if overlap >= 4 or (overlap >= 3 and node_type in {"preference", "question", "decision", "note"}):
            output.add(node.id)
    return output


def _rank_nodes(
    *,
    graph: Any,
    nodes: list[dict[str, Any]],
    query: str,
    use_type_prior: bool,
    top_k: int,
) -> list[dict[str, Any]]:
    query_embedding = graph.embedding_model.embed(query)
    ranked: list[dict[str, Any]] = []
    for item in nodes:
        node = item["node"]
        node_type = waggle_runner._node_type_value(node)
        similarity = max(graph.embedding_model.cosine_similarity(query_embedding, item["embedding"]), 0.0)
        lexical = _lexical_score(query, node)
        prior = TYPE_PRIOR.get(node_type, 0.0) if use_type_prior else 0.0
        score = (0.7 * similarity) + (0.3 * lexical) + prior
        ranked.append(
            {
                "id": node.id,
                "session_id": getattr(node, "session_id", ""),
                "node_type": node_type,
                "label": getattr(node, "label", ""),
                "content": getattr(node, "content", ""),
                "score": score,
                "similarity": similarity,
                "lexical": lexical,
                "type_prior": prior,
            }
        )
    ranked.sort(key=lambda row: (-row["score"], row["node_type"], row["label"]))
    return ranked[:top_k]


def _low_confidence_ranking(rows: list[dict[str, Any]]) -> bool:
    if not rows:
        return True
    top_score = float(rows[0]["score"])
    next_score = float(rows[1]["score"]) if len(rows) > 1 else 0.0
    return top_score < FALLBACK_MIN_TOP_SCORE or (top_score - next_score) < FALLBACK_MIN_MARGIN


def diagnose_case(
    case_id: str,
    case: dict[str, Any],
    *,
    embedding_model: Any,
    top_k: int,
) -> dict[str, Any]:
    case_graph = waggle_runner._build_case_graph(case, embedding_model=embedding_model, agent_id="ranking-diagnostic")
    graph = case_graph["graph"]
    nodes = _load_nodes(case_graph)
    question = waggle_runner._question(case)
    oracle_personal = waggle_runner._task(case) == "single-session-preference"
    classifier_personal = _looks_personalization_query(question)
    gold_ids = _gold_like_node_ids(case, nodes)

    baseline_graph = graph.query(
        query=question,
        project=case_graph["project"],
        agent_id=case_graph["agent_id"],
        max_nodes=top_k,
        retrieval_mode="graph",
    ).nodes

    def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
        top_ids = [row["id"] for row in rows]
        return {
            "gold_like_hit_at_k": bool(gold_ids & set(top_ids)),
            "gold_session_hit_at_k": bool(set(waggle_runner._gold_support_ids(case)) & {row["session_id"] for row in rows}),
            "top": [
                {
                    "id": row["id"],
                    "session_id": row["session_id"],
                    "node_type": row["node_type"],
                    "score": round(float(row["score"]), 6),
                    "similarity": round(float(row["similarity"]), 6),
                    "lexical": round(float(row["lexical"]), 6),
                    "type_prior": round(float(row["type_prior"]), 6),
                    "label": " ".join(str(row["label"]).split())[:160],
                    "content": " ".join(str(row["content"]).split())[:260],
                }
                for row in rows
            ],
        }

    def baseline_graph_rows() -> list[dict[str, Any]]:
        output = []
        for node in baseline_graph:
            output.append(
                {
                    "id": node.id,
                    "session_id": getattr(node, "session_id", ""),
                    "node_type": waggle_runner._node_type_value(node),
                    "score": 0.0,
                    "similarity": 0.0,
                    "lexical": 0.0,
                    "type_prior": 0.0,
                    "label": getattr(node, "label", ""),
                    "content": getattr(node, "content", ""),
                }
            )
        return output

    raw_rows = _rank_nodes(graph=graph, nodes=nodes, query=question, use_type_prior=False, top_k=top_k)
    strategies: dict[str, dict[str, Any]] = {
        "graph_baseline": summarize(baseline_graph_rows()),
        "raw_similarity": summarize(raw_rows),
    }
    oracle_query = question + (PERSONALIZATION_QUERY_SUFFIX if oracle_personal else "")
    classifier_query = question + (PERSONALIZATION_QUERY_SUFFIX if classifier_personal else "")
    strategies["oracle_type_prior"] = summarize(
        _rank_nodes(graph=graph, nodes=nodes, query=question, use_type_prior=oracle_personal, top_k=top_k)
    )
    strategies["oracle_reformulated"] = summarize(
        _rank_nodes(graph=graph, nodes=nodes, query=oracle_query, use_type_prior=False, top_k=top_k)
    )
    strategies["oracle_reformulated_type_prior"] = summarize(
        _rank_nodes(graph=graph, nodes=nodes, query=oracle_query, use_type_prior=oracle_personal, top_k=top_k)
    )
    classifier_rows = _rank_nodes(
        graph=graph, nodes=nodes, query=classifier_query, use_type_prior=classifier_personal, top_k=top_k
    )
    strategies["classifier_reformulated_type_prior"] = summarize(classifier_rows)
    fallback_used = classifier_personal and _low_confidence_ranking(raw_rows)
    fallback_summary = summarize(classifier_rows if fallback_used else raw_rows)
    fallback_summary["fallback_used"] = fallback_used
    fallback_summary["fallback_reason"] = (
        {
            "top_score": round(float(raw_rows[0]["score"]), 6) if raw_rows else 0.0,
            "margin": round(float(raw_rows[0]["score"] - raw_rows[1]["score"]), 6) if len(raw_rows) > 1 else None,
            "min_top_score": FALLBACK_MIN_TOP_SCORE,
            "min_margin": FALLBACK_MIN_MARGIN,
        }
        if fallback_used
        else None
    )
    strategies["classifier_fallback_reformulated_type_prior"] = fallback_summary

    gold_like_nodes = []
    for item in nodes:
        node = item["node"]
        if node.id in gold_ids:
            gold_like_nodes.append(
                {
                    "id": node.id,
                    "session_id": getattr(node, "session_id", ""),
                    "node_type": waggle_runner._node_type_value(node),
                    "label": " ".join(str(getattr(node, "label", "")).split())[:160],
                    "content": " ".join(str(getattr(node, "content", "")).split())[:260],
                }
            )

    return {
        "case_id": case_id,
        "category": waggle_runner._task(case),
        "question": question,
        "gold_support_ids": waggle_runner._gold_support_ids(case),
        "haystack_sessions": len(case_graph["all_session_ids"]),
        "node_count": len(nodes),
        "oracle_personalization": oracle_personal,
        "classifier_personalization": classifier_personal,
        "gold_like_node_count": len(gold_ids),
        "gold_like_node_ids": sorted(gold_ids),
        "gold_like_nodes": gold_like_nodes[:20],
        "strategies": strategies,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose LongMemEval SSP node-ranking levers without reader calls.")
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--case-id", action="append", dest="case_ids", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--embedding-model", default="all-MiniLM-L6-v2")
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()

    EmbeddingModel, _ = waggle_runner._load_waggle_classes()
    embedding_model = EmbeddingModel(args.embedding_model)
    cases = _case_by_id(args.dataset)
    results = []
    for case_id in args.case_ids:
        if case_id not in cases:
            raise ValueError(f"case_id {case_id!r} not found")
        results.append(diagnose_case(case_id, cases[case_id], embedding_model=embedding_model, top_k=args.top_k))

    payload = {
        "dataset": str(args.dataset),
        "embedding_model": args.embedding_model,
        "top_k": args.top_k,
        "strategies": [
            "graph_baseline",
            "raw_similarity",
            "oracle_type_prior",
            "oracle_reformulated",
            "oracle_reformulated_type_prior",
            "classifier_reformulated_type_prior",
            "classifier_fallback_reformulated_type_prior",
        ],
        "cases": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    for name in payload["strategies"]:
        hits = sum(1 for case in results if case["strategies"][name]["gold_like_hit_at_k"])
        session_hits = sum(1 for case in results if case["strategies"][name]["gold_session_hit_at_k"])
        print(f"{name}: gold_like_hit@{args.top_k}={hits}/{len(results)} gold_session_hit@{args.top_k}={session_hits}/{len(results)}")
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
