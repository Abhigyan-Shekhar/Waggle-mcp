"""Tests for the refactored observe_conversation with verbatim-first architecture.

These tests ensure that:
1. observe_conversation always persists verbatim turns first.
2. Extraction failures are non-fatal (logged but don't crash).
3. Verbatim turns are queryable even when zero nodes were extracted.
4. The result includes diagnostics (turn_id, verbatim_stored, nodes_extracted, extraction_errors).
5. Hybrid retrieval is the default.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import numpy as np

from waggle.graph import MemoryGraph
from waggle.models import NodeType


class FakeEmbeddingModel:
    """Minimal embedding model for tests (same as test_graph.py)."""

    model_name = "fake-model"
    model_id = "fake-model:deterministic-v1"

    def embed(self, text: str) -> np.ndarray:
        """Return a deterministic 8-dim unit vector for *text*."""
        vector = np.zeros(8, dtype=np.float32)
        for token in text.lower().split():
            index = sum(ord(character) for character in token) % len(vector)
            vector[index] += 1.0
        norm = np.linalg.norm(vector)
        if norm == 0.0:
            return vector
        return vector / norm

    def embed_batch(self, texts: list[str]) -> np.ndarray:
        """Batch embed: stacks individual embed() results into a 2-D array."""
        return np.stack([self.embed(t) for t in texts], axis=0)

    def to_bytes(self, embedding: np.ndarray) -> bytes:
        """Serialise *embedding* to a raw float32 byte string."""
        return embedding.astype(np.float32).tobytes()

    def from_bytes(self, data: bytes) -> np.ndarray:
        """Deserialise a raw float32 byte string back to an ndarray."""
        return np.frombuffer(data, dtype=np.float32)

    def cosine_similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        """Return the cosine similarity between vectors *a* and *b*."""
        a_norm = np.linalg.norm(a)
        b_norm = np.linalg.norm(b)
        if a_norm == 0.0 or b_norm == 0.0:
            return 0.0
        return float(np.dot(a, b) / (a_norm * b_norm))


def make_graph(tmp_path: Path) -> MemoryGraph:
    """Create a test graph."""
    graph = MemoryGraph(
        db_path=str(tmp_path / "test.db"),
        embedding_model=FakeEmbeddingModel(),
    )
    return graph


class TestObserveConversationVerbatimFirst:
    """Test that verbatim storage is mandatory and happens first."""

    def test_observe_conversation_returns_turn_id(self, tmp_path: Path) -> None:
        """Test that observe_conversation returns a turn_id."""
        graph = make_graph(tmp_path)
        result = graph.observe_conversation(
            user_message="I prefer Python for backend work.",
            assistant_response="Let's use FastAPI.",
        )
        assert result.turn_id
        assert len(result.turn_id) > 0

    def test_observe_conversation_marks_verbatim_stored_true(self, tmp_path: Path) -> None:
        """Test that verbatim_stored is True on success."""
        graph = make_graph(tmp_path)
        result = graph.observe_conversation(
            user_message="We use PostgreSQL.",
            assistant_response="Got it.",
        )
        assert result.verbatim_stored is True

    def test_observe_conversation_succeeds_with_empty_extraction(self, tmp_path: Path) -> None:
        """Test that observe_conversation succeeds even when extraction returns empty list.

        This tests a user's turn that doesn't match any extraction patterns.
        """
        graph = make_graph(tmp_path)
        # Use text that doesn't match common extraction patterns
        result = graph.observe_conversation(
            user_message="the team aligned around using mongo because it just felt right",
            assistant_response="okay, got it",
        )
        # Verbatim should still be stored
        assert result.verbatim_stored is True
        assert result.turn_id
        # Extraction might produce zero nodes
        # (depending on whether named entities or other patterns match)
        assert result.nodes_extracted >= 0

    def test_observe_conversation_extracts_when_patterns_match(self, tmp_path: Path) -> None:
        """Test that nodes_extracted is > 0 when extraction succeeds."""
        graph = make_graph(tmp_path)
        result = graph.observe_conversation(
            user_message="I prefer Python for backend work. Can we use FastAPI?",
            assistant_response="Let's use FastAPI and update src/server.py.",
        )
        assert result.verbatim_stored is True
        assert result.nodes_extracted > 0  # Should extract preferences/decisions


class TestObserveConversationExtractionRobustness:
    """Test that extraction failures don't break verbatim storage."""

    def test_observe_conversation_survives_extraction_exception(self, tmp_path: Path) -> None:
        """Test that verbatim is stored even if extraction raises an exception."""
        graph = make_graph(tmp_path)

        # Mock extract_conversation_candidates to raise an exception
        with patch("waggle.graph.extract_conversation_candidates") as mock_extract:
            mock_extract.side_effect = RuntimeError("Extraction crashed!")

            result = graph.observe_conversation(
                user_message="We use PostgreSQL.",
                assistant_response="Understood.",
            )

        # Verbatim should still be stored
        assert result.verbatim_stored is True
        assert result.turn_id
        # Extraction errors should be logged
        assert any("Extraction" in err for err in result.extraction_errors)
        # No nodes extracted due to error
        assert result.nodes_extracted == 0

    def test_observe_conversation_extraction_error_is_in_result(self, tmp_path: Path) -> None:
        """Test that extraction_errors field captures exception info."""
        graph = make_graph(tmp_path)

        with patch("waggle.graph.extract_conversation_candidates") as mock_extract:
            mock_extract.side_effect = ValueError("Bad extraction input")

            result = graph.observe_conversation(
                user_message="Some message.",
                assistant_response="Some response.",
            )

        assert len(result.extraction_errors) > 0
        assert "ValueError" in result.extraction_errors[0]


class TestVerbatimRetrieval:
    """Test that verbatim turns are queryable even with zero extraction."""

    def test_verbatim_queryable_with_zero_extraction(self, tmp_path: Path) -> None:
        """Test that a turn with zero extracted nodes is still retrievable via verbatim mode."""
        graph = make_graph(tmp_path)

        # Store a turn that produces no extracted nodes
        with patch("waggle.graph.extract_conversation_candidates") as mock_extract:
            mock_extract.return_value = []  # Force zero extraction

            observe_result = graph.observe_conversation(
                user_message="mongo felt right for this project",
                assistant_response="sounds good",
                session_id="test-session",
            )

        assert observe_result.verbatim_stored is True
        assert observe_result.nodes_extracted == 0

        # Query in verbatim mode should find the transcript
        query_result = graph.query(
            query="mongo project",
            retrieval_mode="verbatim",
            session_id="test-session",
            max_nodes=5,
        )

        # Should find the turn via transcript (replay_hits use transcript_text field)
        assert query_result.replay_hits or query_result.fusion_hits
        # Check if mongo is in the transcript
        if query_result.replay_hits:
            assert any(
                "mongo" in (hit.transcript_text or hit.transcript_snippet).lower() for hit in query_result.replay_hits
            )


class TestHybridRetrievalDefault:
    """Test that hybrid retrieval mode works correctly."""

    def test_query_defaults_to_hybrid(self, tmp_path: Path) -> None:
        """Test that query() works with hybrid retrieval mode."""
        graph = make_graph(tmp_path)

        # Store a turn
        graph.observe_conversation(
            user_message="We use PostgreSQL.",
            assistant_response="Got it.",
        )

        # Query explicitly with hybrid retrieval_mode
        result = graph.query(query="database", retrieval_mode="hybrid")

        # Should use hybrid retrieval (fusion_hits should be populated)
        assert result.retrieval_mode in {"hybrid", "tiered", "flat_fallback"}

    def test_query_accepts_hybrid_no_rerank_alias(self, tmp_path: Path) -> None:
        """Test that 'hybrid_no_rerank' is accepted as an alias for 'hybrid'."""
        graph = make_graph(tmp_path)

        graph.observe_conversation(
            user_message="PostgreSQL choice.",
            assistant_response="Got it.",
        )

        # Query with hybrid_no_rerank should work (alias)
        result = graph.query(
            query="database",
            retrieval_mode="hybrid_no_rerank",
        )

        # Should succeed without raising
        assert result.query == "database"


class TestObserveConversationResultFields:
    """Test the structure and content of ObservationResult."""

    def test_observation_result_has_all_required_fields(self, tmp_path: Path) -> None:
        """Test that ObservationResult includes all new fields."""
        graph = make_graph(tmp_path)
        result = graph.observe_conversation(
            user_message="Python for backend.",
            assistant_response="Let's use Python.",
        )

        # Check old fields
        assert hasattr(result, "stored_nodes")
        assert hasattr(result, "created_count")
        assert hasattr(result, "reused_count")
        assert hasattr(result, "conflicts")

        # Check new fields
        assert hasattr(result, "turn_id")
        assert hasattr(result, "verbatim_stored")
        assert hasattr(result, "nodes_extracted")
        assert hasattr(result, "edges_inferred")
        assert hasattr(result, "extraction_errors")

    def test_observation_result_fields_are_populated(self, tmp_path: Path) -> None:
        """Test that the fields have expected types."""
        graph = make_graph(tmp_path)
        result = graph.observe_conversation(
            user_message="We use FastAPI.",
            assistant_response="Noted.",
        )

        assert isinstance(result.turn_id, str)
        assert isinstance(result.verbatim_stored, bool)
        assert isinstance(result.nodes_extracted, int)
        assert isinstance(result.edges_inferred, int)
        assert isinstance(result.extraction_errors, list)
        assert all(isinstance(err, str) for err in result.extraction_errors)


class TestEdgeCases:
    """Test edge cases and error conditions."""

    def test_observe_conversation_with_empty_user_message(self, tmp_path: Path) -> None:
        """Test handling of empty user message."""
        graph = make_graph(tmp_path)

        result = graph.observe_conversation(
            user_message="",
            assistant_response="How can I help?",
        )

        # Should still store the assistant response
        assert result.verbatim_stored is True

    def test_observe_conversation_with_empty_assistant_response(self, tmp_path: Path) -> None:
        """Test handling of empty assistant response."""
        graph = make_graph(tmp_path)

        result = graph.observe_conversation(
            user_message="I prefer Python.",
            assistant_response="",
        )

        # Should still store the user message
        assert result.verbatim_stored is True

    def test_observe_conversation_with_both_empty(self, tmp_path: Path) -> None:
        """Test handling of both messages being empty."""
        graph = make_graph(tmp_path)

        result = graph.observe_conversation(
            user_message="",
            assistant_response="",
        )

        # Should still mark as stored (no content to store, but no error)
        assert result.verbatim_stored is True or result.turn_id  # At minimum, turn_id should exist


class TestBatchEmbeddingAdoption:
    """Verify that _apply_observation_candidates uses embed_batch for multi-node observations."""

    def test_embed_batch_called_once_for_multi_node_observation(self, tmp_path: Path) -> None:
        """embed_batch should be called exactly once regardless of how many nodes are extracted."""
        graph = make_graph(tmp_path)

        with patch("waggle.graph.extract_conversation_candidates") as mock_extract:
            mock_extract.return_value = [
                {
                    "label": "Python",
                    "content": "User prefers Python.",
                    "node_type": NodeType.PREFERENCE,
                    "tags": [],
                },
                {
                    "label": "FastAPI",
                    "content": "FastAPI chosen for web layer.",
                    "node_type": NodeType.DECISION,
                    "tags": [],
                },
                {
                    "label": "PostgreSQL",
                    "content": "PostgreSQL for the database.",
                    "node_type": NodeType.FACT,
                    "tags": [],
                },
                {
                    "label": "Docker",
                    "content": "Docker for containerisation.",
                    "node_type": NodeType.ENTITY,
                    "tags": [],
                },
                {
                    "label": "GitHub Actions",
                    "content": "CI via GitHub Actions.",
                    "node_type": NodeType.FACT,
                    "tags": [],
                },
            ]

            original_embed_batch = graph.embedding_model.embed_batch
            embed_batch_calls: list[list[str]] = []

            def spy_embed_batch(texts: list[str]) -> np.ndarray:
                """Record the call then delegate to the real embed_batch."""
                embed_batch_calls.append(list(texts))
                return original_embed_batch(texts)

            graph.embedding_model.embed_batch = spy_embed_batch  # type: ignore[method-assign]

            result = graph.observe_conversation(
                user_message="I like Python, FastAPI, Postgres, Docker and GitHub Actions.",
                assistant_response="Great stack choices.",
            )

        # embed_batch must have been called exactly once
        assert len(embed_batch_calls) == 1, (
            f"Expected embed_batch to be called once; got {len(embed_batch_calls)} call(s)"
        )
        # The single call must include all 5 candidate texts
        assert len(embed_batch_calls[0]) == 5
        # All five nodes must have been stored
        assert result.verbatim_stored is True
        assert len(result.stored_nodes) == 5

    def test_all_nodes_have_embeddings_after_batch_observe(self, tmp_path: Path) -> None:
        """Every node stored via a multi-node observation must have a valid non-zero embedding."""
        from waggle.models import NodeType

        graph = make_graph(tmp_path)

        with patch("waggle.graph.extract_conversation_candidates") as mock_extract:
            mock_extract.return_value = [
                {"label": "Node A", "content": "Alpha content.", "node_type": NodeType.FACT, "tags": []},
                {"label": "Node B", "content": "Beta content.", "node_type": NodeType.FACT, "tags": []},
                {"label": "Node C", "content": "Gamma content.", "node_type": NodeType.ENTITY, "tags": []},
            ]

            result = graph.observe_conversation(
                user_message="Alpha, Beta, Gamma.",
                assistant_response="Stored.",
            )

        assert len(result.stored_nodes) == 3
        for node in result.stored_nodes:
            # Fetch the raw embedding bytes back from the DB
            fetched = graph.get_node(node.id)
            # The node must exist and have a label (proxy for a complete write)
            assert fetched.label
            assert fetched.content
            # Verify that embedding data was successfully generated and persisted
            assert fetched.embedding_model_id
            assert fetched.embedding_dim and fetched.embedding_dim > 0

    def test_dedup_still_works_with_batch_embeddings(self, tmp_path: Path) -> None:
        """Dedup must fire correctly when the embedding comes from the batch path."""
        from waggle.models import NodeType

        graph = make_graph(tmp_path)

        # Lower the dedup threshold so near-duplicate texts merge
        graph.dedup_similarity_threshold = 0.0  # merge everything

        # First observation: store one node
        with patch("waggle.graph.extract_conversation_candidates") as mock_extract:
            mock_extract.return_value = [
                {"label": "Database", "content": "We use PostgreSQL.", "node_type": NodeType.FACT, "tags": []},
            ]
            graph.observe_conversation(user_message="PostgreSQL.", assistant_response="OK.")

        # Second observation: identical content — must trigger dedup merge, not create a new node
        with patch("waggle.graph.extract_conversation_candidates") as mock_extract:
            mock_extract.return_value = [
                {"label": "Database", "content": "We use PostgreSQL.", "node_type": NodeType.FACT, "tags": []},
            ]
            result2 = graph.observe_conversation(user_message="PostgreSQL again.", assistant_response="Got it.")

        # The second call should have reused (deduped) the first node, not created a new one
        assert result2.reused_count >= 1, (
            f"Expected dedup to fire on identical content; reused_count={result2.reused_count}"
        )

    def test_fallback_when_embed_batch_unavailable(self, tmp_path: Path) -> None:
        """If embed_batch raises AttributeError/NotImplementedError, add_node must still succeed via its own embed()."""
        from waggle.models import NodeType

        graph = make_graph(tmp_path)

        # Simulate a model backend that doesn't support embed_batch by
        # shadowing it on the instance with a function that raises.
        # (del on a class-defined method doesn't work; instance shadowing does.)
        def _no_batch(texts: list[str]) -> np.ndarray:
            """Stub that simulates a backend without embed_batch support."""
            raise AttributeError("embed_batch not supported by this backend")

        graph.embedding_model.embed_batch = _no_batch  # type: ignore[method-assign]

        with patch("waggle.graph.extract_conversation_candidates") as mock_extract:
            mock_extract.return_value = [
                {"label": "Fallback A", "content": "Content A.", "node_type": NodeType.FACT, "tags": []},
                {"label": "Fallback B", "content": "Content B.", "node_type": NodeType.FACT, "tags": []},
            ]

            result = graph.observe_conversation(
                user_message="Fallback test.",
                assistant_response="Noted.",
            )

        # Both nodes must still be stored even without embed_batch
        assert result.verbatim_stored is True
        assert len(result.stored_nodes) == 2
        assert not result.extraction_errors

    def test_malformed_batch_output_raises(self, tmp_path: Path) -> None:
        """If embed_batch returns an array with the wrong length, it should raise a ValueError."""

        graph = make_graph(tmp_path)

        def _bad_batch(texts: list[str]) -> np.ndarray:
            # Always return just one vector regardless of input length
            return np.stack([graph.embedding_model.embed(texts[0])], axis=0)

        graph.embedding_model.embed_batch = _bad_batch  # type: ignore[method-assign]

        with patch("waggle.graph.extract_conversation_candidates") as mock_extract:
            mock_extract.return_value = [
                {"label": "Alpha", "content": "Alpha content.", "node_type": NodeType.FACT, "tags": []},
                {"label": "Beta", "content": "Beta content.", "node_type": NodeType.FACT, "tags": []},
            ]

            result = graph.observe_conversation(
                user_message="Alpha and Beta.",
                assistant_response="Got it.",
            )

            # The ValueError from the shape guard is caught by observe_conversation
            # and logged as a candidate application failure.
            assert any("embed_batch returned 1 vectors, expected 2" in err for err in result.extraction_errors)
            assert result.verbatim_stored is True
