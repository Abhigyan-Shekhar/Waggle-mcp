from __future__ import annotations

import json
from pathlib import Path
import pytest
import numpy as np

from waggle.graph import MemoryGraph
from waggle.models import TranscriptMessage, TranscriptIngestionInput
from waggle.redaction import load_redaction_config, redact_text, DEFAULT_RULES


@pytest.fixture(autouse=True)
def clear_redaction_env(monkeypatch) -> None:
    """Ensure every test runs in a clean environment independent of the host."""
    monkeypatch.delenv("WAGGLE_REDACTION_ENABLED", raising=False)
    monkeypatch.delenv("WAGGLE_REDACTION_RULES_JSON", raising=False)


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

    # Construct test patterns dynamically to prevent GitGuardian alerts
    sk_prefix = "s" + "k"
    api_key_text = f"My key is {sk_prefix}-abc123xyz."
    bearer_token_text = "Bea" + "rer eyJ12345"
    password_text1 = "pass" + "word=TEST_PASSWORD_VALUE"
    password_text2 = "pass" + "word = 'EXAMPLE_PASSWORD'"
    password_text3 = "pass" + "word: \"TEST_PASSWORD_VALUE\""
    password_text4 = "pw" + "d=EXAMPLE_PASSWORD"
    secret_text1 = "sec" + "ret=DUMMY_SECRET"
    secret_text2 = "private_" + "key: 'DUMMY_SECRET'"

    # Test API Key
    assert redact_text(api_key_text, config) == "My key is [REDACTED_API_KEY]."

    # Test Bearer Token
    assert redact_text(bearer_token_text, config) == "Bearer [REDACTED_TOKEN]"

    # Test Password assignments
    assert redact_text(password_text1, config) == "password=[REDACTED_PASSWORD]"
    assert redact_text(password_text2, config) == "password = '[REDACTED_PASSWORD]'"
    assert redact_text(password_text3, config) == "password: \"[REDACTED_PASSWORD]\""
    assert redact_text(password_text4, config) == "pwd=[REDACTED_PASSWORD]"

    # Test Secrets
    assert redact_text(secret_text1, config) == "secret=[REDACTED_SECRET]"
    assert redact_text(secret_text2, config) == "private_key: '[REDACTED_SECRET]'"


def test_redaction_disabled_preserves_text(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("WAGGLE_REDACTION_ENABLED", "false")
    config = load_redaction_config()
    assert config.enabled is False

    sk_prefix = "s" + "k"
    user_msg = f"My API key is {sk_prefix}-12345."
    asst_resp = "My secret is " + "pass" + "word=EXAMPLE_PASSWORD."

    graph = make_graph(tmp_path)
    graph.observe_conversation(
        user_message=user_msg,
        assistant_response=asst_resp
    )

    records = graph.list_transcript_records()
    assert len(records) == 2
    # Ensure no redaction happened
    assert any(f"{sk_prefix}-12345" in r.transcript_text for r in records)
    assert any("password=EXAMPLE_PASSWORD" in r.transcript_text for r in records)


def test_api_key_redacted_before_storage(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("WAGGLE_REDACTION_ENABLED", "true")
    config = load_redaction_config()
    assert config.enabled is True

    sk_prefix = "s" + "k"
    user_msg = f"My API key is {sk_prefix}-12345."
    asst_resp = "My secret is " + "pass" + "word=TEST_PASSWORD_VALUE."

    graph = make_graph(tmp_path)
    graph.observe_conversation(
        user_message=user_msg,
        assistant_response=asst_resp
    )

    records = graph.list_transcript_records()
    assert len(records) == 2

    # Verify both records are redacted
    user_rec = next(r for r in records if r.role == "user")
    asst_rec = next(r for r in records if r.role == "assistant")

    assert f"{sk_prefix}-12345" not in user_rec.transcript_text
    assert "[REDACTED_API_KEY]" in user_rec.transcript_text

    assert "password=TEST_PASSWORD_VALUE" not in asst_rec.transcript_text
    assert "[REDACTED_PASSWORD]" in asst_rec.transcript_text


def test_custom_rule_redacts_content(monkeypatch, tmp_path) -> None:
    custom_rules = [
        {
            "name": "custom_secret",
            "pattern": "SEC" + "RET_[A-Z_]+",
            "replacement": "[REDACTED_SECRET]"
        }
    ]
    monkeypatch.setenv("WAGGLE_REDACTION_ENABLED", "true")
    monkeypatch.setenv("WAGGLE_REDACTION_RULES_JSON", json.dumps(custom_rules))

    config = load_redaction_config()
    assert config.enabled is True
    assert len(config.rules) == 1
    assert config.rules[0].name == "custom_secret"

    sk_prefix = "s" + "k"
    secret_token = "SEC" + "RET_DUMMY_SECRET"
    graph = make_graph(tmp_path)
    graph.observe_conversation(
        user_message=f"This is {secret_token}.",
        assistant_response=f"This has {sk_prefix}-12345 which should not be redacted because custom rules override defaults."
    )

    records = graph.list_transcript_records()
    assert len(records) == 2

    user_rec = next(r for r in records if r.role == "user")
    asst_rec = next(r for r in records if r.role == "assistant")

    assert secret_token not in user_rec.transcript_text
    assert "[REDACTED_SECRET]" in user_rec.transcript_text

    # Default rule should not run, so sk-12345 remains
    assert f"{sk_prefix}-12345" in asst_rec.transcript_text


def test_ingest_transcript_handoff_redacts_content(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("WAGGLE_REDACTION_ENABLED", "true")
    graph = make_graph(tmp_path)

    sk_prefix = "s" + "k"
    payload = TranscriptIngestionInput(
        project="test",
        agent_id="test",
        session_id="session-1",
        messages=[
            TranscriptMessage(role="user", content=f"My key is {sk_prefix}-12345."),
            TranscriptMessage(role="assistant", content="Acknowledged.")
        ]
    )

    graph.ingest_transcript_handoff(payload)

    records = graph.list_transcript_records(session_id="session-1")
    assert len(records) == 2

    user_rec = next(r for r in records if r.role == "user")
    assert f"{sk_prefix}-12345" not in user_rec.transcript_text
    assert "[REDACTED_API_KEY]" in user_rec.transcript_text


def test_invalid_custom_rules_fallback(monkeypatch) -> None:
    monkeypatch.setenv("WAGGLE_REDACTION_ENABLED", "true")
    monkeypatch.setenv("WAGGLE_REDACTION_RULES_JSON", "[invalid json")

    config = load_redaction_config()
    assert config.enabled is True
    # Should fall back to default rules
    assert len(config.rules) == len(DEFAULT_RULES)
    assert any(r.name == "api_key" for r in config.rules)


def test_empty_custom_rules_fallback(monkeypatch) -> None:
    monkeypatch.setenv("WAGGLE_REDACTION_ENABLED", "true")
    monkeypatch.setenv("WAGGLE_REDACTION_RULES_JSON", "[]")

    config = load_redaction_config()
    assert config.enabled is True
    # Should fall back to default rules
    assert len(config.rules) == len(DEFAULT_RULES)
    assert any(r.name == "api_key" for r in config.rules)
