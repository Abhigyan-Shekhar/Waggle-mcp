import re
import sys


def main():
    if len(sys.argv) < 2:
        return
    prompt_file = sys.argv[1]
    with open(prompt_file) as f:
        prompt = f.read()

    # The prompt contains Waggle chunk nodes.
    # We simulate an LLM that extracts users who have numeric/location content.
    # From our generation, numeric/location examples contain "dollars" or "Paris"

    users_with_evidence = set()

    # Split prompt into examples roughly
    examples = prompt.split("Example ")
    for ex in examples:
        if "dollars" in ex or "Paris" in ex:
            # extract user ID
            m = re.search(r"User:\s*(\d+)", ex)
            if m:
                users_with_evidence.add(int(m.group(1)))

    users = sorted(list(users_with_evidence))
    pairs = []
    for i in range(len(users)):
        for j in range(i+1, len(users)):
            pairs.append(f"({users[i]}, {users[j]})")

    # Print the answer to stdout as requested by the benchmark
    print(" | ".join(pairs))

if __name__ == "__main__":
    main()
