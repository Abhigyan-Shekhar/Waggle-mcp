from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from waggle.abhi import (
    build_abhi_document,
    validate_abhi_document,
)
from waggle.config import AppConfig
from waggle.embeddings import EmbeddingModel
from waggle.errors import ValidationFailure
from waggle.graph import MemoryGraph
from waggle.models import (
    ApiKeyRecord,
    AuditEventRecord,
    NodeType,
    RetentionPolicyRecord,
    RetentionPruneRunRecord,
)

LOGGER = logging.getLogger(__name__)

WRITE_HEAVY_TOOLS = {
    "store_node",
    "store_edge",
    "decompose_and_store",
    "observe_conversation",
    "clear_session",
    "clear_project",
    "clear_all",
    "pull",
    "merge",
    "grep",
    "import_graph_backup",
    "import_abhi",
    "merge_abhi",
    "load_abhi_chunks",
    "query_abhi",
    "import_markdown_vault",
}

REQUIRED_RUNTIME_METHODS = (
    "export_context_bundle",
    "export_markdown_vault",
    "export_abhi",
    "diff_abhi",
    "import_abhi",
    "merge_abhi",
    "load_abhi_chunks",
    "query_abhi",
    "validate_abhi",
    "inspect_abhi",
    "list_context_scopes",
    "get_node_history",
    "import_markdown_vault",
    "timeline",
    "list_conflicts",
    "resolve_conflict",
    "edge_quality_report",
)

_EXPORT_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("OpenAI-style API key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{20,}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("JWT token", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9._-]{8,}\.[A-Za-z0-9._-]{8,}\b")),
    ("Password assignment", re.compile(r"(?i)\b(password|passwd|pwd)\b\s*[:=]\s*['\"]?\S+")),
    (
        "Secret/token assignment",
        re.compile(r"(?i)\b(api[_ -]?key|secret[_ -]?key|access[_ -]?token)\b\s*[:=]\s*['\"]?\S+"),
    ),
)


def _resolve_passphrase(args: argparse.Namespace) -> str:
    env_name = str(getattr(args, "passphrase_env", "") or "").strip()
    if env_name:
        return os.environ.get(env_name, "").strip()
    if bool(getattr(args, "encrypt", False)):
        return getpass.getpass("ABHI passphrase: ").strip()
    return ""


def _resolve_drive_token_path(args: argparse.Namespace, config: AppConfig) -> Path:
    raw = str(getattr(args, "token_path", "") or "").strip()
    if raw:
        return Path(raw).expanduser()
    export_root = Path(config.export_dir).expanduser() if config.export_dir else Path.home() / ".waggle"
    return export_root / "google-drive-token.json"


def _serialize_api_key_record(record: ApiKeyRecord) -> dict[str, Any]:
    return {
        "api_key_id": record.api_key_id,
        "tenant_id": record.tenant_id,
        "prefix": record.prefix,
        "name": record.name,
        "status": record.status,
        "created_at": record.created_at.isoformat(),
        "expires_at": record.expires_at.isoformat() if record.expires_at else None,
        "revoked_at": record.revoked_at.isoformat() if record.revoked_at else None,
        "last_used_at": record.last_used_at.isoformat() if record.last_used_at else None,
        "created_by": record.created_by,
        "scopes": record.scopes,
    }


def _serialize_retention_policy(record: RetentionPolicyRecord) -> dict[str, Any]:
    next_due_at = None
    if record.last_pruned_at is not None:
        next_due_at = record.last_pruned_at + timedelta(hours=record.prune_interval_hours)
    return {
        "tenant_id": record.tenant_id,
        "enabled": record.enabled,
        "retention_days": record.retention_days,
        "prune_interval_hours": record.prune_interval_hours,
        "last_pruned_at": record.last_pruned_at.isoformat() if record.last_pruned_at else None,
        "next_due_at": next_due_at.isoformat() if next_due_at else None,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
    }


def _serialize_retention_run(record: RetentionPruneRunRecord) -> dict[str, Any]:
    return {
        "run_id": record.run_id,
        "tenant_id": record.tenant_id,
        "status": record.status,
        "cutoff": record.cutoff.isoformat(),
        "started_at": record.started_at.isoformat(),
        "completed_at": record.completed_at.isoformat() if record.completed_at else None,
        "deleted_nodes": record.deleted_nodes,
        "deleted_edges": record.deleted_edges,
        "deleted_transcripts": record.deleted_transcripts,
        "deleted_context_windows": record.deleted_context_windows,
        "deleted_context_window_edges": record.deleted_context_window_edges,
        "deleted_exports": record.deleted_exports,
        "duration_ms": record.duration_ms,
        "error_message": record.error_message,
    }


def _serialize_audit_event(record: AuditEventRecord) -> dict[str, Any]:
    return {
        "event_id": record.event_id,
        "tenant_id": record.tenant_id,
        "event_type": record.event_type,
        "actor_type": record.actor_type,
        "actor_id": record.actor_id,
        "api_key_id": record.api_key_id,
        "resource_type": record.resource_type,
        "resource_id": record.resource_id,
        "action": record.action,
        "status": record.status,
        "ip_address": record.ip_address,
        "user_agent": record.user_agent,
        "created_at": record.created_at.isoformat(),
        "metadata": record.metadata,
    }


def _parse_api_key_scopes(raw: str | None) -> list[str]:
    if raw is None:
        return []
    return [item.strip() for item in str(raw).split(",") if item.strip()]


def _scan_export_transcripts_for_secrets(
    backend: MemoryGraph,
    *,
    project: str = "",
    agent_id: str = "",
    session_id: str = "",
    scope: str = "all",
    since_date: str = "",
    max_findings: int = 10,
) -> list[dict[str, Any]]:
    snapshot = backend.get_graph_snapshot()
    document = build_abhi_document(
        snapshot,
        scope=scope,
        project=project,
        agent_id=agent_id,
        session_id=session_id,
        since_date=since_date,
        include_embeddings=False,
        encrypted=False,
    )
    findings: list[dict[str, Any]] = []
    for row in document.get("transcripts", []):
        text = str(row.get("transcript_text", ""))
        if not text.strip():
            continue
        for label, pattern in _EXPORT_SECRET_PATTERNS:
            match = pattern.search(text)
            if match is None:
                continue
            secret = match.group(0)
            preview = text.replace(secret, "[REDACTED]")
            findings.append(
                {
                    "pattern": label,
                    "transcript_id": str(row.get("id", "")),
                    "session_id": str(row.get("session_id", "")),
                    "turn_index": int(row.get("turn_index", 0) or 0),
                    "role": str(row.get("role", "")),
                    "preview": preview[:180],
                }
            )
            break
        if len(findings) >= max_findings:
            break
    return findings


def _assert_export_safe(
    backend: MemoryGraph,
    *,
    force: bool,
    project: str = "",
    agent_id: str = "",
    session_id: str = "",
    scope: str = "all",
    since_date: str = "",
) -> None:
    findings = _scan_export_transcripts_for_secrets(
        backend,
        project=project,
        agent_id=agent_id,
        session_id=session_id,
        scope=scope,
        since_date=since_date,
    )
    if findings and not force:
        summary = "; ".join(
            f"{item['pattern']} in {item['role']} turn {item['turn_index']} of session {item['session_id'] or 'default'}"
            for item in findings[:3]
        )
        raise ValidationFailure(
            "Export refused because transcript_records appear to contain secrets. "
            f"Run again with --force only after redacting or confirming the export scope is safe. Findings: {summary}."
        )


def _object_input_schema(
    properties: dict[str, Any] | None = None,
    *,
    required: list[str] | None = None,
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties or {},
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


def _scope_properties() -> dict[str, dict[str, Any]]:
    return {
        "agent_id": {
            "type": "string",
            "default": "",
            "description": "Optional agent or client identifier used to partition memory.",
        },
        "project": {
            "type": "string",
            "default": "",
            "description": "Optional project or workspace name used to partition memory.",
        },
        "session_id": {
            "type": "string",
            "default": "",
            "description": "Optional conversation or run identifier used to partition memory.",
        },
    }


def _assert_runtime_feature_parity() -> None:
    missing = [name for name in REQUIRED_RUNTIME_METHODS if not hasattr(MemoryGraph, name)]
    if not missing:
        return
    joined = ", ".join(missing)
    raise RuntimeError(
        "Detected a stale waggle runtime on the import path. Missing methods: "
        f"{joined}. This usually means an older copied package in site-packages is "
        "shadowing the current source tree or editable install. Recreate the virtualenv "
        "or uninstall old waggle/graph-memory-mcp builds before running waggle-mcp."
    )


def _build_backend(config: AppConfig) -> Any:
    embedding_model = EmbeddingModel(
        config.model_name,
        embedding_backend=config.embedding_backend,
    )
    if config.is_fast_mode:
        embedding_model.disable_warmup()
    if config.backend == "sqlite":
        return MemoryGraph(
            config.db_path,
            embedding_model,
            tenant_id=config.default_tenant_id,
            dedup_similarity_threshold=config.dedup_threshold,
            recency_half_life_days=config.recency_half_life_days,
            tiered_retrieval=config.tiered_retrieval,
            tiered_retrieval_top_k_windows=config.tiered_retrieval_top_k_windows,
            hybrid_retrieval_config=config.hybrid_retrieval_config(),
            export_dir=config.export_dir,
            api_key_environment=config.api_key_environment,
        )
    from waggle.neo4j_graph import Neo4jMemoryGraph

    return Neo4jMemoryGraph(
        uri=config.neo4j_uri,
        username=config.neo4j_username,
        password=config.neo4j_password,
        database=config.neo4j_database or None,
        embedding_model=embedding_model,
        tenant_id=config.default_tenant_id,
        export_dir=config.export_dir,
        api_key_environment=config.api_key_environment,
    )


def _default_graph(config: AppConfig | None = None) -> Any:
    try:
        return _build_backend(config or AppConfig.from_env())
    except ValidationFailure as exc:
        raise RuntimeError(str(exc)) from exc


def _emit_cli_error(code: str, message: str, details: dict[str, Any]) -> None:
    payload = {"code": code, "message": message, "details": details}
    sys.stderr.write(json.dumps(payload) + "\n")
    sys.stderr.flush()


_GREEN = "\033[92m"
_RED = "\033[91m"
_CYAN = "\033[96m"
_BOLD = "\033[1m"
_RESET = "\033[0m"
_NO_COLOR = not sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return text if _NO_COLOR else f"{code}{text}{_RESET}"


def _ok(msg: str) -> None:
    print(f"  {_c(_GREEN, chr(0x2705))} {msg}")


def _fail(msg: str) -> None:
    print(f"  {_c(_RED, chr(0x274C))} {msg}")


def _python_exe() -> str:
    return sys.executable


def _default_stdio_command() -> str:
    return "waggle-mcp"
