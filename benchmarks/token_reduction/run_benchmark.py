import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from rlm.utils.token_utils import count_tokens

SCENARIOS_DIR = Path(__file__).parent / "scenarios"


def estimate_tokens(text: str) -> int:
    return count_tokens(
        [{"role": "user", "content": text}],
        model_name="gpt-4o",
    )


def run():
    print(f"{'Scenario':25} {'Baseline':10} {'Waggle':10} {'Reduction %'}")
    print("-" * 60)

    for file in SCENARIOS_DIR.glob("*.json"):
        with open(file, encoding="utf-8") as f:
            data = json.load(f)

        turns = data["turns"]

        baseline = sum(estimate_tokens(" ".join(turns[: i + 1])) for i in range(len(turns)))

        waggle = sum(estimate_tokens(turn) for turn in turns)

        reduction = ((baseline - waggle) / baseline) * 100 if baseline else 0

        print(f"{data['name']:25} {baseline:<10} {waggle:<10} {reduction:.1f}%")


if __name__ == "__main__":
    run()
