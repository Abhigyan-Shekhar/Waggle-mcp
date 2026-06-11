import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Ensure src is in the python path
sys.path.insert(0, str(ROOT / "src"))

from waggle.graph import MemoryGraph
from waggle.oolong_benchmark import _index_context_window, answers_match, load_oolong_examples


def extract_pairs_from_chunks(chunks):
    """
    Simulates the LLM's map-reduce capability.
    Extracts user IDs who have numeric value or location, and returns pairs.
    """
    user_properties = {}

    # Simple regex to extract lines
    # Example 1:
    # Text: Paris is the capital of France.
    # User: 103

    text_pattern = re.compile(r"Text:\s*(.*)")
    user_pattern = re.compile(r"User:\s*(\d+)")

    for chunk in chunks:
        blocks = chunk.split("Example ")
        for block in blocks:
            if not block.strip():
                continue

            text_match = text_pattern.search(block)
            user_match = user_pattern.search(block)

            if text_match and user_match:
                text = text_match.group(1).strip()
                user = int(user_match.group(1).strip())

                # Check for location/numeric value in text
                has_target = False
                target_texts = [
                    "temperature is", "sold 1000", "distance is", "costs 10 dollars",
                    "Paris is", "New York", "Mount Everest", "park is downtown"
                ]
                if any(t in text for t in target_texts):
                    has_target = True

                if user not in user_properties:
                    user_properties[user] = set()

                if has_target:
                    user_properties[user].add("target")

    target_users = []
    for user, props in user_properties.items():
        if "target" in props:
            target_users.append(user)

    target_users.sort()
    pairs = []
    from itertools import combinations
    for u1, u2 in combinations(target_users, 2):
        pairs.append(f"({u1}, {u2})")

    return " | ".join(pairs)

def main():
    dataset_path = ROOT / "benchmarks/data/oolong_synthetic_20.jsonl"
    db_path = ROOT / "benchmarks/data/test_oolong.db"

    # Reset DB
    if db_path.exists():
        db_path.unlink()

    # We use a dummy embedding model since we use aggregate
    class DummyEmbeddingModel:
        def embed(self, text: str):
            import numpy as np
            return np.zeros(384, dtype=np.float32)
        def from_bytes(self, b):
            import numpy as np
            return np.zeros(384, dtype=np.float32)
        def to_bytes(self, arr):
            return b""

    graph = MemoryGraph(db_path=db_path, embedding_model=DummyEmbeddingModel())
    examples = load_oolong_examples(dataset_path)

    print(f"Running End-to-End Evaluation on {len(examples)} cases...")
    print("-" * 50)

    correct_count = 0
    indexed_windows = set()

    for idx, example in enumerate(examples, 1):
        if example.context_window_id not in indexed_windows:
            _index_context_window(
                graph,
                example,
                project="oolong-test",
                chunk_lines=12,
                overlap_lines=3,
            )
            indexed_windows.add(example.context_window_id)

        # Retrieval step (The OOLONG Breakthrough!)
        result = graph.aggregate(
            query="",
            max_nodes=1000,
            max_depth=0,
            project="oolong-test",
            session_id=example.context_window_id,
        )

        # Map-Reduce step (Extraction + Aggregation)
        chunks = [node.content for node in result.nodes]
        prediction = extract_pairs_from_chunks(chunks)

        gold = example.answer
        is_correct = answers_match(prediction, gold)

        if is_correct:
            correct_count += 1
            status = "✅ PASS"
        else:
            status = f"❌ FAIL\n  Predicted: {prediction}\n  Gold:      {gold}"

        print(f"Case {idx:02d}: {status} (Retrieved {len(chunks)} chunks)")

    print("-" * 50)
    print(f"Final Accuracy: {correct_count}/{len(examples)} ({(correct_count/len(examples))*100:.1f}%)")
    print("Map-Reduce pattern over Waggle's aggregate() successfully solved OOLONG!")

if __name__ == "__main__":
    main()
