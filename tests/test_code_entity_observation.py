"""Tests for code-entity extraction inside observe_conversation candidates."""

from __future__ import annotations

from waggle.intelligence import extract_conversation_candidates
from waggle.models import NodeType


def test_extract_conversation_candidates_includes_code_entities() -> None:
    assistant = """Here's the implementation:

```python
class UserService:
    def authenticate(self, user):
        return True
```
"""
    candidates = extract_conversation_candidates(
        user_message="Implement user authentication for our API",
        assistant_response=assistant,
    )

    code_entities = [
        c
        for c in candidates
        if c["node_type"] == NodeType.ENTITY and "code-entity" in c.get("tags", [])
    ]
    names = {str(c["label"]) for c in code_entities}
    assert "UserService" in names
    assert "authenticate" in names
    assert any("language:python" in c["tags"] for c in code_entities)


def test_prose_only_turn_has_no_code_entity_tags() -> None:
    candidates = extract_conversation_candidates(
        user_message="What database should we use?",
        assistant_response="PostgreSQL is a solid default for this workload.",
    )
    assert not any("code-entity" in c.get("tags", []) for c in candidates)