from __future__ import annotations

import pytest

from waggle.config import AppConfig
from waggle.errors import ValidationFailure


def test_embedding_backend_defaults_to_pytorch(monkeypatch):
    monkeypatch.delenv("WAGGLE_EMBEDDING_BACKEND", raising=False)

    config = AppConfig.from_env()

    assert config.embedding_backend == "pytorch"


def test_embedding_backend_accepts_onnx(monkeypatch):
    monkeypatch.setenv("WAGGLE_EMBEDDING_BACKEND", "onnx")

    config = AppConfig.from_env()

    assert config.embedding_backend == "onnx"


def test_embedding_backend_rejects_invalid_value(monkeypatch):
    monkeypatch.setenv("WAGGLE_EMBEDDING_BACKEND", "banana")

    with pytest.raises(ValidationFailure):
        AppConfig.from_env()


@pytest.mark.parametrize(
    ("field_name", "error_message"),
    [
        ("hybrid_vector_weight", "WAGGLE_HYBRID_VECTOR_WEIGHT"),
        ("hybrid_bm25_weight", "WAGGLE_HYBRID_BM25_WEIGHT"),
        ("hybrid_graph_weight", "WAGGLE_HYBRID_GRAPH_WEIGHT"),
        ("hybrid_recency_weight", "WAGGLE_HYBRID_RECENCY_WEIGHT"),
    ],
)
def test_negative_hybrid_weights_raise_validation_failure(
    field_name: str,
    error_message: str,
) -> None:
    config = AppConfig(
        backend="sqlite",
        transport="stdio",
        model_name="test",
        db_path="test.db",
        default_tenant_id="local-default",
        http_host="127.0.0.1",
        http_port=8080,
        log_level="INFO",
        rate_limit_rpm=120,
        write_rate_limit_rpm=60,
        max_concurrent_requests=8,
        max_payload_bytes=1024,
        request_timeout_seconds=30,
        export_dir=None,
        neo4j_uri="",
        neo4j_username="",
        neo4j_password="",
        neo4j_database="",
    )
    setattr(config, field_name, -1.0)

    with pytest.raises(ValidationFailure, match=error_message):
        config.validate()


def test_zero_hybrid_weights_are_allowed() -> None:
    config = AppConfig(
        backend="sqlite",
        transport="stdio",
        model_name="test",
        db_path="test.db",
        default_tenant_id="local-default",
        http_host="127.0.0.1",
        http_port=8080,
        log_level="INFO",
        rate_limit_rpm=120,
        write_rate_limit_rpm=60,
        max_concurrent_requests=8,
        max_payload_bytes=1024,
        request_timeout_seconds=30,
        export_dir=None,
        neo4j_uri="",
        neo4j_username="",
        neo4j_password="",
        neo4j_database="",
        hybrid_vector_weight=0.0,
        hybrid_bm25_weight=0.0,
        hybrid_graph_weight=0.0,
        hybrid_recency_weight=0.0,
    )

    config.validate()
