"""
Clean OOLONG-synth dataset builder.
Fixes:
  1. 1 question per unique context_window_id (no context reuse)
  2. Diverse task groups (user + counting, balanced)
  3. Strips preamble instruction from context so LLM must classify text itself
  4. Saves to oolong_real_clean_30.jsonl
"""
import json
from collections import defaultdict
from pathlib import Path

from datasets import load_dataset

ROOT = Path(__file__).resolve().parents[2]
OUT_PATH = ROOT / "benchmarks/data/oolong_real_clean_30.jsonl"
TARGET_PER_GROUP = 15   # 15 user + 15 counting = 30 total
MAX_CTX_WORDS   = 2000  # keep context manageable

def strip_preamble(text: str) -> str:
    """
    Remove the task instruction preamble from context_window_text.
    The preamble ends at the blank line before the first 'Date:' line.
    What remains is the raw data lines only — the LLM must infer labels.
    """
    lines = text.split("\n")
    data_start = None
    for i, line in enumerate(lines):
        if line.strip().startswith("Date:"):
            data_start = i
            break
    if data_start is not None:
        return "\n".join(lines[data_start:]).strip()
    return text.strip()

def main():
    print("Streaming oolong-synth validation split...")
    ds = load_dataset("oolongbench/oolong-synth", split="validation", streaming=True)

    # Collect: one example per unique context_window_id per task_group
    seen_cw: dict[str, set] = defaultdict(set)  # task_group -> set of cw_ids
    collected: dict[str, list] = defaultdict(list)

    for s in ds:
        tg = s.get("task_group", "")
        if tg not in ("user", "counting"):
            continue

        cw_id = str(s["context_window_id"])
        if cw_id in seen_cw[tg]:
            continue  # already have a question for this window in this group

        raw_ctx = s["context_window_text"]
        clean_ctx = strip_preamble(raw_ctx)
        ctx_words = len(clean_ctx.split())

        if ctx_words > MAX_CTX_WORDS:
            continue  # skip huge contexts for now

        seen_cw[tg].add(cw_id)
        collected[tg].append({
            "context_window_id": f"cw-clean-{cw_id}-{tg}",
            "raw_context_window_id": cw_id,
            "context_window_text": clean_ctx,
            "question": s["question"],
            "answer": s["answer"],
            "answer_type": str(s["answer_type"]),
            "task_group": tg,
            "task": str(s["task"]),
            "ctx_words": ctx_words,
        })

        done = len(collected["user"]) + len(collected["counting"])
        if done % 5 == 0:
            print(f"  Collected: user={len(collected['user'])} counting={len(collected['counting'])}")

        if (len(collected["user"]) >= TARGET_PER_GROUP and
                len(collected["counting"]) >= TARGET_PER_GROUP):
            break

    # Balance and assign IDs
    user_rows    = collected["user"][:TARGET_PER_GROUP]
    counting_rows = collected["counting"][:TARGET_PER_GROUP]
    all_rows = user_rows + counting_rows

    total_unique_cw = len(set(r["raw_context_window_id"] for r in all_rows))

    out = []
    for i, row in enumerate(all_rows):
        row["example_id"] = f"clean-{i}"
        out.append(row)

    with OUT_PATH.open("w") as f:
        for row in out:
            f.write(json.dumps(row) + "\n")

    print(f"\n✅ Saved {len(out)} examples → {OUT_PATH}")
    print(f"   Unique context windows: {total_unique_cw}")
    print(f"   task_group dist: user={len(user_rows)}, counting={len(counting_rows)}")
    print(f"   Avg ctx words: {sum(r['ctx_words'] for r in out)//len(out)}")

    # Show sample stripped context
    sample = out[0]
    print("\n--- Sample stripped context (first 300 chars) ---")
    print(sample["context_window_text"][:300])
    print("\n--- Sample question ---")
    print(sample["question"])
    print("\n--- Sample gold answer ---")
    print(sample["answer"])

if __name__ == "__main__":
    main()
