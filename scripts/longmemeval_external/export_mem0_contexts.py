from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
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
    os.environ.setdefault("MEM0_TELEMETRY", "false")

    with args.output_path.open("w", encoding="utf-8") as handle:
        for index, case in enumerate(cases):
            started = time.perf_counter()
            row = export_case(
                case,
                case_index=index,
                system=args.system,
                top_k=args.top_k,
                infer=args.infer,
                max_turn_chars=args.max_turn_chars,
                per_item_budget=args.per_item_budget,
                embedder_provider=args.embedder_provider,
                embedder_model=args.embedder_model,
                embedding_dims=args.embedding_dims,
            )
            row["metadata"]["latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            print(f"exported {row['case_id']} {args.system} items={len(row['context_items'])}", file=sys.stderr, flush=True)
    return 0


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export Mem0 OSS LongMemEval contexts as JSONL.")
    parser.add_argument("--dataset-path", type=Path, required=True)
    parser.add_argument("--output-path", type=Path, required=True)
    parser.add_argument("--system", default="mem0_oss_raw")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--infer", action="store_true", help="Use Mem0 LLM extraction. Requires an LLM key and is costly.")
    parser.add_argument("--max-turn-chars", type=int, default=1800)
    parser.add_argument("--per-item-budget", type=int, default=700)
    parser.add_argument("--embedder-provider", default="huggingface")
    parser.add_argument("--embedder-model", default="all-MiniLM-L6-v2")
    parser.add_argument("--embedding-dims", type=int, default=384)
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
    infer: bool,
    max_turn_chars: int,
    per_item_budget: int,
    embedder_provider: str = "huggingface",
    embedder_model: str = "all-MiniLM-L6-v2",
    embedding_dims: int = 384,
) -> dict[str, Any]:
    os.environ.setdefault("MEM0_TELEMETRY", "false")
    from mem0 import Memory

    case_id = case_id_for(case, case_index)
    temp_dir = Path(tempfile.mkdtemp(prefix=f"mem0-{case_id}-"))
    try:
        memory = Memory.from_config(
            mem0_config(
                case_id,
                temp_dir,
                infer=infer,
                embedder_provider=embedder_provider,
                embedder_model=embedder_model,
                embedding_dims=embedding_dims,
            )
        )
        added = ingest_case(memory, case, case_id=case_id, infer=infer, max_turn_chars=max_turn_chars)
        results = memory.search(legacy._question(case), filters={"user_id": case_id}, top_k=max(1, top_k))
        hits = normalize_results(results)
        items: list[dict[str, Any]] = []
        for rank, hit in enumerate(hits, start=1):
            text = format_context_item(hit)
            if per_item_budget > 0:
                text = trim_text_to_budget(text, per_item_budget)
            metadata = dict(hit.get("metadata") or {})
            items.append(
                {
                    "item_id": str(hit.get("id") or f"mem0_hit_{rank}"),
                    "item_type": "mem0_memory",
                    "text": text,
                    "inclusion_reason": "mem0_search_hit",
                    "rank": rank,
                    "token_count": token_estimate(text),
                    "metadata": {
                        **metadata,
                        "score": hit.get("score"),
                        "role": hit.get("role") or metadata.get("role"),
                    },
                }
            )
        return {
            "case_id": case_id,
            "system": system,
            "context_items": items,
            "retrieved_transcript_ids": [str((item["metadata"] or {}).get("session_id") or item["item_id"]) for item in items],
            "source_evidence_ids": [str((item["metadata"] or {}).get("session_id") or item["item_id"]) for item in items],
            "retrieval_mode": f"{system}:mem0_oss_{'infer' if infer else 'raw'}_{embedder_provider}_{embedder_model}_qdrant",
            "adapter_notes": [
                "Uses Mem0 OSS Memory.add and Memory.search.",
                f"Embeddings/vector store are {embedder_provider} {embedder_model} plus local Qdrant.",
                "Default mode uses infer=False to avoid LLM extraction cost; pass --infer for Mem0's LLM extraction path.",
            ],
            "metadata": {
                "top_k": top_k,
                "infer": infer,
                "max_turn_chars": max_turn_chars,
                "per_item_budget": per_item_budget,
                "embedder_provider": embedder_provider,
                "embedder_model": embedder_model,
                "embedding_dims": embedding_dims,
                "added_items": added,
            },
        }
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def mem0_config(
    case_id: str,
    temp_dir: Path,
    *,
    infer: bool,
    embedder_provider: str = "huggingface",
    embedder_model: str = "all-MiniLM-L6-v2",
    embedding_dims: int = 384,
) -> dict[str, Any]:
    llm_provider = "groq" if infer else "ollama"
    llm_config: dict[str, Any]
    if infer:
        llm_config = {"model": "llama-3.3-70b-versatile", "temperature": 0.0, "max_tokens": 700}
    else:
        llm_config = {"model": "llama3.1:8b", "ollama_base_url": "http://localhost:11434"}
    return {
        "version": "v1.1",
        "history_db_path": str(temp_dir / "history.db"),
        "llm": {"provider": llm_provider, "config": llm_config},
        "embedder": mem0_embedder_config(embedder_provider, embedder_model, embedding_dims),
        "vector_store": {
            "provider": "qdrant",
            "config": {
                "collection_name": f"mem0_{safe_collection_name(case_id)}",
                "embedding_model_dims": embedding_dims,
                "path": str(temp_dir / "qdrant"),
                "on_disk": False,
            },
        },
    }


def mem0_embedder_config(provider: str, model: str, embedding_dims: int) -> dict[str, Any]:
    if provider == "ollama":
        return {
            "provider": "ollama",
            "config": {
                "model": model,
                "embedding_dims": embedding_dims,
                "ollama_base_url": "http://localhost:11434",
            },
        }
    return {
        "provider": provider,
        "config": {
            "model": model,
            "embedding_dims": embedding_dims,
        },
    }


def ingest_case(memory: Any, case: dict[str, Any], *, case_id: str, infer: bool, max_turn_chars: int) -> int:
    added = 0
    session_ids = [str(item) for item in case.get("haystack_session_ids") or []]
    dates = [str(item) for item in case.get("haystack_dates") or []]
    for session_index, session in enumerate(case.get("haystack_sessions") or []):
        if not isinstance(session, list):
            continue
        session_id = session_ids[session_index] if session_index < len(session_ids) else f"session_{session_index}"
        date = dates[session_index] if session_index < len(dates) else ""
        for turn_index, turn in enumerate(session):
            role = legacy._message_role(turn) or "user"
            text = legacy._message_text(turn)
            if not text:
                continue
            if max_turn_chars > 0:
                text = text[:max_turn_chars]
            message_role = role if role in {"user", "assistant", "system"} else "user"
            memory.add(
                [{"role": message_role, "content": text}],
                user_id=case_id,
                metadata={"session_id": session_id, "turn_index": turn_index, "date": date, "role": role},
                infer=infer,
            )
            added += 1
    return added


def normalize_results(results: Any) -> list[dict[str, Any]]:
    if isinstance(results, dict) and isinstance(results.get("results"), list):
        return [item for item in results["results"] if isinstance(item, dict)]
    if isinstance(results, list):
        return [item for item in results if isinstance(item, dict)]
    return []


def format_context_item(hit: dict[str, Any]) -> str:
    metadata = hit.get("metadata") or {}
    session_id = metadata.get("session_id") or "unknown"
    date = metadata.get("date") or ""
    role = hit.get("role") or metadata.get("role") or "memory"
    score = hit.get("score")
    header = f"Mem0 memory hit [{hit.get('id', 'unknown')}] session={session_id}"
    if date:
        header += f" documentDate={date}"
    if score is not None:
        header += f" score={score}"
    return f"{header}\n{role}: {hit.get('memory') or ''}".strip()


def safe_collection_name(case_id: str) -> str:
    return "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in case_id)[:40]


if __name__ == "__main__":
    raise SystemExit(main())
