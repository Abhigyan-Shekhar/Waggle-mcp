from __future__ import annotations

import json
from pathlib import Path
import pytest
import numpy as np

from waggle.graph import MemoryGraph
from waggle.models import TranscriptMessage, TranscriptIngestionInput
from waggle.redaction import load_redaction_config, redact_text, DEFAULT_RULES


class FakeEmbeddingModel:
    model_name = "fake-model"
    model_id = "fake-model:deterministic-v1"

    def embed(self, text: str) -> np.ndarray:
        vector = np.zeros(8, dtype=np.float32)
        for token in text.lower().split():
            index = sum(ord(character) for character in token) % len(vector)
            vector[index] += 1.0
        norm = np.linalg.norm(vector)
        if norm == 0.0:
            return vector
        return vector / norm

    def to_bytes(self, embedding: np.ndarray) -> bytes:
        return embedding.astype(np.float32).tobytes()

    def from_bytes(self, data: bytes) -> np.ndarray:
        return np.frombuffer(data, dtype=np.float32)

    def cosine_similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        a_norm = np.linalg.norm(a)
        b_norm = np.linalg.norm(b)
        if a_norm == 0.0 or b_norm == 0.0:
            return 0.0
        return float(np.dot(a, b) / (a_norm * b_norm))


def make_graph(tmp_path: Path) -> MemoryGraph:
    return MemoryGraph(tmp_path / "memory.db", FakeEmbeddingModel())


def test_redact_text_defaults() -> None:
    # Build a config that is enabled with default rules
    config = load_redaction_config()
    config.enabled = True
    config.rules = list(DEFAULT_RULES)

    # Test API Key
    assert redact_text("My key is sk-abc123xyz.", config) == "My key is [REDACTED_API_KEY]."

    # Test Bearer Token
    assert redact_text("Bearer eyJ12345", config) == "Bearer [REDACTED_TOKEN]"

    # Test Password assignments
    assert redact_text("password=mysecret", config) == "password=[REDACTED_PASSWORD]"
    assert redact_text("password = 'mysecret'", config) == "password = '[REDACTED_PASSWORD]'"
    assert redact_text("password: \"mysecret\"", config) == "password: \"[REDACTED_PASSWORD]\""
    assert redact_text("pwd=secret", config) == "pwd=[REDACTED_PASSWORD]"

    # Test Secrets
    assert redact_text("secret=mysecret", config) == "secret=[REDACTED_SECRET]"
    assert redact_text("private_key: 'mykey'", config) == "private_key: '[REDACTED_SECRET]'"


def test_redaction_disabled_preserves_text(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("WAGGLE_REDACTION_ENABLED", "false")
    config = load_redaction_config()
    assert config.enabled is False

    graph = make_graph(tmp_path)
    graph.observe_conversation(
        user_message="My API key is sk-12345.",
        assistant_response="My secret is password=123."
    )

    records = graph.list_transcript_records()
    assert len(records) == 2
    # Ensure no redaction happened
    assert any("sk-12345" in r.transcript_text for r in records)
    assert any("password=123" in r.transcript_text for r in records)


def test_api_key_redacted_before_storage(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("WAGGLE_REDACTION_ENABLED", "true")
    config = load_redaction_config()
    assert config.enabled is True

    graph = make_graph(tmp_path)
    graph.observe_conversation(
        user_message="My API key is sk-12345.",
        assistant_response="My secret is password=123."
    )

    records = graph.list_transcript_records()
    assert len(records) == 2

    # Verify both records are redacted
    user_rec = next(r for r in records if r.role == "user")
    asst_rec = next(r for r in records if r.role == "assistant")

    assert "sk-12345" not in user_rec.transcript_text
    assert "[REDACTED_API_KEY]" in user_rec.transcript_text

    assert "password=123" not in asst_rec.transcript_text
    assert "[REDACTED_PASSWORD]" in asst_rec.transcript_text


def test_custom_rule_redacts_content(monkeypatch, tmp_path) -> None:
    custom_rules = [
        {
            "name": "custom_secret",
            "pattern": "SECRET_[A-Z0-9]+",
            "replacement": "[REDACTED_SECRET]"
        }
    ]
    monkeypatch.setenv("WAGGLE_REDACTION_ENABLED", "true")
    monkeypatch.setenv("WAGGLE_REDACTION_RULES_JSON", json.dumps(custom_rules))

    config = load_redaction_config()
    assert config.enabled is True
    assert len(config.rules) == 1
    assert config.rules[0].name == "custom_secret"

    graph = make_graph(tmp_path)
    graph.observe_conversation(
        user_message="This is SECRET_12345.",
        assistant_response="This has sk-12345 which should not be redacted because custom rules override defaults."
    )

    records = graph.list_transcript_records()
    assert len(records) == 2

    user_rec = next(r for r in records if r.role == "user")
    asst_rec = next(r for r in records if r.role == "assistant")

    assert "SECRET_12345" not in user_rec.transcript_text
    assert "[REDACTED_SECRET]" in user_rec.transcript_text

    # Default rule should not run, so sk-12345 remains
    assert "sk-12345" in asst_rec.transcript_text


def test_ingest_transcript_handoff_redacts_content(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("WAGGLE_REDACTION_ENABLED", "true")
    graph = make_graph(tmp_path)

    payload = TranscriptIngestionInput(
        project="test",
        agent_id="test",
        session_id="session-1",
        messages=[
            TranscriptMessage(role="user", content="My key is sk-12345."),
            TranscriptMessage(role="assistant", content="Acknowledged.")
        ]
    )

    graph.ingest_transcript_handoff(payload)

    records = graph.list_transcript_records(session_id="session-1")
    assert len(records) == 2

    user_rec = next(r for r in records if r.role == "user")
    assert "sk-12345" not in user_rec.transcript_text
    assert "[REDACTED_API_KEY]" in user_rec.transcript_text
