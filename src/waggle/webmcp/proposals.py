"""Durable application workflow state for WebMCP memory proposals."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

_PROPOSAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS webmcp_memory_proposals (
    proposal_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    target_memory_id TEXT NOT NULL,
    target_memory_version TEXT NOT NULL,
    current_content TEXT NOT NULL,
    proposed_content TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    evidence_ids_json TEXT NOT NULL DEFAULT '[]',
    proposed_by_type TEXT NOT NULL,
    proposed_by_id TEXT NOT NULL DEFAULT '',
    dedupe_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'approved', 'rejected', 'applied')),
    created_at TEXT NOT NULL,
    reviewed_at TEXT DEFAULT NULL,
    reviewed_by TEXT DEFAULT '',
    approved_content TEXT DEFAULT NULL,
    applied_at TEXT DEFAULT NULL,
    result_memory_id TEXT DEFAULT NULL
);

CREATE INDEX IF NOT EXISTS idx_webmcp_proposals_project_status
ON webmcp_memory_proposals(tenant_id, project_id, status, created_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS idx_webmcp_pending_proposal_dedupe
ON webmcp_memory_proposals(tenant_id, dedupe_key)
WHERE status = 'pending';
"""


def _serialize_proposal(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "proposal_id": str(row["proposal_id"]),
        "status": str(row["status"]),
        "project_id": str(row["project_id"]),
        "target": {
            "memory_id": str(row["target_memory_id"]),
            "current_content": str(row["current_content"]),
            "version": str(row["target_memory_version"]),
        },
        "proposed_content": str(row["proposed_content"]),
        "reason": str(row["reason"]),
        "evidence_ids": list(json.loads(row["evidence_ids_json"] or "[]")),
        "proposed_by": {
            "type": str(row["proposed_by_type"]),
            "id": str(row["proposed_by_id"]),
        },
        "created_at": str(row["created_at"]),
        "reviewed_at": row["reviewed_at"],
        "reviewed_by": str(row["reviewed_by"] or ""),
        "approved_content": row["approved_content"],
        "applied_at": row["applied_at"],
        "result_memory_id": row["result_memory_id"],
    }


class ProposalRepository:
    """Small SQLite repository deliberately separate from authoritative memory."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser()
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(_PROPOSAL_SCHEMA)

    def create_or_get_pending(
        self,
        *,
        tenant_id: str,
        project_id: str,
        target_memory_id: str,
        target_memory_version: str,
        current_content: str,
        proposed_content: str,
        reason: str,
        evidence_ids: list[str],
        proposed_by_type: str,
        proposed_by_id: str,
        dedupe_key: str,
    ) -> tuple[dict[str, Any], bool]:
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                """
                SELECT * FROM webmcp_memory_proposals
                WHERE tenant_id = ? AND dedupe_key = ? AND status = 'pending'
                """,
                (tenant_id, dedupe_key),
            ).fetchone()
            if existing is not None:
                return _serialize_proposal(existing), False

            proposal_id = f"proposal_{uuid4().hex}"
            created_at = datetime.now(UTC).isoformat()
            try:
                connection.execute(
                    """
                    INSERT INTO webmcp_memory_proposals (
                        proposal_id, tenant_id, project_id, target_memory_id,
                        target_memory_version, current_content, proposed_content,
                        reason, evidence_ids_json, proposed_by_type, proposed_by_id,
                        dedupe_key, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                    """,
                    (
                        proposal_id,
                        tenant_id,
                        project_id,
                        target_memory_id,
                        target_memory_version,
                        current_content,
                        proposed_content,
                        reason,
                        json.dumps(evidence_ids, separators=(",", ":")),
                        proposed_by_type,
                        proposed_by_id,
                        dedupe_key,
                        created_at,
                    ),
                )
            except sqlite3.IntegrityError:
                existing = connection.execute(
                    """
                    SELECT * FROM webmcp_memory_proposals
                    WHERE tenant_id = ? AND dedupe_key = ? AND status = 'pending'
                    """,
                    (tenant_id, dedupe_key),
                ).fetchone()
                if existing is None:
                    raise
                return _serialize_proposal(existing), False

            created = connection.execute(
                "SELECT * FROM webmcp_memory_proposals WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
            if created is None:  # pragma: no cover - guarded by the preceding insert
                raise RuntimeError("Proposal insert did not produce a readable row.")
            return _serialize_proposal(created), True

    def list_for_project(
        self,
        *,
        tenant_id: str,
        project_id: str,
        status: str = "pending",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM webmcp_memory_proposals
                WHERE tenant_id = ? AND project_id = ? AND status = ?
                ORDER BY created_at DESC, proposal_id DESC
                LIMIT ?
                """,
                (tenant_id, project_id, status, limit),
            ).fetchall()
        return [_serialize_proposal(row) for row in rows]
