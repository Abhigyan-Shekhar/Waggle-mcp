from __future__ import annotations

import math
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from waggle.errors import ValidationFailure
from waggle.retrieval.hybrid import HybridRetrievalConfig

DEFAULT_DB_PATH = "~/.waggle/waggle.db"

# Valid values for WAGGLE_STARTUP_MODE
STARTUP_MODE_FAST = "fast"  # skip ML warmup; schema/inspection only
STARTUP_MODE_NORMAL = "normal"  # background warmup (default)
STARTUP_MODE_STRICT = "strict"  # block until embeddings ready before serving


def _discover_codex_waggle_db_path(home: Path | None = None) -> str | None:
    """Reuse Codex's configured Waggle DB path when present.

    This keeps repo-launched commands like `waggle-mcp edit-graph` pointed at the
    same SQLite file the Codex MCP server is already using, instead of silently
    falling back to the historical `~/.waggle/waggle.db` default.
    """

    root = home or Path.home()
    config_path = root / ".codex" / "config.toml"
    if not config_path.exists():
        return None
    try:
        payload = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    db_path = payload.get("mcp_servers", {}).get("waggle", {}).get("env", {}).get("WAGGLE_DB_PATH")
    if not isinstance(db_path, str) or not db_path.strip():
        return None
    return str(Path(db_path).expanduser())


def resolve_default_db_path() -> str:
    configured = _discover_codex_waggle_db_path()
    if configured:
        return configured
    return DEFAULT_DB_PATH


def _parse_int(name: str, value: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise ValidationFailure(f"{name} must be an integer, got '{value}'.") from exc


def _parse_float(name: str, value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValidationFailure(f"{name} must be a float, got '{value}'.") from exc

    if not math.isfinite(parsed):
        raise ValidationFailure(f"{name} must be a finite float, got '{value}'.")

    return parsed


@dataclass(slots=True)
class AppConfig:
    backend: str
    transport: str
    model_name: str
    db_path: str
    default_tenant_id: str
    http_host: str
    http_port: int
    log_level: str
    rate_limit_rpm: int
    write_rate_limit_rpm: int
    max_concurrent_requests: int
    max_payload_bytes: int
    request_timeout_seconds: int
    export_dir: str | None
    neo4j_uri: str
    neo4j_username: str
    neo4j_password: str
    neo4j_database: str
    embedding_backend: str = "pytorch"
    retention_enabled: bool = False
    retention_days: int = 90
    retention_prune_interval_hours: int = 24
    recency_half_life_days: float = 30.0
    tiered_retrieval: bool = False
    tiered_retrieval_top_k_windows: int = 3
    hybrid_vector_weight: float = 1.0
    hybrid_bm25_weight: float = 1.0
    hybrid_graph_weight: float = 1.0
    hybrid_recency_weight: float = 1.0
    hybrid_rerank_enabled: bool = False
    hybrid_rerank_model: str = "claude-3-5-sonnet-latest"
    hybrid_rerank_top_k_in: int = 20
    hybrid_rerank_top_k_out: int = 5
    startup_mode: str = STARTUP_MODE_NORMAL  # fast | normal | strict
    api_key_environment: str = "test"  # test | local | live; controls generated API key prefix
    # Canonicalization-at-write dedup threshold.
    # Nodes with cosine similarity >= this value (and matching node_type + scope)
    # are merged at write time instead of creating a duplicate.
    # Must be >= 0.85 to avoid false-positive merges.
    dedup_threshold: float = 0.88

    @classmethod
    def from_env(cls) -> AppConfig:
        # Render (and other PaaS providers) commonly inject a dynamic `PORT` env var.
        # Prefer `WAGGLE_HTTP_PORT` when set, otherwise fall back to `PORT`.
        if os.environ.get("WAGGLE_HTTP_PORT") is not None:
            http_port_name = "WAGGLE_HTTP_PORT"
            resolved_http_port = os.environ["WAGGLE_HTTP_PORT"]
        elif os.environ.get("PORT") is not None:
            http_port_name = "PORT"
            resolved_http_port = os.environ["PORT"]
        else:
            http_port_name = "WAGGLE_HTTP_PORT"
            resolved_http_port = "8080"
        config = cls(
            backend=os.environ.get("WAGGLE_BACKEND", "sqlite").strip().lower(),
            transport=os.environ.get("WAGGLE_TRANSPORT", "stdio").strip().lower(),
            model_name=os.environ.get("WAGGLE_MODEL", "all-MiniLM-L6-v2"),
            embedding_backend=os.environ.get(
                "WAGGLE_EMBEDDING_BACKEND",
                "pytorch",
            )
            .strip()
            .lower(),
            db_path=os.environ.get("WAGGLE_DB_PATH") or resolve_default_db_path(),
            default_tenant_id=os.environ.get("WAGGLE_DEFAULT_TENANT_ID", "local-default").strip(),
            http_host=os.environ.get("WAGGLE_HTTP_HOST", "0.0.0.0"),
            http_port=_parse_int(
                http_port_name,
                resolved_http_port,
            ),
            log_level=os.environ.get("WAGGLE_LOG_LEVEL", "INFO"),
            rate_limit_rpm=_parse_int("WAGGLE_RATE_LIMIT_RPM", os.environ.get("WAGGLE_RATE_LIMIT_RPM", "120")),
            write_rate_limit_rpm=_parse_int(
                "WAGGLE_WRITE_RATE_LIMIT_RPM", os.environ.get("WAGGLE_WRITE_RATE_LIMIT_RPM", "60")
            ),
            max_concurrent_requests=_parse_int(
                "WAGGLE_MAX_CONCURRENT_REQUESTS", os.environ.get("WAGGLE_MAX_CONCURRENT_REQUESTS", "8")
            ),
            max_payload_bytes=_parse_int(
                "WAGGLE_MAX_PAYLOAD_BYTES", os.environ.get("WAGGLE_MAX_PAYLOAD_BYTES", str(1024 * 1024))
            ),
            request_timeout_seconds=_parse_int(
                "WAGGLE_REQUEST_TIMEOUT_SECONDS", os.environ.get("WAGGLE_REQUEST_TIMEOUT_SECONDS", "30")
            ),
            recency_half_life_days=_parse_float(
                "WAGGLE_RECENCY_HALF_LIFE_DAYS", os.environ.get("WAGGLE_RECENCY_HALF_LIFE_DAYS", "30.0")
            ),
            hybrid_vector_weight=_parse_float(
                "WAGGLE_HYBRID_VECTOR_WEIGHT", os.environ.get("WAGGLE_HYBRID_VECTOR_WEIGHT", "1.0")
            ),
            hybrid_bm25_weight=_parse_float(
                "WAGGLE_HYBRID_BM25_WEIGHT", os.environ.get("WAGGLE_HYBRID_BM25_WEIGHT", "1.0")
            ),
            hybrid_graph_weight=_parse_float(
                "WAGGLE_HYBRID_GRAPH_WEIGHT", os.environ.get("WAGGLE_HYBRID_GRAPH_WEIGHT", "1.0")
            ),
            hybrid_recency_weight=_parse_float(
                "WAGGLE_HYBRID_RECENCY_WEIGHT", os.environ.get("WAGGLE_HYBRID_RECENCY_WEIGHT", "1.0")
            ),
            hybrid_rerank_enabled=os.environ.get("WAGGLE_HYBRID_RERANK_ENABLED", "false").strip().lower() == "true",
            hybrid_rerank_model=os.environ.get("WAGGLE_HYBRID_RERANK_MODEL", "claude-3-5-sonnet-latest").strip(),
            hybrid_rerank_top_k_in=_parse_int(
                "WAGGLE_HYBRID_RERANK_TOP_K_IN", os.environ.get("WAGGLE_HYBRID_RERANK_TOP_K_IN", "20")
            ),
            hybrid_rerank_top_k_out=_parse_int(
                "WAGGLE_HYBRID_RERANK_TOP_K_OUT", os.environ.get("WAGGLE_HYBRID_RERANK_TOP_K_OUT", "5")
            ),
            export_dir=os.environ.get("WAGGLE_EXPORT_DIR"),
            neo4j_uri=os.environ.get("WAGGLE_NEO4J_URI", "").strip(),
            neo4j_username=os.environ.get("WAGGLE_NEO4J_USERNAME", "").strip(),
            neo4j_password=os.environ.get("WAGGLE_NEO4J_PASSWORD", ""),
            neo4j_database=os.environ.get("WAGGLE_NEO4J_DATABASE", "").strip(),
            retention_enabled=os.environ.get("WAGGLE_RETENTION_ENABLED", "false").strip().lower() == "true",
            retention_days=_parse_int("WAGGLE_RETENTION_DAYS", os.environ.get("WAGGLE_RETENTION_DAYS", "90")),
            retention_prune_interval_hours=_parse_int(
                "WAGGLE_RETENTION_PRUNE_INTERVAL_HOURS", os.environ.get("WAGGLE_RETENTION_PRUNE_INTERVAL_HOURS", "24")
            ),
            startup_mode=os.environ.get("WAGGLE_STARTUP_MODE", STARTUP_MODE_NORMAL).strip().lower(),
            api_key_environment=os.environ.get("WAGGLE_API_KEY_ENVIRONMENT", "test").strip().lower(),
            tiered_retrieval=os.environ.get("WAGGLE_TIERED_RETRIEVAL", "false").strip().lower() == "true",
            tiered_retrieval_top_k_windows=_parse_int(
                "WAGGLE_TIERED_TOP_K_WINDOWS", os.environ.get("WAGGLE_TIERED_TOP_K_WINDOWS", "3")
            ),
            dedup_threshold=_parse_float("WAGGLE_DEDUP_THRESHOLD", os.environ.get("WAGGLE_DEDUP_THRESHOLD", "0.88")),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.transport not in {"stdio", "http"}:
            raise ValidationFailure(f"Unsupported WAGGLE_TRANSPORT: {self.transport}")
        if self.backend not in {"sqlite", "neo4j"}:
            raise ValidationFailure(f"Unsupported WAGGLE_BACKEND: {self.backend}")
        if self.transport == "http" and self.backend != "neo4j":
            raise ValidationFailure("HTTP transport requires WAGGLE_BACKEND=neo4j.")
        if not self.default_tenant_id:
            raise ValidationFailure("WAGGLE_DEFAULT_TENANT_ID cannot be empty.")
        if self.backend == "sqlite":
            self.db_path = str(Path(self.db_path).expanduser())
        if self.backend == "neo4j" and (not self.neo4j_uri or not self.neo4j_username or not self.neo4j_password):
            raise ValidationFailure(
                "Neo4j backend requires WAGGLE_NEO4J_URI, WAGGLE_NEO4J_USERNAME, and WAGGLE_NEO4J_PASSWORD."
            )
        if self.startup_mode not in {STARTUP_MODE_FAST, STARTUP_MODE_NORMAL, STARTUP_MODE_STRICT}:
            raise ValidationFailure(
                f"Unsupported WAGGLE_STARTUP_MODE: {self.startup_mode!r}. Valid values: fast, normal, strict."
            )
        if self.api_key_environment not in {"test", "local", "live"}:
            raise ValidationFailure(
                f"Unsupported WAGGLE_API_KEY_ENVIRONMENT: {self.api_key_environment!r}. Valid values: test, local, live."
            )
        if self.dedup_threshold < 0.85:
            raise ValidationFailure("WAGGLE_DEDUP_THRESHOLD must be >= 0.85 to avoid false-positive merges.")
        if self.recency_half_life_days <= 0:
            raise ValidationFailure("WAGGLE_RECENCY_HALF_LIFE_DAYS must be greater than 0.")
        if self.tiered_retrieval_top_k_windows < 1:
            raise ValidationFailure("WAGGLE_TIERED_TOP_K_WINDOWS must be at least 1.")
        if self.hybrid_rerank_top_k_in < 1:
            raise ValidationFailure("WAGGLE_HYBRID_RERANK_TOP_K_IN must be at least 1.")
        if self.hybrid_rerank_top_k_out < 1:
            raise ValidationFailure("WAGGLE_HYBRID_RERANK_TOP_K_OUT must be at least 1.")
        if self.retention_days < 1:
            raise ValidationFailure("WAGGLE_RETENTION_DAYS must be at least 1.")
        if self.retention_prune_interval_hours < 1:
            raise ValidationFailure("WAGGLE_RETENTION_PRUNE_INTERVAL_HOURS must be at least 1.")
        if self.hybrid_vector_weight < 0:
            raise ValidationFailure("WAGGLE_HYBRID_VECTOR_WEIGHT must be non-negative.")
        if self.hybrid_bm25_weight < 0:
            raise ValidationFailure("WAGGLE_HYBRID_BM25_WEIGHT must be non-negative.")
        if self.hybrid_graph_weight < 0:
            raise ValidationFailure("WAGGLE_HYBRID_GRAPH_WEIGHT must be non-negative.")
        if self.hybrid_recency_weight < 0:
            raise ValidationFailure("WAGGLE_HYBRID_RECENCY_WEIGHT must be non-negative.")

        if self.embedding_backend not in {"pytorch", "onnx"}:
            raise ValidationFailure(f"Unsupported WAGGLE_EMBEDDING_BACKEND: {self.embedding_backend}")

    def hybrid_retrieval_config(self) -> HybridRetrievalConfig:
        return HybridRetrievalConfig(
            vector_weight=self.hybrid_vector_weight,
            bm25_weight=self.hybrid_bm25_weight,
            graph_weight=self.hybrid_graph_weight,
            recency_weight=self.hybrid_recency_weight,
            rerank_enabled=self.hybrid_rerank_enabled,
            rerank_model=self.hybrid_rerank_model,
            rerank_top_k_in=self.hybrid_rerank_top_k_in,
            rerank_top_k_out=self.hybrid_rerank_top_k_out,
            recency_half_life_days=self.recency_half_life_days,
        )

    @property
    def is_fast_mode(self) -> bool:
        return self.startup_mode == STARTUP_MODE_FAST

    @property
    def is_strict_mode(self) -> bool:
        return self.startup_mode == STARTUP_MODE_STRICT
