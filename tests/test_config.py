from __future__ import annotations

from pathlib import Path

import pytest

from waggle.config import (
    DEFAULT_DB_PATH,
    AppConfig,
    _discover_codex_waggle_db_path,
    resolve_default_db_path,
)
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


def test_discover_db_path_missing_config(tmp_path):
    assert _discover_codex_waggle_db_path(home=tmp_path) is None


def test_discover_db_path_invalid_toml(tmp_path):
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    config_file = codex_dir / "config.toml"
    config_file.write_text("invalid = { toml = [", encoding="utf-8")
    assert _discover_codex_waggle_db_path(home=tmp_path) is None


def test_discover_db_path_blank_value(tmp_path):
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    config_file = codex_dir / "config.toml"
    config_file.write_text('[mcp_servers.waggle.env]\nWAGGLE_DB_PATH = ""', encoding="utf-8")
    assert _discover_codex_waggle_db_path(home=tmp_path) is None


def test_discover_db_path_missing_env_key(tmp_path):
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    config_file = codex_dir / "config.toml"
    config_file.write_text("[mcp_servers.waggle]\nenv = {}", encoding="utf-8")
    assert _discover_codex_waggle_db_path(home=tmp_path) is None


def test_discover_db_path_valid_path(tmp_path):
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    config_file = codex_dir / "config.toml"
    target_path = tmp_path / "custom" / "waggle.db"
    config_content = f'[mcp_servers.waggle.env]\nWAGGLE_DB_PATH = "{target_path.as_posix()}"'
    config_file.write_text(config_content, encoding="utf-8")
    result = _discover_codex_waggle_db_path(home=tmp_path)
    assert result == str(target_path)


def test_resolve_default_db_path_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert resolve_default_db_path() == DEFAULT_DB_PATH


def test_resolve_default_db_path_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    config_file = codex_dir / "config.toml"
    target_path = tmp_path / "custom" / "waggle.db"
    config_content = f'[mcp_servers.waggle.env]\nWAGGLE_DB_PATH = "{target_path.as_posix()}"'
    config_file.write_text(config_content, encoding="utf-8")
    assert resolve_default_db_path() == str(target_path)
