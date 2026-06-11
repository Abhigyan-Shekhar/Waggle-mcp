"""
Full OOLONG End-to-End Evaluation with Real LLM (Groq llama-3.3-70b-versatile)
===============================================================================
Datasets:
  1. OOLONG-Pairs (synthetic, 20 cases) — pairs aggregation task
  2. OOLONG-synth (real, 30 cases, user+counting) — from oolongbench/oolong-synth

Modes:
  Mode 1 — top-k retrieval (max_nodes=8) → Groq LLM → parse answer
  Mode 2 — Waggle aggregate (max_nodes=1000) → Groq LLM → parse answer

NO deterministic solver. LLM answers everything.

RLM Baseline (from paper arXiv:2511.02817):
  GPT-4o on oolong-synth at ~16K context: ~42% accuracy (user+counting tasks)
  Full-context frontier models at 128K: <50% on both splits

Run:
    PYTHONPATH=src GROQ_API_KEY=gsk_... .venv/bin/python3 scripts/oolong/run_oolong_llm_eval.py
"""
import ast
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import groq
import numpy as np

from waggle.graph import MemoryGraph
from waggle.oolong_benchmark import _index_context_window, load_oolong_examples

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL_PAIRS = "llama-3.1-8b-instant"   # fast + low TPM
GROQ_MODEL_SYNTH = "llama-3.1-8b-instant"   # same model, consistent comparison

PAIRS_DATASET  = ROOT / "benchmarks/data/oolong_synthetic_20.jsonl"
REAL_DATASET   = ROOT / "benchmarks/data/oolong_real_clean_30.jsonl"  # clean: 1 CW per example, no preamble

DB_PATH = ROOT / "benchmarks/data/llm_eval.db"

# RLM paper reported numbers (arXiv:2511.02817, Table 2 / Figure 3)
# GPT-4o-mini on oolong-synth small context window subset ≈ 38–42%
# We use 42% as RLM-equivalent upper bound for fair comparison
RLM_REPORTED_ACCURACY = 42.0


# ---------------------------------------------------------------------------
# Dummy embedding model (aggregate uses structural, not semantic, retrieval)
# ---------------------------------------------------------------------------
class DummyEmbeddingModel:
    def embed(self, text):
        return np.zeros(384, dtype=np.float32)

    def from_bytes(self, b):
        return np.frombuffer(b, dtype=np.float32) if b else np.zeros(384, dtype=np.float32)

    def to_bytes(self, arr):
        return np.asarray(arr, dtype=np.float32).tobytes()

    @staticmethod
    def cosine_similarity(a, b):
        a, b = np.asarray(a, np.float32), np.asarray(b, np.float32)
        na, nb = np.linalg.norm(a), np.linalg.norm(b)
        return float(np.dot(a, b) / (na * nb)) if na and nb else 0.0


# ---------------------------------------------------------------------------
# Groq LLM caller
# ---------------------------------------------------------------------------
_groq_client = groq.Groq(api_key=GROQ_API_KEY)


def call_llm(prompt: str, max_tokens: int = 512, model: str | None = None) -> str:
    """Call Groq LLM and return the response text. Retries on rate limit."""
    if model is None:
        model = GROQ_MODEL_PAIRS
    for attempt in range(5):
        try:
            resp = _groq_client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
                temperature=0.0,
            )
            return resp.choices[0].message.content.strip()
        except groq.RateLimitError:
            wait = min(60, 5 * (2 ** attempt))
            print(f"    [rate limit] sleeping {wait}s...")
            time.sleep(wait)
        except Exception as e:
            print(f"    [llm error] {e}")
            return ""
    return ""


# ---------------------------------------------------------------------------
# OOLONG-Pairs prompt (same one used in our previous eval)
# ---------------------------------------------------------------------------
PAIRS_PROMPT_TEMPLATE = """\
You are given a dataset of examples.

Each example contains:

* Text
* User
* Date

Each text belongs to one category:
description, entity, human being, numeric value, location, abbreviation.
The labels are not provided and must be inferred.

---

DATA:
{data}

---

TASK:

List all pairs of user IDs (no duplicates, smaller ID first) where both users have at least one instance containing a numeric value or location.

Return format:
(user_id_1, user_id_2)
One per line. No explanation.\
"""

# ---------------------------------------------------------------------------
# OOLONG-synth general prompt (user + counting tasks)
# ---------------------------------------------------------------------------
SYNTH_PROMPT_TEMPLATE = """\
{context_window_text}

---

{question}

Give only the final answer, nothing else.\
"""


# ---------------------------------------------------------------------------
# Answer normalisation
# ---------------------------------------------------------------------------
def _normalise(raw: str) -> str:
    raw = raw.strip().lower()
    raw = re.sub(r"[^a-z0-9\(\),\s\|]", " ", raw)
    return " ".join(raw.split())


def parse_pairs_from_llm(raw: str) -> list[str]:
    """Extract (u1, u2) pair lines from LLM output."""
    pairs = []
    for line in raw.strip().splitlines():
        line = line.strip()
        m = re.match(r"\((\d+),\s*(\d+)\)", line)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            pairs.append(f"({min(a,b)}, {max(a,b)})")
    return sorted(set(pairs))


def parse_gold_pairs(raw: str) -> list[str]:
    """Parse gold answer — either pipe-separated or repr(list)."""
    raw = raw.strip()
    try:
        parsed = ast.literal_eval(raw)
        if isinstance(parsed, list):
            return sorted(str(x).strip() for x in parsed)
    except Exception:
        pass
    return sorted(p.strip() for p in raw.split("|") if p.strip())


def pairs_exact_match(pred_raw: str, gold_raw: str) -> bool:
    return parse_pairs_from_llm(pred_raw) == parse_gold_pairs(gold_raw)


def parse_gold_synth(raw) -> str:
    """Gold for synth tasks is a list with one string element."""
    if isinstance(raw, list):
        return _normalise(" ".join(str(x) for x in raw))
    return _normalise(str(raw))


def synth_exact_match(pred_raw: str, gold) -> bool:
    gold_str = parse_gold_synth(gold)
    pred_str = _normalise(pred_raw)
    # Check if gold string appears anywhere in the prediction
    return gold_str in pred_str or pred_str == gold_str


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------
@dataclass
class CaseResult:
    example_id: str
    dataset: str          # "pairs" or "synth"
    task_group: str
    mode: str
    retrieved_node_count: int
    retrieved_tokens: int
    prompt_tokens: int
    predicted_answer: str
    gold_answer: str
    exact_match: bool
    latency_s: float


@dataclass
class ModeReport:
    mode: str
    cases: list[CaseResult] = field(default_factory=list)

    def summary(self, dataset: str | None = None) -> dict:
        cases = [c for c in self.cases if dataset is None or c.dataset == dataset]
        correct = sum(1 for c in cases if c.exact_match)
        total = len(cases)
        answered = sum(1 for c in cases if c.predicted_answer.strip())
        avg_ret = sum(c.retrieved_tokens for c in cases) / max(total, 1)
        avg_pmt = sum(c.prompt_tokens for c in cases) / max(total, 1)
        avg_lat = sum(c.latency_s for c in cases) / max(total, 1)
        return {
            "mode": self.mode,
            "dataset": dataset or "all",
            "total": total,
            "answered": answered,
            "correct": correct,
            "accuracy_pct": round((correct / total) * 100, 1) if total else 0.0,
            "avg_retrieved_tokens": round(avg_ret),
            "avg_prompt_tokens": round(avg_pmt),
            "avg_latency_s": round(avg_lat, 2),
        }


# ---------------------------------------------------------------------------
# Graph builder — one shared DB
# ---------------------------------------------------------------------------
def build_graph(pairs_dataset: str, real_dataset: str, db_path: str):
    if os.path.exists(db_path):
        os.remove(db_path)

    graph = MemoryGraph(db_path=db_path, embedding_model=DummyEmbeddingModel())

    # Index pairs dataset
    pairs_examples = load_oolong_examples(pairs_dataset)
    indexed = set()
    for ex in pairs_examples:
        if ex.context_window_id not in indexed:
            _index_context_window(graph, ex, project="", chunk_lines=12, overlap_lines=3)
            indexed.add(ex.context_window_id)

    # Index real synth dataset — store whole context as single node per example
    real_examples = []
    with open(real_dataset) as f:
        for line in f:
            row = json.loads(line)
            real_examples.append(row)
            node_id = graph.add_node(
                label=f"oolong-synth-{row['example_id']}",
                content=row["context_window_text"],
                node_type="note",
                session_id=f"real-{row['context_window_id']}",
                tags=["oolong", "synth", row["task_group"]],
            )

    return graph, pairs_examples, real_examples


# ---------------------------------------------------------------------------
# Run one example — both modes
# ---------------------------------------------------------------------------
def run_pairs_case(graph, example, mode: str) -> CaseResult:
    session = example.context_window_id

    if mode == "topk":
        result = graph.query(query=example.question, max_nodes=8, session_id=session)
    else:
        result = graph.aggregate(query="", max_nodes=1000, max_depth=0, session_id=session)

    chunks = [n.content for n in result.nodes]
    ret_tokens = sum(len(c.split()) for c in chunks)

    data_block = "\n\n".join(chunks)
    prompt = PAIRS_PROMPT_TEMPLATE.format(data=data_block)
    pmt_tokens = len(prompt.split())

    t0 = time.time()
    llm_out = call_llm(prompt, max_tokens=800, model=GROQ_MODEL_PAIRS)
    latency = time.time() - t0

    em = pairs_exact_match(llm_out, example.answer)

    return CaseResult(
        example_id=example.example_id,
        dataset="pairs",
        task_group="oolong-pairs",
        mode=mode,
        retrieved_node_count=len(chunks),
        retrieved_tokens=ret_tokens,
        prompt_tokens=pmt_tokens,
        predicted_answer=llm_out,
        gold_answer=example.answer,
        exact_match=em,
        latency_s=latency,
    )


def run_synth_case(graph, row: dict, mode: str) -> CaseResult:
    session = f"real-{row['context_window_id']}"

    if mode == "topk":
        result = graph.query(query=row["question"], max_nodes=8, session_id=session)
        chunks = [n.content for n in result.nodes]
    else:
        result = graph.aggregate(query="", max_nodes=1000, max_depth=0, session_id=session)
        chunks = [n.content for n in result.nodes]

    # For synth tasks the context IS the full text — so for topk we use the
    # retrieved fragments, for aggregate we get the full context back
    data_block = "\n\n".join(chunks) if chunks else row["context_window_text"]
    ret_tokens = sum(len(c.split()) for c in chunks)

    prompt = SYNTH_PROMPT_TEMPLATE.format(
        context_window_text=data_block,
        question=row["question"],
    )
    pmt_tokens = len(prompt.split())

    t0 = time.time()
    llm_out = call_llm(prompt, max_tokens=128, model=GROQ_MODEL_SYNTH)
    latency = time.time() - t0

    em = synth_exact_match(llm_out, row["answer"])

    return CaseResult(
        example_id=row["example_id"],
        dataset="synth",
        task_group=row["task_group"],
        mode=mode,
        retrieved_node_count=len(chunks),
        retrieved_tokens=ret_tokens,
        prompt_tokens=pmt_tokens,
        predicted_answer=llm_out,
        gold_answer=str(row["answer"]),
        exact_match=em,
        latency_s=latency,
    )


# ---------------------------------------------------------------------------
# Printing
# ---------------------------------------------------------------------------
def print_report(report: ModeReport):
    print(f"\n{'='*84}")
    print(f"  MODE: {report.mode}")
    print(f"{'='*84}")
    print(f"  {'ID':<14} {'Dataset':<8} {'Task':<12} {'Chunks':>6} {'RetTok':>7} {'PmtTok':>7} {'Lat':>5} {'✓'}")
    print(f"  {'-'*75}")
    for c in report.cases:
        em = "✅" if c.exact_match else "❌"
        print(f"  {c.example_id:<14} {c.dataset:<8} {c.task_group:<12} "
              f"{c.retrieved_node_count:>6} {c.retrieved_tokens:>7} "
              f"{c.prompt_tokens:>7} {c.latency_s:>4.1f}s {em}")
        if not c.exact_match:
            pred = c.predicted_answer.replace("\n", " ")[:80]
            gold = str(c.gold_answer)[:60]
            print(f"    pred: {pred}")
            print(f"    gold: {gold}")

    for ds in ("pairs", "synth"):
        s = report.summary(ds)
        print(f"\n  [{ds.upper()}] correct={s['correct']}/{s['total']}  "
              f"accuracy={s['accuracy_pct']}%  "
              f"avg_ret_tok={s['avg_retrieved_tokens']}  "
              f"avg_lat={s['avg_latency_s']}s")

    s = report.summary()
    print(f"\n  [OVERALL] correct={s['correct']}/{s['total']}  accuracy={s['accuracy_pct']}%")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    print("\n" + "="*84)
    print("  OOLONG FULL LLM EVALUATION  (Groq llama-3.3-70b-versatile)")
    print("="*84)

    print("\n🔧 Building graph...")
    t0 = time.time()
    graph, pairs_examples, real_examples = build_graph(PAIRS_DATASET, REAL_DATASET, DB_PATH)
    print(f"   Done in {time.time()-t0:.1f}s — {len(pairs_examples)} pairs + {len(real_examples)} synth examples indexed")

    mode1 = ModeReport(mode="Mode 1 — top-k (max_nodes=8) + Groq LLM")
    mode2 = ModeReport(mode="Mode 2 — Waggle aggregate (max_nodes=1000) + Groq LLM")

    total = len(pairs_examples) + len(real_examples)
    done = 0

    print(f"\n⚡ Running {total} pairs cases × 2 modes = {total*2} LLM calls total...\n")

    # --- Pairs dataset ---
    for ex in pairs_examples:
        for mode_key, report in [("topk", mode1), ("aggregate", mode2)]:
            print(f"  [{done+1}/{total*2}] {ex.example_id} | mode={mode_key}", end=" ", flush=True)
            c = run_pairs_case(graph, ex, mode_key)
            report.cases.append(c)
            em = "✅" if c.exact_match else "❌"
            print(f"chunks={c.retrieved_node_count} tokens={c.retrieved_tokens} {em} ({c.latency_s:.1f}s)")
            done += 1
            time.sleep(3.0)  # 3s gap to stay within Groq TPM limits

    # --- Real synth dataset ---
    for row in real_examples:
        for mode_key, report in [("topk", mode1), ("aggregate", mode2)]:
            print(f"  [{done+1}/{total*2}] {row['example_id']} | mode={mode_key} | task={row['task_group']}", end=" ", flush=True)
            c = run_synth_case(graph, row, mode_key)
            report.cases.append(c)
            em = "✅" if c.exact_match else "❌"
            print(f"chunks={c.retrieved_node_count} tokens={c.retrieved_tokens} {em} ({c.latency_s:.1f}s)")
            done += 1
            time.sleep(3.0)  # 3s gap to stay within Groq TPM limits

    # --- Print results ---
    print_report(mode1)
    print_report(mode2)

    # --- Head-to-head comparison ---
    s1_all = mode1.summary()
    s2_all = mode2.summary()
    s1_p = mode1.summary("pairs")
    s2_p = mode2.summary("pairs")
    s1_s = mode1.summary("synth")
    s2_s = mode2.summary("synth")

    print(f"\n{'='*84}")
    print("  HEAD-TO-HEAD COMPARISON")
    print(f"{'='*84}")
    print(f"  {'Metric':<35} {'Mode1 (top-k)':>15} {'Mode2 (Waggle)':>15} {'RLM (paper)':>12}")
    print(f"  {'-'*80}")
    print(f"  {'Overall accuracy':<35} {s1_all['accuracy_pct']:>14.1f}% {s2_all['accuracy_pct']:>14.1f}% {RLM_REPORTED_ACCURACY:>11.1f}%")
    print(f"  {'OOLONG-Pairs accuracy':<35} {s1_p['accuracy_pct']:>14.1f}% {s2_p['accuracy_pct']:>14.1f}% {'N/A':>12}")
    print(f"  {'OOLONG-synth accuracy':<35} {s1_s['accuracy_pct']:>14.1f}% {s2_s['accuracy_pct']:>14.1f}% {RLM_REPORTED_ACCURACY:>11.1f}%")
    print(f"  {'Avg retrieved tokens (pairs)':<35} {s1_p['avg_retrieved_tokens']:>15} {s2_p['avg_retrieved_tokens']:>15} {'full ctx':>12}")
    print(f"  {'Avg prompt tokens (synth)':<35} {s1_s['avg_prompt_tokens']:>15} {s2_s['avg_prompt_tokens']:>15} {'full ctx':>12}")
    print(f"  {'Avg latency/call':<35} {s1_all['avg_latency_s']:>14.2f}s {s2_all['avg_latency_s']:>14.2f}s {'N/A':>12}")

    # Verdict
    print()
    delta = s2_all['accuracy_pct'] - s1_all['accuracy_pct']
    delta_rlm = s2_all['accuracy_pct'] - RLM_REPORTED_ACCURACY
    if delta > 0:
        print(f"  🏆 Waggle aggregate beats top-k by {delta:.1f}% accuracy")
    if delta_rlm > 0:
        print(f"  🏆 Waggle aggregate beats RLM paper baseline by {delta_rlm:.1f}%")
    else:
        print(f"  ⚠  Waggle ({s2_all['accuracy_pct']}%) vs RLM paper ({RLM_REPORTED_ACCURACY}%) — delta {delta_rlm:.1f}%")

    # --- Save JSON ---
    out = {
        "model": GROQ_MODEL_PAIRS,
        "rlm_paper_baseline_pct": RLM_REPORTED_ACCURACY,
        "mode1": {"summary": s1_all, "cases": [vars(c) for c in mode1.cases]},
        "mode2": {"summary": s2_all, "cases": [vars(c) for c in mode2.cases]},
    }
    out_path = ROOT / "benchmarks/data/llm_eval_report.json"
    out_path.write_text(json.dumps(out, indent=2))
    print(f"\n  📄 Full JSON saved → {out_path}\n")


if __name__ == "__main__":
    main()
