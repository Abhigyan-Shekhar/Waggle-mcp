import json
import random
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Categories
CATEGORIES = [
    "description and abstract concept",
    "entity",
    "human being",
    "numeric value",
    "location",
    "abbreviation"
]

# Generators for each category
def generate_text(category):
    if category == "description and abstract concept":
        return random.choice([
            "Freedom is a state of mind.",
            "The concept of time is relative.",
            "Justice is blind.",
            "Democracy is a form of government."
        ])
    elif category == "entity":
        return random.choice([
            "The company reported record profits.",
            "The UN held a summit.",
            "Google released a new product.",
            "The committee approved the proposal."
        ])
    elif category == "human being":
        return random.choice([
            "Albert Einstein was a physicist.",
            "Marie Curie won two Nobel prizes.",
            "The president gave a speech.",
            "John Doe is a software engineer."
        ])
    elif category == "numeric value":
        return random.choice([
            "The temperature is 75 degrees.",
            "We sold 1000 units yesterday.",
            "The distance is 50 miles.",
            "It costs 10 dollars."
        ])
    elif category == "location":
        return random.choice([
            "Paris is the capital of France.",
            "The office is in New York.",
            "Mount Everest is the highest mountain.",
            "The park is downtown."
        ])
    elif category == "abbreviation":
        return random.choice([
            "NASA stands for National Aeronautics and Space Administration.",
            "WHO is the World Health Organization.",
            "CEO means Chief Executive Officer.",
            "USA is the United States of America."
        ])
    return "Unknown."

def generate_case(num_users=5, num_examples=40):
    users = list(range(100, 100 + num_users))
    user_properties = {u: set() for u in users}

    text_blocks = []

    for i in range(1, num_examples + 1):
        user = random.choice(users)
        category = random.choice(CATEGORIES)
        text = generate_text(category)

        # Track properties
        user_properties[user].add(category)

        date = f"2026-04-{random.randint(1, 30):02d}"

        block = f"Example {i}:\nText: {text}\nUser: {user}\nDate: {date}"
        text_blocks.append(block)

    context_window_text = "\n\n".join(text_blocks)

    # Target users: those who have at least one numeric value OR location
    target_users = []
    for user, props in user_properties.items():
        if "numeric value" in props or "location" in props:
            target_users.append(user)

    # Generate pairs
    target_users.sort()
    pairs = list(combinations(target_users, 2))
    answer_list = [f"({u1}, {u2})" for u1, u2 in pairs]

    question = (
        "Each of the questions can be labelled as one of the labels: description and abstract concept, "
        "entity, human being, numeric value, location, abbreviation.\n\n"
        "In the above data, list all pairs of user IDs (no duplicate pairs, list lower ID first) "
        "where both users have at least one instance with a numeric value or location.\n\n"
        "In your answer, list all pairs in the format (user_id_1, user_id_2), separated by newlines."
    )

    # Store answer directly as Python list literal string like `['(101, 102)', '(101, 103)']` to match evaluation style
    # The evaluation in oolong_benchmark uses `answers_match` which handles these list strings
    answer_str = repr(answer_list)

    return {
        "context_window_text": context_window_text + "\n",
        "question": question,
        "answer": answer_str,
        "answer_type": "list",
        "task_group": "oolong-pairs"
    }

def main():
    output_path = ROOT / "benchmarks/data/oolong_synthetic_20.jsonl"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    print("Generating 20 cases for OOLONG-Pairs...")
    cases = []

    # We want varying complexities
    for i in range(20):
        # 5 cases with small context, 10 medium, 5 large
        if i < 5:
            num_users = random.randint(3, 5)
            num_examples = random.randint(20, 30)
        elif i < 15:
            num_users = random.randint(6, 12)
            num_examples = random.randint(50, 100)
        else:
            num_users = random.randint(15, 25)
            num_examples = random.randint(150, 250)

        cases.append(generate_case(num_users=num_users, num_examples=num_examples))

    with open(output_path, "w") as f:
        for case in cases:
            f.write(json.dumps(case) + "\n")

    print(f"Dataset generated at {output_path}")

if __name__ == "__main__":
    main()
