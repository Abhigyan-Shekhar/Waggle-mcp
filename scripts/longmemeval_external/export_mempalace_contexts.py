from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from scripts import run_longmemeval_waggle_phase as legacy
from scripts.longmemeval_full.context_builder import token_estimate, trim_text_to_budget
from scripts.longmemeval_full.ingestion import case_id_for


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cases = load_cases(args.dataset_path)
    if args.limit:
        cases = cases[: max(0, args.limit)]
    args.output_path.parent.mkdir(parents=True, exist_ok=True)

    with args.output_path.open("w", encoding="utf-8") as handle:
        for index, case in enumerate(cases):
            started = time.perf_counter()
            row = export_case(
                case,
                case_index=index,
                system=args.system,
                top_k=args.top_k,
                granularity=args.granularity,
                roles=args.roles,
                per_item_budget=args.per_item_budget,
            )
            row["metadata"]["latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            print(f"exported {row['case_id']} {args.system} items={len(row['context_items'])}", file=sys.stderr, flush=True)
    return 0


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export MemPalace-style verbatim LongMemEval contexts as JSONL.")
    parser.add_argument("--dataset-path", type=Path, required=True)
    parser.add_argument("--output-path", type=Path, required=True)
    parser.add_argument("--system", default="mempalace")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--granularity", choices=["session", "turn"], default="session")
    parser.add_argument("--roles", choices=["all", "user"], default="all")
    parser.add_argument("--per-item-budget", type=int, default=900)
    return parser.parse_args(argv)


def load_cases(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and isinstance(payload.get("cases"), list):
        payload = payload["cases"]
    if not isinstance(payload, list):
        raise ValueError("dataset path must contain a JSON list or {'cases': [...]}")
    return payload


def export_case(
    case: dict[str, Any],
    *,
    case_index: int,
    system: str,
    top_k: int,
    granularity: str,
    roles: str,
    per_item_budget: int,
) -> dict[str, Any]:
    import chromadb

    corpus = build_corpus(case, granularity=granularity, roles=roles)
    case_id = case_id_for(case, case_index)
    if not corpus:
        return {
            "case_id": case_id,
            "system": system,
            "context": "",
            "context_items": [],
            "retrieval_mode": f"{system}:verbatim_chroma_{granularity}_{roles}",
            "adapter_notes": ["No corpus documents were produced for this case."],
            "metadata": {"top_k": top_k, "granularity": granularity, "roles": roles},
        }

    client = chromadb.EphemeralClient()
    collection_name = f"mempalace_{re.sub(r'[^a-zA-Z0-9_]', '_', case_id)[:40]}"
    delete_collection_if_present(client, collection_name)
    collection = client.create_collection(collection_name)
    collection.add(
        documents=[item["document"] for item in corpus],
        ids=[f"doc_{index}" for index, _item in enumerate(corpus)],
        metadatas=[
            {
                "corpus_id": item["corpus_id"],
                "session_id": item["session_id"],
                "date": item["date"],
                "role": item["role"],
                "turn_index": item["turn_index"],
            }
            for item in corpus
        ],
    )
    result = collection.query(
        query_texts=[legacy._question(case)],
        n_results=min(max(1, top_k), len(corpus)),
        include=["distances", "metadatas", "documents"],
    )
    ids = list(result.get("ids", [[]])[0] or [])
    distances = list(result.get("distances", [[]])[0] or [])
    doc_lookup = {f"doc_{index}": item for index, item in enumerate(corpus)}
    items: list[dict[str, Any]] = []
    for rank, doc_id in enumerate(ids, start=1):
        item = doc_lookup.get(doc_id)
        if item is None:
            continue
        distance = float(distances[rank - 1]) if rank - 1 < len(distances) else 0.0
        text = format_context_item(item)
        if per_item_budget > 0:
            text = trim_text_to_budget(text, per_item_budget)
        items.append(
            {
                "item_id": item["corpus_id"],
                "item_type": f"mempalace_{granularity}",
                "text": text,
                "inclusion_reason": "mempalace_verbatim_chroma_hit",
                "rank": rank,
                "token_count": token_estimate(text),
                "metadata": {
                    "session_id": item["session_id"],
                    "date": item["date"],
                    "role": item["role"],
                    "turn_index": item["turn_index"],
                    "distance": distance,
                    "similarity_proxy": 1.0 - distance,
                },
            }
        )
    return {
        "case_id": case_id,
        "system": system,
        "context_items": items,
        "retrieved_transcript_ids": [item["item_id"] for item in items],
        "source_evidence_ids": [item["metadata"]["session_id"] for item in items],
        "retrieval_mode": f"{system}:verbatim_chroma_{granularity}_{roles}",
        "adapter_notes": [
            "Local MemPalace-style baseline using ChromaDB default embeddings and verbatim storage.",
            "All roles are preserved for end-to-end QA unless --roles=user is selected.",
            "This exports contexts into Waggle's controlled reader/judge harness; it is not MemPalace's retrieval-only benchmark score.",
        ],
        "metadata": {
            "top_k": top_k,
            "granularity": granularity,
            "roles": roles,
            "per_item_budget": per_item_budget,
            "corpus_count": len(corpus),
        },
    }


def delete_collection_if_present(client: Any, collection_name: str) -> None:
    try:
        client.delete_collection(collection_name)
    except Exception as exc:
        message = str(exc).lower()
        if "does not exist" in message or "not found" in message:
            return
        raise


def build_corpus(case: dict[str, Any], *, granularity: str, roles: str) -> list[dict[str, Any]]:
    sessions = list(case.get("haystack_sessions") or [])
    session_ids = [str(item) for item in case.get("haystack_session_ids") or []]
    dates = [str(item) for item in case.get("haystack_dates") or []]
    corpus: list[dict[str, Any]] = []
    for session_index, session in enumerate(sessions):
        if not isinstance(session, list):
            continue
        session_id = session_ids[session_index] if session_index < len(session_ids) else f"session_{session_index}"
        date = dates[session_index] if session_index < len(dates) else ""
        if granularity == "session":
            lines = [f"Session [{session_id}]"]
            if date:
                lines.append(f"documentDate: {date}")
            for turn_index, turn in enumerate(session):
                role = legacy._message_role(turn) or "unknown"
                if roles == "user" and role != "user":
                    continue
                text = legacy._message_text(turn)
                if text:
                    lines.append(f"{role}: {text}")
            if len(lines) > (2 if date else 1):
                corpus.append(
                    {
                        "corpus_id": session_id,
                        "session_id": session_id,
                        "date": date,
                        "role": "session",
                        "turn_index": -1,
                        "document": "\n".join(lines),
                    }
                )
            continue
        for turn_index, turn in enumerate(session):
            role = legacy._message_role(turn) or "unknown"
            if roles == "user" and role != "user":
                continue
            text = legacy._message_text(turn)
            if not text:
                continue
            corpus.append(
                {
                    "corpus_id": f"{session_id}_turn_{turn_index}",
                    "session_id": session_id,
                    "date": date,
                    "role": role,
                    "turn_index": turn_index,
                    "document": f"Session [{session_id}]\ndocumentDate: {date}\n{role}: {text}".strip(),
                }
            )
    return corpus


def format_context_item(item: dict[str, Any]) -> str:
    return f"MemPalace verbatim hit [{item['corpus_id']}]:\n{item['document']}"


if __name__ == "__main__":
    raise SystemExit(main())
