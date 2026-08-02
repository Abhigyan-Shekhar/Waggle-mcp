from __future__ import annotations

import argparse
import ast
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from waggle.graph import MemoryGraph
from waggle.models import NodeType


@dataclass
class OolongExample:
    example_id: str
    context_window_id: str
    context_text: str
    question: str
    answer: str
    raw_answer: Any


def _row_value(row: dict[str, Any], *keys: str, default: str = "") -> Any:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return default


def load_oolong_examples(path: str | Path, dataset_kind: str = "") -> list[OolongExample]:
    rows = []
    with Path(path).open(encoding="utf-8") as handle:
        first = handle.read(1)
        handle.seek(0)
        if first == "[":
            rows = json.load(handle)
        else:
            rows = [json.loads(line) for line in handle if line.strip()]
    examples: list[OolongExample] = []
    for index, row in enumerate(rows):
        context = str(_row_value(row, "context_window_text", "context_text", "context", default=""))
        answer = _row_value(row, "answer", "target", "gold", default="")
        examples.append(
            OolongExample(
                example_id=str(_row_value(row, "example_id", "id", default=f"example-{index}")),
                context_window_id=str(_row_value(row, "context_window_id", "window_id", default=f"window-{index}")),
                context_text=context,
                question=str(_row_value(row, "question", "query", default="")),
                answer=str(answer),
                raw_answer=answer,
            )
        )
    return examples


def _index_context_window(
    graph: MemoryGraph,
    example: OolongExample,
    *,
    project: str = "oolong",
    chunk_lines: int = 12,
    overlap_lines: int = 3,
) -> None:
    lines = [line for line in example.context_text.splitlines() if line.strip()]
    if not lines:
        lines = [example.context_text]
    step = max(1, chunk_lines - overlap_lines)
    for start in range(0, len(lines), step):
        chunk = "\n".join(lines[start : start + chunk_lines]).strip()
        if not chunk:
            continue
        graph.add_node(
            label=f"{example.example_id} chunk {start // step + 1}",
            content=chunk,
            node_type=NodeType.NOTE,
            project=project,
            session_id=example.context_window_id,
            metadata={"example_id": example.example_id, "context_window_id": example.context_window_id},
        )


def _parse_answer(raw: str | list[str]) -> list[str]:
    if isinstance(raw, list):
        return sorted(str(item).strip() for item in raw if str(item).strip())
    text = str(raw).strip()
    try:
        parsed = ast.literal_eval(text)
        if isinstance(parsed, list):
            return sorted(str(item).strip() for item in parsed if str(item).strip())
    except Exception:
        pass
    return sorted(part.strip() for part in text.split("|") if part.strip())


def answers_match(predicted: str | list[str], gold: str | list[str]) -> bool:
    return _parse_answer(predicted) == _parse_answer(gold)


def main() -> int:
    parser = argparse.ArgumentParser(description="Load and validate an OOLONG-style dataset.")
    parser.add_argument("dataset")
    parser.add_argument("--eval-mode", default="retrieval_only")
    args = parser.parse_args()
    examples = load_oolong_examples(args.dataset)
    print(f"loaded {len(examples)} OOLONG examples for eval_mode={args.eval_mode}")
    return 0
