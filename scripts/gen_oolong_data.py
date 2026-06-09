import json
import random

# Generate 20 cases
cases = []
for i in range(20):
    # We want a large context. We will generate 100 examples per case to make it long.
    # 2-4 users will have numeric/location.
    users = list(range(100, 120))
    target_users = random.sample(users, random.randint(3, 5))
    target_users.sort()

    gold_pairs = []
    for u1 in range(len(target_users)):
        for u2 in range(u1 + 1, len(target_users)):
            gold_pairs.append(f"({target_users[u1]}, {target_users[u2]})")

    examples = []
    for j in range(100):
        u = random.choice(users)
        if u in target_users and random.random() < 0.2:
            # Generate numeric/location
            if random.random() < 0.5:
                text = f"I have {random.randint(10, 1000)} dollars in my pocket."
            else:
                text = "The city of Paris is nice."
        else:
            text = "This is an abstract concept or general entity."

        examples.append(f"Example {j+1}:\nText: {text}\nUser: {u}\nDate: 2026-04-{random.randint(1,28):02d}")

    context_text = "\n\n".join(examples)
    question = "Each of the questions can be labelled as one of the labels: description and abstract concept, entity, human being, numeric value, location, abbreviation.\n\nIn the above data, list all pairs of user IDs (no duplicate pairs, list lower ID first) where both users have at least one instance with a numeric value or location.\n\nIn your answer, list all pairs in the format (user_id_1, user_id_2), separated by newlines."

    cases.append({
        "context_window_text": context_text,
        "question": question,
        "answer": gold_pairs,
        "answer_type": "list",
        "task_group": "oolong-pairs"
    })

with open("benchmarks/data/oolong_20.jsonl", "w") as f:
    for c in cases:
        f.write(json.dumps(c) + "\n")
