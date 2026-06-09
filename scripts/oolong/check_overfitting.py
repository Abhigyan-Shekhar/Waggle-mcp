"""
Overfitting diagnostic for OOLONG-synth results.
Checks:
  1. Gold answer distribution (label bias)
  2. Whether LLM is guessing majority class
  3. Context window text vs labelled text leakage
  4. LLM predictions vs gold for every case
  5. Whether 'user' task 100% is real reasoning or lucky guessing
"""
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REAL_DATASET  = ROOT / "benchmarks/data/oolong_real_30.jsonl"
REPORT_JSON   = ROOT / "benchmarks/data/llm_eval_report.json"

# ---------------------------------------------------------------------------
# Load data
# ---------------------------------------------------------------------------
examples = []
with REAL_DATASET.open() as f:
    for line in f:
        examples.append(json.loads(line))

report = json.loads(Path(REPORT_JSON).read_text()) if Path(REPORT_JSON).exists() else None

# ---------------------------------------------------------------------------
# 1. Gold answer distribution
# ---------------------------------------------------------------------------
print("=" * 70)
print("1. GOLD ANSWER DISTRIBUTION")
print("=" * 70)
all_answers = []
for e in examples:
    ans = e['answer']
    if isinstance(ans, list):
        all_answers.extend(str(x).strip().lower() for x in ans)
    else:
        all_answers.append(str(ans).strip().lower())

cnt = Counter(all_answers)
total = len(all_answers)
print(f"Total gold answers: {total}")
print(f"Unique values: {len(cnt)}")
print("\nTop answers (value, count, % of total):")
for val, count in cnt.most_common(20):
    pct = count / total * 100
    print(f"  {val!r:<30} {count:>4}  ({pct:.1f}%)")

majority_val, majority_cnt = cnt.most_common(1)[0]
majority_pct = majority_cnt / total * 100
print(f"\n⚠  Majority class = {majority_val!r} at {majority_pct:.1f}%")
print(f"   A model that always outputs '{majority_val}' would score {majority_pct:.1f}%")

# ---------------------------------------------------------------------------
# 2. Per-task-group gold distribution
# ---------------------------------------------------------------------------
print("\n" + "=" * 70)
print("2. GOLD ANSWERS BY TASK GROUP")
print("=" * 70)
for grp in ("user", "counting"):
    grp_examples = [e for e in examples if e["task_group"] == grp]
    grp_answers   = []
    for e in grp_examples:
        ans = e['answer']
        if isinstance(ans, list):
            grp_answers.extend(str(x).strip().lower() for x in ans)
        else:
            grp_answers.append(str(ans).strip().lower())
    cnt2 = Counter(grp_answers)
    print(f"\n  [{grp.upper()}] {len(grp_examples)} cases, {len(grp_answers)} gold labels")
    for val, c in cnt2.most_common(10):
        print(f"    {val!r:<30} {c:>3}  ({c/len(grp_answers)*100:.0f}%)")

# ---------------------------------------------------------------------------
# 3. Check for labelled-text leakage
# ---------------------------------------------------------------------------
print("\n" + "=" * 70)
print("3. LABEL LEAKAGE CHECK")
print("=" * 70)
leakage_count = 0
for e in examples:
    ctx = e.get("context_window_text", "")
    # The labelled version contains explicit "Label: " strings
    if "Label:" in ctx or "label:" in ctx or "classified as" in ctx.lower():
        leakage_count += 1
        print(f"  ⚠ {e['example_id']}: context contains label keywords")
        print(f"    first 200 chars: {ctx[:200]}")
print(f"\n  Leakage detected in {leakage_count}/{len(examples)} examples")
if leakage_count == 0:
    print("  ✅ No label leakage in context_window_text field")

# ---------------------------------------------------------------------------
# 4. LLM predictions vs gold (from saved report)
# ---------------------------------------------------------------------------
if report:
    print("\n" + "=" * 70)
    print("4. LLM PREDICTIONS vs GOLD — MODE 2 (aggregate) — SYNTH CASES")
    print("=" * 70)
    synth_cases = [c for c in report["mode2"]["cases"] if c["dataset"] == "synth"]
    print(f"\n  {'ID':<12} {'Task':<10} {'EM':<4} {'Gold':<35} {'Predicted':<50}")
    print(f"  {'-'*110}")
    for c in synth_cases:
        em = "✅" if c["exact_match"] else "❌"
        gold = str(c["gold_answer"])[:35]
        pred = c["predicted_answer"].replace("\n", " ")[:50]
        # find task group from examples
        ex = next((e for e in examples if e["example_id"] == c["example_id"]), {})
        tg = ex.get("task_group", "?")[:9]
        print(f"  {c['example_id']:<12} {tg:<10} {em:<4} {gold:<35} {pred}")

    # Detailed fail analysis
    fails = [c for c in synth_cases if not c["exact_match"]]
    print(f"\n  FAILED cases ({len(fails)}):")
    for c in fails:
        ex = next((e for e in examples if e["example_id"] == c["example_id"]), {})
        print(f"\n  --- {c['example_id']} [{ex.get('task_group')}] ---")
        print(f"  Q: {ex.get('question', '')[:150]}")
        print(f"  Gold: {c['gold_answer']}")
        print(f"  Pred: {c['predicted_answer'][:200]}")

# ---------------------------------------------------------------------------
# 5. Baseline: what score does "always output majority class" get?
# ---------------------------------------------------------------------------
print("\n" + "=" * 70)
print("5. MAJORITY-CLASS BASELINE vs ACTUAL LLM SCORE")
print("=" * 70)
if report:
    for mode_key, label in [("mode1", "Mode1 top-k"), ("mode2", "Mode2 aggregate")]:
        synth_cases = [c for c in report[mode_key]["cases"] if c["dataset"] == "synth"]
        correct = sum(1 for c in synth_cases if c["exact_match"])
        pct = correct / len(synth_cases) * 100 if synth_cases else 0
        print(f"  {label}: {correct}/{len(synth_cases)} = {pct:.1f}%")

    # Compute majority-class baseline per case
    majority_correct = 0
    for e in examples:
        ans = e['answer']
        ans_str = str(ans[0] if isinstance(ans, list) else ans).strip().lower()
        if ans_str == majority_val:
            majority_correct += 1
    maj_pct = majority_correct / len(examples) * 100
    print(f"\n  Majority-class baseline (always say '{majority_val}'): "
          f"{majority_correct}/{len(examples)} = {maj_pct:.1f}%")

    if abs(66.7 - maj_pct) < 5:
        print(f"\n  🚨 OVERFITTING ALERT: LLM score ({66.7}%) ≈ majority-class baseline ({maj_pct:.1f}%)")
        print("     The model may be guessing the majority label, not reasoning.")
    else:
        print(f"\n  ✅ LLM score (66.7%) is {66.7 - maj_pct:+.1f}% above majority-class baseline ({maj_pct:.1f}%)")
        print("     Score is not explainable by label bias alone.")

print()
