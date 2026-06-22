"""Tests for Phase 3 Graphify-inspired features:

G. Code-aware extraction (code_extraction + observe_conversation linking)
H. Unified code+conversation query (is_code_query, code_entity_boost, query boost)
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np

from waggle.code_extraction import CodeEntity, extract_code_entities
from waggle.graph import MemoryGraph
from waggle.intelligence import (
    code_entity_boost,
    code_query_identifiers,
    is_code_entity_node,
    is_code_query,
)
from waggle.models import NodeType


class FakeEmbeddingModel:
    model_name = "fake-model"
    model_id = "fake-model:deterministic-v1"

    def embed(self, text: str):
        seed = int.from_bytes(hashlib.sha256(text.strip().lower().encode("utf-8")).digest()[:8], "big")
        vector = np.random.default_rng(seed).standard_normal(64).astype(np.float32)
        norm = float(np.linalg.norm(vector))
        return vector / norm if norm > 0 else vector

    def to_bytes(self, embedding) -> bytes:
        return np.asarray(embedding, dtype=np.float32).tobytes()

    def from_bytes(self, data: bytes):
        return np.frombuffer(data, dtype=np.float32)

    def cosine_similarity(self, a, b) -> float:
        an = float(np.linalg.norm(a))
        bn = float(np.linalg.norm(b))
        return float(np.dot(a, b) / (an * bn)) if an and bn else 0.0


def make_graph(tmp_path: Path) -> MemoryGraph:
    return MemoryGraph(tmp_path / "memory.db", FakeEmbeddingModel())


# ---------------------------------------------------------------------------
# Feature G: code_extraction module
# ---------------------------------------------------------------------------


class TestCodeExtraction:
    def test_no_code_blocks_returns_empty(self) -> None:
        assert extract_code_entities("just a plain sentence with no code") == []

    def test_python_function_and_class(self) -> None:
        text = "Here:\n```python\nclass UserService:\n    def authenticate(self, user):\n        return True\n```"
        entities = extract_code_entities(text)
        names = {(e.name, e.entity_type) for e in entities}
        assert ("UserService", "class") in names
        assert ("authenticate", "function") in names

    def test_python_async_function(self) -> None:
        text = "```py\nasync def fetch_data(url):\n    pass\n```"
        entities = extract_code_entities(text)
        assert ("fetch_data", "function") in {(e.name, e.entity_type) for e in entities}

    def test_javascript_function_and_class(self) -> None:
        text = "```js\nexport class Widget {}\nfunction render(props) { return null; }\n```"
        entities = extract_code_entities(text)
        names = {(e.name, e.entity_type) for e in entities}
        assert ("Widget", "class") in names
        assert ("render", "function") in names

    def test_javascript_arrow_const(self) -> None:
        text = "```javascript\nconst handleClick = (e) => { console.log(e); };\n```"
        entities = extract_code_entities(text)
        assert ("handleClick", "function") in {(e.name, e.entity_type) for e in entities}

    def test_multiple_blocks_deduped(self) -> None:
        text = "```python\ndef foo():\n    pass\n```\nand\n```python\ndef foo():\n    pass\n```"
        entities = extract_code_entities(text)
        assert sum(1 for e in entities if e.name == "foo") == 1

    def test_language_detected_without_hint(self) -> None:
        text = "```\ndef compute_total(items):\n    return sum(items)\n```"
        entities = extract_code_entities(text)
        assert any(e.name == "compute_total" for e in entities)

    def test_malformed_code_does_not_raise(self) -> None:
        text = "```python\ndef broken(:\n  ???\n```"
        # Should not raise, may return partial/empty
        extract_code_entities(text)

    def test_entity_carries_language_and_snippet(self) -> None:
        text = "```python\ndef greet(name):\n    pass\n```"
        entity = next(e for e in extract_code_entities(text) if e.name == "greet")
        assert isinstance(entity, CodeEntity)
        assert entity.language == "python"
        assert "greet" in entity.snippet


# ---------------------------------------------------------------------------
# Feature G: observe_conversation integration
# ---------------------------------------------------------------------------


class TestObserveCodeLinking:
    def test_prose_turn_is_noop(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        result = graph.observe_conversation(
            user_message="We should use PostgreSQL.",
            assistant_response="Agreed, PostgreSQL is solid.",
        )
        assert result.code_entities_extracted == 0

    def test_code_block_creates_entity_nodes(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        result = graph.observe_conversation(
            user_message="Can you review this?\n```python\ndef calculate_tax(amount):\n    return amount * 0.2\n```",
            assistant_response="Looks fine.",
        )
        assert result.code_entities_extracted >= 1
        recent = graph.list_recent_nodes(limit=20)
        assert any(n.label == "calculate_tax" and n.node_type == NodeType.ENTITY for n in recent)

    def test_code_entity_tagged_and_flagged(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        graph.observe_conversation(
            user_message="```python\ndef widget_factory():\n    pass\n```",
            assistant_response="ok",
        )
        recent = graph.list_recent_nodes(limit=20)
        code_node = next(n for n in recent if n.label == "widget_factory")
        assert "code" in (code_node.tags or [])
        assert code_node.metadata.get("code_entity") is True

    def test_code_entity_reuses_existing_label(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        # Pre-create an ENTITY node named "authenticate" (as if from Graphify)
        existing = graph.add_node(
            label="authenticate",
            content="def authenticate(user)",
            node_type=NodeType.ENTITY,
            tags=["graphify"],
            metadata={"graphify_source": True},
        ).node
        graph.observe_conversation(
            user_message="```python\ndef authenticate(user):\n    return True\n```",
            assistant_response="ok",
        )
        # Should not create a duplicate "authenticate" ENTITY node
        recent = graph.list_recent_nodes(limit=50)
        auth_nodes = [n for n in recent if n.label == "authenticate" and n.node_type == NodeType.ENTITY]
        assert len(auth_nodes) == 1
        assert auth_nodes[0].id == existing.id


# ---------------------------------------------------------------------------
# Feature H: code-aware query helpers
# ---------------------------------------------------------------------------


class TestCodeQueryHelpers:
    def test_is_code_query_positive(self) -> None:
        assert is_code_query("what does authenticate() do")
        assert is_code_query("explain the calculate_tax function")
        assert is_code_query("how does db.connect work")
        assert is_code_query("the getUserName method")

    def test_is_code_query_negative(self) -> None:
        assert not is_code_query("what did we decide for lunch")
        assert not is_code_query("the meeting is tomorrow")

    def test_code_query_identifiers(self) -> None:
        ids = code_query_identifiers("how does db.connect() relate to authenticate")
        assert "db.connect" in ids
        assert "connect" in ids  # bare final segment

    def test_code_query_identifiers_snake_and_camel(self) -> None:
        assert "calculate_tax" in code_query_identifiers("the calculate_tax helper")
        assert "getusername" in {i.lower() for i in code_query_identifiers("call getUserName now")}

    def test_is_code_entity_node_by_metadata(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        node = graph.add_node(
            label="parse", content="def parse()", node_type=NodeType.ENTITY, metadata={"code_entity": True}
        ).node
        assert is_code_entity_node(node)

    def test_is_code_entity_node_by_graphify(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        node = graph.add_node(
            label="User", content="class User", node_type=NodeType.ENTITY, metadata={"graphify_source": True}
        ).node
        assert is_code_entity_node(node)

    def test_non_code_node_not_flagged(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        node = graph.add_node(label="lunch plan", content="tacos", node_type=NodeType.FACT).node
        assert not is_code_entity_node(node)

    def test_code_entity_boost_applies(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        node = graph.add_node(
            label="authenticate", content="def authenticate", node_type=NodeType.ENTITY, metadata={"code_entity": True}
        ).node
        assert code_entity_boost("how does authenticate() work", node) > 0.0

    def test_code_entity_boost_zero_for_prose_query(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        node = graph.add_node(
            label="authenticate", content="def authenticate", node_type=NodeType.ENTITY, metadata={"code_entity": True}
        ).node
        assert code_entity_boost("what is for lunch", node) == 0.0

    def test_code_entity_boost_zero_for_non_code_node(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        node = graph.add_node(label="authenticate", content="a plan", node_type=NodeType.FACT).node
        # Even though label matches, a non-code node gets no boost
        assert code_entity_boost("how does authenticate() work", node) == 0.0


# ---------------------------------------------------------------------------
# Feature H: end-to-end unified retrieval
# ---------------------------------------------------------------------------


class TestUnifiedQuery:
    def test_code_entity_surfaces_for_code_query(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        # A code entity (as if imported from Graphify)
        graph.add_node(
            label="calculate_tax",
            content="def calculate_tax(amount): return amount * 0.2",
            node_type=NodeType.ENTITY,
            tags=["code"],
            metadata={"code_entity": True},
        )
        # Some unrelated conversation memory
        graph.add_node(label="lunch", content="we had tacos", node_type=NodeType.FACT)
        graph.add_node(label="weather", content="it was sunny", node_type=NodeType.FACT)

        result = graph.query(query="how does calculate_tax() work", max_nodes=5, retrieval_mode="graph")
        labels = [n.label for n in result.nodes]
        assert "calculate_tax" in labels

    def test_prose_query_still_works(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        graph.add_node(label="lunch decision", content="we chose tacos for the team lunch", node_type=NodeType.DECISION)
        result = graph.query(query="what did we decide for lunch", max_nodes=5, retrieval_mode="graph")
        assert any("lunch" in n.label.lower() for n in result.nodes)


class TestGetRelatedReverseEdge:
    """Regression: get_related must not crash on reverse-direction edges.

    A derived_from edge B->A means A is reachable from B but not vice versa in
    the directed graph. get_related(A) expands to include B (a predecessor), and
    the distance calc must use the undirected view to avoid NetworkXNoPath.
    """

    def test_get_related_with_reverse_edge(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        a = graph.add_node(label="target", content="target node", node_type=NodeType.FACT).node.id
        b = graph.add_node(label="source", content="source node", node_type=NodeType.CONCEPT).node.id
        # Edge points B -> A (reverse relative to querying from A)
        graph.add_edge(source_id=b, target_id=a, relationship="derived_from")

        result = graph.get_related(node_id=a, max_depth=2)
        node_ids = {n.id for n in result.nodes}
        assert a in node_ids
        assert b in node_ids  # predecessor included, no crash

    def test_get_node_history_with_reverse_edge(self, tmp_path: Path) -> None:
        graph = make_graph(tmp_path)
        a = graph.add_node(label="anchor", content="anchor", node_type=NodeType.FACT).node.id
        b = graph.add_node(label="derived", content="derived", node_type=NodeType.CONCEPT).node.id
        graph.add_edge(source_id=b, target_id=a, relationship="derived_from")
        # Should not raise
        history = graph.get_node_history(node_id=a, max_depth=2)
        assert history.node.id == a
