from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from typing import Any

import numpy as np

from waggle.errors import ValidationFailure
from waggle.evidence import merge_evidence_records, merge_validity_windows
from waggle.intelligence import (
    canonical_concept_overlap,
    compatible_node_types,
    contains_conflicting_months,
    contains_conflicting_numbers,
    content_token_jaccard,
    describes_rejected_or_limited_option,
    detect_conflict_reason,
    extract_choice_entity,
    is_acronym_match,
    label_similarity,
    normalize_text,
    paraphrase_dedup_score,
    type_aware_dedup_threshold,
)
from waggle.intelligence import (
    extract_conversation_candidates as extract_conversation_candidates,
)
from waggle.models import (
    CanonicalizeResult,
    ClearScopeResult,
    ConflictEntry,
    ConflictListResult,
    ConflictRecord,
    DedupCandidatePair,
    DedupCandidatesResult,
    Edge,
    EvidenceRecord,
    Node,
    NodeStoreResult,
    NodeType,
    RelationType,
    normalize_relationship,
    utc_now,
)

from .base import (
    MemoryGraphBase,
    _encode_evidence_records,
    _encode_metadata,
    _merge_scope_value,
    _parse_datetime,
    _scope_matches,
)
from .base import (
    recency_weight as recency_weight,
)
from .base import (
    score_node as score_node,
)

LOGGER = logging.getLogger(__name__)


class MutationMixin(MemoryGraphBase):
    """Mixin class for MemoryGraph handling node/edge mutation, conflict resolution, and deduplication."""

    def add_node(
        self,
        *,
        node_id: str | None = None,
        label: str,
        content: str,
        node_type: NodeType,
        tags: list[str] | None = None,
        source_prompt: str = "",
        source_turn_pair_id: str = "",
        agent_id: str = "",
        project: str = "",
        session_id: str = "",
        evidence_records: list[EvidenceRecord] | None = None,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
        context_window_id: str | None = None,
        embedding: np.ndarray | None = None,
        metadata: dict[str, Any] | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> NodeStoreResult:
        resolved_context_window_id = context_window_id
        if resolved_context_window_id is None:
            _, resolved_context_window_id = self.resolve_window_context(
                project=project, session_id=session_id, connection=connection
            )
        node_kwargs: dict[str, Any] = {}
        if node_id is not None and str(node_id).strip():
            node_kwargs["id"] = str(node_id).strip()
        embedding_vector, embedding_model_id, embedding_dim = (
            self._embed_with_metadata(content)
            if embedding is None
            else (embedding, self._current_embedding_model_id(), int(embedding.shape[0]))
        )
        if embedding_dim <= 0:
            raise ValueError("Node writes require embedding_dim metadata.")
        node = Node(
            **node_kwargs,
            tenant_id=self.tenant_id,
            agent_id=agent_id,
            project=project,
            session_id=session_id,
            context_window_id=resolved_context_window_id,
            label=label,
            content=content,
            node_type=node_type,
            tags=tags or [],
            source_prompt=source_prompt,
            embedding_model_id=embedding_model_id,
            embedding_dim=embedding_dim,
            source_turn_pair_id=source_turn_pair_id,
            metadata=metadata or {},
            evidence_records=evidence_records or [],
            valid_from=valid_from,
            valid_to=valid_to,
        )

        def _insert(active_connection: sqlite3.Connection) -> NodeStoreResult:
            if self.enable_dedup:
                duplicate = self._find_duplicate_node(active_connection, node=node, embedding=embedding_vector)
                if duplicate is not None:
                    existing_node, dedup_reason, similarity = duplicate
                    merged_node = self._merge_duplicate_node(
                        active_connection,
                        existing_node=existing_node,
                        incoming_node=node,
                    )
                    if merged_node.context_window_id:
                        active_connection.execute(
                            """
                            UPDATE nodes
                            SET context_window_id = COALESCE(context_window_id, ?)
                            WHERE tenant_id = ? AND id = ?
                            """,
                            (merged_node.context_window_id, self.tenant_id, merged_node.id),
                        )
                        self._mark_window_embedding_stale(active_connection, merged_node.context_window_id)
                    self.emit_audit_event(
                        event_type="graph.node.updated",
                        resource_type="node",
                        resource_id=merged_node.id,
                        action="update",
                        metadata={
                            "reason": "dedup_merge",
                            "dedup_reason": dedup_reason,
                            "similarity": similarity,
                        },
                        connection=active_connection,
                    )
                    return NodeStoreResult(
                        node=merged_node,
                        created=False,
                        dedup_reason=dedup_reason,
                        similarity=similarity,
                    )

            active_connection.execute(
                """
                INSERT INTO nodes (
                    id, tenant_id, agent_id, project, session_id, context_window_id,
                    label, content, node_type, tags, aliases, metadata, embedding, embedding_model_id, embedding_dim,
                    source_prompt, source_turn_pair_id, evidence_records, valid_from, valid_to,
                    created_at, updated_at, access_count
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    node.id,
                    node.tenant_id,
                    node.agent_id,
                    node.project,
                    node.session_id,
                    node.context_window_id,
                    node.label,
                    node.content,
                    node.node_type.value,
                    json.dumps(node.tags),
                    json.dumps(node.aliases),
                    _encode_metadata(node.metadata),
                    self._encode_embedding(embedding_vector),
                    node.embedding_model_id,
                    node.embedding_dim,
                    node.source_prompt,
                    node.source_turn_pair_id,
                    _encode_evidence_records(node.evidence_records),
                    node.valid_from.isoformat() if node.valid_from is not None else None,
                    node.valid_to.isoformat() if node.valid_to is not None else None,
                    node.created_at.isoformat(),
                    node.updated_at.isoformat(),
                    node.access_count,
                ),
            )
            self._mark_window_embedding_stale(active_connection, resolved_context_window_id)
            self._update_window_node_count(active_connection, resolved_context_window_id)
            conflicts = self._register_conflicts(active_connection, node) if self.enable_dedup else []
            self.emit_audit_event(
                event_type="graph.node.created",
                resource_type="node",
                resource_id=node.id,
                action="create",
                metadata={
                    "node_type": node.node_type.value,
                    "project": node.project,
                    "session_id": node.session_id,
                },
                connection=active_connection,
            )
            return NodeStoreResult(node=node, created=True, conflicts=conflicts)

        if connection is not None:
            return _insert(connection)
        with self._lock, self._pool.checkout() as managed_connection:
            return _insert(managed_connection)

    def add_edge(
        self,
        *,
        edge_id: str | None = None,
        source_id: str,
        target_id: str,
        relationship: str | RelationType,
        weight: float = 1.0,
        metadata: dict[str, Any] | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> Edge:
        edge_kwargs: dict[str, Any] = {}
        if edge_id is not None and str(edge_id).strip():
            edge_kwargs["id"] = str(edge_id).strip()
        edge = Edge(
            **edge_kwargs,
            tenant_id=self.tenant_id,
            source_id=source_id,
            target_id=target_id,
            relationship=relationship,
            weight=weight,
            metadata=metadata or {},
        )

        def _insert(active_connection: sqlite3.Connection) -> Edge:
            self._require_node(active_connection, edge.source_id)
            self._require_node(active_connection, edge.target_id)
            source_row = self._fetch_node_row(active_connection, edge.source_id)
            target_row = self._fetch_node_row(active_connection, edge.target_id)
            if source_row is None or target_row is None:
                raise ValueError("Edge endpoint missing during insert.")
            source_node = self._row_to_node(source_row)
            target_node = self._row_to_node(target_row)
            existing_edge = self._find_existing_edge(
                active_connection,
                source_id=edge.source_id,
                target_id=edge.target_id,
                relationship=edge.relationship,
            )
            if existing_edge is not None:
                return existing_edge
            active_connection.execute(
                """
                INSERT INTO edges (
                    id, tenant_id, source_id, target_id, relationship, weight, metadata, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    edge.id,
                    edge.tenant_id,
                    edge.source_id,
                    edge.target_id,
                    edge.relationship,
                    edge.weight,
                    json.dumps(edge.metadata),
                    edge.created_at.isoformat(),
                ),
            )
            if edge.relationship in {RelationType.UPDATES.value, RelationType.CONTRADICTS.value}:
                self._mark_node_superseded(
                    active_connection, old_node=target_node, new_node=source_node, relationship=edge.relationship
                )
            return edge

        if connection is not None:
            return _insert(connection)
        with self._lock, self._pool.checkout() as managed_connection:
            return _insert(managed_connection)

    def list_conflicts(
        self,
        *,
        include_resolved: bool = False,
        limit: int = 25,
    ) -> ConflictListResult:
        if limit < 1:
            raise ValueError("limit must be at least 1.")

        with self._lock, self._pool.checkout() as connection:
            edge_rows = connection.execute(
                """
                SELECT id, source_id, target_id, relationship, weight, metadata, created_at, tenant_id
                FROM edges
                WHERE tenant_id = ?
                  AND relationship IN (?, ?)
                ORDER BY created_at DESC
                """,
                (self.tenant_id, RelationType.CONTRADICTS.value, RelationType.UPDATES.value),
            ).fetchall()
            edges = [self._row_to_edge(row) for row in edge_rows]
            entries = self._build_conflict_entries(
                connection,
                edges=edges,
                include_resolved=include_resolved,
                limit=limit,
            )
        return ConflictListResult(conflicts=entries, include_resolved=include_resolved)

    def resolve_conflict(
        self,
        *,
        edge_id: str,
        resolution_note: str = "",
        winner: str | None = None,
    ) -> ConflictEntry:
        with self._lock, self._pool.checkout() as connection:
            row = connection.execute(
                """
                SELECT id, source_id, target_id, relationship, weight, metadata, created_at, tenant_id
                FROM edges
                WHERE tenant_id = ? AND id = ?
                LIMIT 1
                """,
                (self.tenant_id, edge_id),
            ).fetchone()
            if row is None:
                raise ValueError(f"Conflict edge not found: {edge_id}")
            edge = self._row_to_edge(row)
            if edge.relationship not in {RelationType.CONTRADICTS.value, RelationType.UPDATES.value}:
                raise ValueError("Only contradicts or updates edges can be resolved.")

            # Validate winner if provided
            if winner is not None:
                if winner not in {edge.source_id, edge.target_id}:
                    raise ValueError(
                        f"winner '{winner}' is not an endpoint of edge '{edge_id}'. "
                        f"Must be one of: '{edge.source_id}' (source) or '{edge.target_id}' (target)."
                    )

            metadata = dict(edge.metadata)
            metadata["resolved"] = True
            metadata["resolved_at"] = utc_now().isoformat()
            if resolution_note.strip():
                metadata["resolution_note"] = resolution_note.strip()
            if winner is not None:
                metadata["winner"] = winner

            connection.execute(
                """
                UPDATE edges
                SET metadata = ?
                WHERE tenant_id = ? AND id = ?
                """,
                (json.dumps(metadata, sort_keys=True), self.tenant_id, edge_id),
            )

            # Determine winning and losing nodes
            if winner is not None:
                losing_id = edge.target_id if winner == edge.source_id else edge.source_id
                winning_id = winner
                losing_node = self.get_node(losing_id)
                winning_node = self.get_node(winning_id)
                now = utc_now()
                LOGGER.info(
                    "resolve_conflict: superseding node %s (loser) in favour of %s (winner) via edge %s (%s) at %s",
                    losing_id,
                    winning_id,
                    edge_id,
                    edge.relationship,
                    now.isoformat(),
                )
                # Set valid_to on the losing node
                connection.execute(
                    "UPDATE nodes SET valid_to = ?, updated_at = ? WHERE id = ? AND tenant_id = ?",
                    (now.isoformat(), now.isoformat(), losing_id, self.tenant_id),
                )
                self._mark_node_superseded(
                    connection,
                    old_node=losing_node,
                    new_node=winning_node,
                    relationship=edge.relationship,
                )
            else:
                self._mark_node_superseded(
                    connection,
                    old_node=self.get_node(edge.target_id),
                    new_node=self.get_node(edge.source_id),
                    relationship=edge.relationship,
                )

            updated_edge = Edge(
                id=edge.id,
                tenant_id=edge.tenant_id,
                source_id=edge.source_id,
                target_id=edge.target_id,
                relationship=edge.relationship,
                weight=edge.weight,
                metadata=metadata,
                created_at=edge.created_at,
            )
            entry = self._build_conflict_entries(
                connection,
                edges=[updated_edge],
                include_resolved=True,
                limit=1,
            )
        if not entry:
            raise ValueError(f"Resolved conflict could not be loaded: {edge_id}")
        return entry[0]

    def update_node(
        self,
        *,
        node_id: str,
        content: str | None = None,
        label: str | None = None,
        tags: list[str] | None = None,
        agent_id: str | None = None,
        project: str | None = None,
        session_id: str | None = None,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
        evidence_records: list[EvidenceRecord] | None = None,
    ) -> Node:
        if (
            content is None
            and label is None
            and tags is None
            and agent_id is None
            and project is None
            and session_id is None
            and valid_from is None
            and valid_to is None
            and evidence_records is None
        ):
            raise ValueError("At least one field must be provided for update.")

        with self._lock, self._pool.checkout() as connection:
            row = self._fetch_node_row(connection, node_id)
            if row is None:
                raise ValueError(f"Node not found: {node_id}")

            node = self._row_to_node(row)
            updated_label = label if label is not None else node.label
            updated_content = content if content is not None else node.content
            updated_tags = tags if tags is not None else node.tags
            updated_at = utc_now()
            embedding_bytes = row["embedding"]
            embedding_model_id = node.embedding_model_id
            embedding_dim = node.embedding_dim
            if content is not None:
                embedding_vector, embedding_model_id, embedding_dim = self._embed_with_metadata(updated_content)
                embedding_bytes = self._encode_embedding(embedding_vector)

            scope_changed = (
                (project is not None and project != node.project)
                or (session_id is not None and session_id != node.session_id)
                or (agent_id is not None and agent_id != node.agent_id)
            )

            resolved_context_window_id = node.context_window_id
            if scope_changed:
                _, resolved_context_window_id = self.resolve_window_context(
                    connection,
                    agent_id=agent_id if agent_id is not None else node.agent_id,
                    project=project if project is not None else node.project,
                    session_id=session_id if session_id is not None else node.session_id,
                )

            updated_node = Node(
                id=node.id,
                tenant_id=node.tenant_id,
                agent_id=agent_id if agent_id is not None else node.agent_id,
                project=project if project is not None else node.project,
                session_id=session_id if session_id is not None else node.session_id,
                context_window_id=resolved_context_window_id,
                label=updated_label,
                content=updated_content,
                node_type=node.node_type,
                tags=updated_tags,
                aliases=node.aliases,
                source_prompt=node.source_prompt,
                embedding_model_id=embedding_model_id,
                embedding_dim=embedding_dim,
                source_turn_pair_id=node.source_turn_pair_id,
                metadata=node.metadata,
                evidence_records=evidence_records if evidence_records is not None else node.evidence_records,
                valid_from=valid_from if valid_from is not None else node.valid_from,
                valid_to=valid_to if valid_to is not None else node.valid_to,
                created_at=node.created_at,
                updated_at=updated_at,
                access_count=node.access_count,
            )

            connection.execute(
                """
                UPDATE nodes
                SET label = ?, content = ?, tags = ?, metadata = ?, embedding = ?, embedding_model_id = ?, embedding_dim = ?, updated_at = ?,
                    agent_id = ?, project = ?, session_id = ?,
                    evidence_records = ?, valid_from = ?, valid_to = ?,
                    context_window_id = ?, aliases = ?
                WHERE id = ? AND tenant_id = ?
                """,
                (
                    updated_node.label,
                    updated_node.content,
                    json.dumps(updated_node.tags),
                    _encode_metadata(updated_node.metadata),
                    embedding_bytes,
                    updated_node.embedding_model_id,
                    updated_node.embedding_dim,
                    updated_node.updated_at.isoformat(),
                    updated_node.agent_id,
                    updated_node.project,
                    updated_node.session_id,
                    _encode_evidence_records(updated_node.evidence_records),
                    updated_node.valid_from.isoformat() if updated_node.valid_from is not None else None,
                    updated_node.valid_to.isoformat() if updated_node.valid_to is not None else None,
                    updated_node.context_window_id,
                    json.dumps(updated_node.aliases),
                    updated_node.id,
                    self.tenant_id,
                ),
            )

            if scope_changed:
                if node.context_window_id:
                    self._mark_window_embedding_stale(connection, node.context_window_id)
                    self._update_window_node_count(connection, node.context_window_id)
                if resolved_context_window_id:
                    self._mark_window_embedding_stale(connection, resolved_context_window_id)
                    self._update_window_node_count(connection, resolved_context_window_id)
            elif content is not None and resolved_context_window_id:
                self._mark_window_embedding_stale(connection, resolved_context_window_id)

            self.emit_audit_event(
                event_type="graph.node.updated",
                resource_type="node",
                resource_id=updated_node.id,
                action="update",
                metadata={"project": updated_node.project, "session_id": updated_node.session_id},
                connection=connection,
            )
            return updated_node

    def update_edge(
        self,
        *,
        edge_id: str,
        source_id: str | None = None,
        target_id: str | None = None,
        relationship: str | RelationType | None = None,
        weight: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Edge:
        if source_id is None and target_id is None and relationship is None and weight is None and metadata is None:
            raise ValueError("At least one field must be provided for edge update.")

        with self._lock, self._pool.checkout() as connection:
            row = self._fetch_edge_row(connection, edge_id)
            if row is None:
                raise ValueError(f"Edge not found: {edge_id}")
            edge = self._row_to_edge(row)
            updated_edge = Edge(
                id=edge.id,
                tenant_id=edge.tenant_id,
                source_id=source_id if source_id is not None else edge.source_id,
                target_id=target_id if target_id is not None else edge.target_id,
                relationship=relationship if relationship is not None else edge.relationship,
                weight=weight if weight is not None else edge.weight,
                metadata=metadata if metadata is not None else edge.metadata,
                created_at=edge.created_at,
            )
            self._require_node(connection, updated_edge.source_id)
            self._require_node(connection, updated_edge.target_id)
            connection.execute(
                """
                UPDATE edges
                SET source_id = ?, target_id = ?, relationship = ?, weight = ?, metadata = ?
                WHERE id = ? AND tenant_id = ?
                """,
                (
                    updated_edge.source_id,
                    updated_edge.target_id,
                    updated_edge.relationship,
                    updated_edge.weight,
                    _encode_metadata(updated_edge.metadata),
                    edge_id,
                    self.tenant_id,
                ),
            )
            if updated_edge.relationship in {RelationType.UPDATES.value, RelationType.CONTRADICTS.value}:
                source_row = self._fetch_node_row(connection, updated_edge.source_id)
                target_row = self._fetch_node_row(connection, updated_edge.target_id)
                if source_row is None or target_row is None:
                    raise ValueError("Edge endpoint missing during update.")
                source_node = self._row_to_node(source_row)
                target_node = self._row_to_node(target_row)
                self._mark_node_superseded(
                    connection, old_node=target_node, new_node=source_node, relationship=updated_edge.relationship
                )
            self.emit_audit_event(
                event_type="graph.relationship.updated",
                resource_type="edge",
                resource_id=updated_edge.id,
                action="update",
                metadata={"relationship": updated_edge.relationship},
                connection=connection,
            )
            return updated_edge

    def delete_edge(self, *, edge_id: str) -> Edge:
        with self._lock, self._pool.checkout() as connection:
            row = self._fetch_edge_row(connection, edge_id)
            if row is None:
                raise ValueError(f"Edge not found: {edge_id}")
            edge = self._row_to_edge(row)
            connection.execute("DELETE FROM edges WHERE id = ? AND tenant_id = ?", (edge_id, self.tenant_id))
            self.emit_audit_event(
                event_type="graph.relationship.deleted",
                resource_type="edge",
                resource_id=edge.id,
                action="delete",
                metadata={"relationship": edge.relationship},
                connection=connection,
            )
            return edge

    def delete_node(self, *, node_id: str) -> Node:
        with self._lock, self._pool.checkout() as connection:
            row = self._fetch_node_row(connection, node_id)
            if row is None:
                raise ValueError(f"Node not found: {node_id}")
            node = self._row_to_node(row)
            connection.execute(
                "DELETE FROM edges WHERE (source_id = ? OR target_id = ?) AND tenant_id = ?",
                (node_id, node_id, self.tenant_id),
            )
            connection.execute("DELETE FROM nodes WHERE id = ? AND tenant_id = ?", (node_id, self.tenant_id))
            if node.context_window_id:
                self._mark_window_embedding_stale(connection, node.context_window_id)
                self._update_window_node_count(connection, node.context_window_id)
            self.emit_audit_event(
                event_type="graph.node.deleted",
                resource_type="node",
                resource_id=node.id,
                action="delete",
                metadata={"node_type": node.node_type.value, "project": node.project, "session_id": node.session_id},
                connection=connection,
            )
            return node

    def clear_session(self, *, session_id: str, dry_run: bool = False) -> ClearScopeResult:
        normalized_session = session_id.strip()
        if not normalized_session:
            raise ValueError("session_id is required.")
        with self._lock, self._pool.checkout() as connection:
            result = self._clear_scope_rows(connection, scope="session", session_id=normalized_session, dry_run=dry_run)
            if not dry_run:
                self.emit_audit_event(
                    event_type="graph.scope_cleared",
                    resource_type="session",
                    resource_id=normalized_session,
                    action="delete",
                    metadata=result.model_dump(mode="json"),
                    connection=connection,
                )
            return result

    def clear_project(self, *, project: str, dry_run: bool = False) -> ClearScopeResult:
        normalized_project = project.strip()
        if not normalized_project:
            raise ValueError("project is required.")
        with self._lock, self._pool.checkout() as connection:
            result = self._clear_scope_rows(connection, scope="project", project=normalized_project, dry_run=dry_run)
            if not dry_run:
                self.emit_audit_event(
                    event_type="graph.scope_cleared",
                    resource_type="project",
                    resource_id=normalized_project,
                    action="delete",
                    metadata=result.model_dump(mode="json"),
                    connection=connection,
                )
            return result

    def clear_all(self, *, dry_run: bool = False) -> ClearScopeResult:
        with self._lock, self._pool.checkout() as connection:
            result = self._clear_scope_rows(connection, scope="all", dry_run=dry_run)
            if not dry_run:
                self.emit_audit_event(
                    event_type="graph.scope_cleared",
                    resource_type="tenant",
                    resource_id=self.tenant_id,
                    action="delete",
                    metadata=result.model_dump(mode="json"),
                    connection=connection,
                )
            return result

    def _clear_scope_rows(
        self,
        connection: sqlite3.Connection,
        *,
        scope: str,
        project: str = "",
        session_id: str = "",
        dry_run: bool = False,
    ) -> ClearScopeResult:
        result = ClearScopeResult(scope=scope, project=project, session_id=session_id, dry_run=dry_run)
        if scope == "all":
            node_rows = connection.execute(
                "SELECT id, node_type FROM nodes WHERE tenant_id = ?",
                (self.tenant_id,),
            ).fetchall()
            node_ids = [str(row["id"]) for row in node_rows]
            window_ids = [
                str(row["id"])
                for row in connection.execute(
                    "SELECT id FROM context_windows WHERE tenant_id = ?",
                    (self.tenant_id,),
                ).fetchall()
            ]
            repo_ids = [
                str(row["id"])
                for row in connection.execute(
                    "SELECT id FROM repos WHERE tenant_id = ?",
                    (self.tenant_id,),
                ).fetchall()
            ]
            result.deleted_graph_ui_rows = connection.execute(
                "SELECT COUNT(*) FROM graph_ui_state WHERE tenant_id = ?",
                (self.tenant_id,),
            ).fetchone()[0]
            result.deleted_transcripts = connection.execute(
                "SELECT COUNT(*) FROM transcript_records WHERE tenant_id = ?",
                (self.tenant_id,),
            ).fetchone()[0]

            if not dry_run:
                connection.execute(
                    "DELETE FROM graph_ui_state WHERE tenant_id = ?",
                    (self.tenant_id,),
                )
                connection.execute(
                    "DELETE FROM transcript_records WHERE tenant_id = ?",
                    (self.tenant_id,),
                )
        elif scope == "project":
            repo_ids = [
                str(row["id"])
                for row in connection.execute(
                    "SELECT id FROM repos WHERE tenant_id = ? AND name = ?",
                    (self.tenant_id, project),
                ).fetchall()
            ]
            window_ids = [
                str(row["id"])
                for row in connection.execute(
                    """
                    SELECT cw.id
                    FROM context_windows cw
                    JOIN repos r ON r.id = cw.repo_id
                    WHERE cw.tenant_id = ? AND r.name = ?
                    """,
                    (self.tenant_id, project),
                ).fetchall()
            ]
            node_rows = connection.execute(
                "SELECT id, node_type FROM nodes WHERE tenant_id = ? AND project = ?",
                (self.tenant_id, project),
            ).fetchall()
            node_ids = [str(row["id"]) for row in node_rows]
            result.deleted_graph_ui_rows = connection.execute(
                "SELECT COUNT(*) FROM graph_ui_state WHERE tenant_id = ? AND project = ?",
                (self.tenant_id, project),
            ).fetchone()[0]
            result.deleted_transcripts = connection.execute(
                "SELECT COUNT(*) FROM transcript_records WHERE tenant_id = ? AND project = ?",
                (self.tenant_id, project),
            ).fetchone()[0]

            if not dry_run:
                connection.execute(
                    "DELETE FROM graph_ui_state WHERE tenant_id = ? AND project = ?",
                    (self.tenant_id, project),
                )
                connection.execute(
                    "DELETE FROM transcript_records WHERE tenant_id = ? AND project = ?",
                    (self.tenant_id, project),
                )
        elif scope == "session":
            repo_ids = []
            window_ids = [
                str(row["id"])
                for row in connection.execute(
                    "SELECT id FROM context_windows WHERE tenant_id = ? AND session_id = ?",
                    (self.tenant_id, session_id),
                ).fetchall()
            ]
            node_rows = connection.execute(
                "SELECT id, node_type FROM nodes WHERE tenant_id = ? AND session_id = ?",
                (self.tenant_id, session_id),
            ).fetchall()
            node_ids = [str(row["id"]) for row in node_rows]
            result.deleted_graph_ui_rows = connection.execute(
                "SELECT COUNT(*) FROM graph_ui_state WHERE tenant_id = ? AND session_id = ?",
                (self.tenant_id, session_id),
            ).fetchone()[0]
            result.deleted_transcripts = connection.execute(
                "SELECT COUNT(*) FROM transcript_records WHERE tenant_id = ? AND session_id = ?",
                (self.tenant_id, session_id),
            ).fetchone()[0]

            if not dry_run:
                connection.execute(
                    "DELETE FROM graph_ui_state WHERE tenant_id = ? AND session_id = ?",
                    (self.tenant_id, session_id),
                )
                connection.execute(
                    "DELETE FROM transcript_records WHERE tenant_id = ? AND session_id = ?",
                    (self.tenant_id, session_id),
                )
        else:
            raise ValueError(f"Unsupported clear scope: {scope}")

        # Compute counts by node type
        counts_by_node_type: dict[str, int] = {}
        for row in node_rows:
            nt = str(row["node_type"])
            counts_by_node_type[nt] = counts_by_node_type.get(nt, 0) + 1
        result.counts_by_node_type = counts_by_node_type

        if node_ids:
            placeholders = ", ".join("?" for _ in node_ids)
            result.deleted_edges = connection.execute(
                f"""
                SELECT COUNT(*) FROM edges
                WHERE tenant_id = ? AND (source_id IN ({placeholders}) OR target_id IN ({placeholders}))
                """,
                (self.tenant_id, *node_ids, *node_ids),
            ).fetchone()[0]
            result.deleted_nodes = len(node_ids)

            if not dry_run:
                connection.execute(
                    f"""
                    DELETE FROM edges
                    WHERE tenant_id = ? AND (source_id IN ({placeholders}) OR target_id IN ({placeholders}))
                    """,
                    (self.tenant_id, *node_ids, *node_ids),
                )
                connection.execute(
                    f"DELETE FROM nodes WHERE tenant_id = ? AND id IN ({placeholders})",
                    (self.tenant_id, *node_ids),
                )

        if window_ids:
            placeholders = ", ".join("?" for _ in window_ids)
            result.deleted_context_window_edges = connection.execute(
                f"""
                SELECT COUNT(*) FROM context_window_edges
                WHERE tenant_id = ? AND (source_window_id IN ({placeholders}) OR target_window_id IN ({placeholders}))
                """,
                (self.tenant_id, *window_ids, *window_ids),
            ).fetchone()[0]
            result.deleted_context_windows = connection.execute(
                f"SELECT COUNT(*) FROM context_windows WHERE tenant_id = ? AND id IN ({placeholders})",
                (self.tenant_id, *window_ids),
            ).fetchone()[0]

            if not dry_run:
                connection.execute(
                    f"""
                    DELETE FROM context_window_edges
                    WHERE tenant_id = ? AND (source_window_id IN ({placeholders}) OR target_window_id IN ({placeholders}))
                    """,
                    (self.tenant_id, *window_ids, *window_ids),
                )
                connection.execute(
                    f"DELETE FROM context_windows WHERE tenant_id = ? AND id IN ({placeholders})",
                    (self.tenant_id, *window_ids),
                )

        if repo_ids:
            placeholders = ", ".join("?" for _ in repo_ids)
            result.deleted_repos = connection.execute(
                f"SELECT COUNT(*) FROM repos WHERE tenant_id = ? AND id IN ({placeholders})",
                (self.tenant_id, *repo_ids),
            ).fetchone()[0]

            if not dry_run:
                connection.execute(
                    f"DELETE FROM repos WHERE tenant_id = ? AND id IN ({placeholders})",
                    (self.tenant_id, *repo_ids),
                )
        elif scope == "all":
            result.deleted_repos = len(repo_ids)

        return result

    def _require_node(self, connection: sqlite3.Connection, node_id: str) -> None:
        if self._fetch_node_row(connection, node_id) is None:
            raise ValueError(f"Node not found: {node_id}")

    def _find_duplicate_node(
        self,
        connection: sqlite3.Connection,
        *,
        node: Node,
        embedding: np.ndarray,
    ) -> tuple[Node, str, float | None] | None:
        filters = ["tenant_id = ?", "embedding IS NOT NULL"]
        params: list[Any] = [self.tenant_id]
        if node.project:
            filters.append("project = ?")
            params.append(node.project)
        if node.session_id:
            filters.append("session_id = ?")
            params.append(node.session_id)
        elif node.agent_id:
            filters.append("agent_id = ?")
            params.append(node.agent_id)

        rows = connection.execute(
            f"""
            SELECT id, agent_id, project, session_id, context_window_id, label, content, node_type, tags, source_prompt, metadata, evidence_records,
                   valid_from, valid_to, created_at, updated_at, access_count, embedding, tenant_id
            FROM nodes
            WHERE {" AND ".join(filters)}
            """,
            tuple(params),
        ).fetchall()

        normalized_label = normalize_text(node.label)
        normalized_content = normalize_text(node.content)
        # Type-aware cosine threshold — decisions merge at 0.82, facts at 0.92, etc.
        type_threshold = type_aware_dedup_threshold(node.node_type, default=self.dedup_similarity_threshold)
        best_match: tuple[Node, float] | None = None

        # Pre-normalise the query embedding ONCE so the inner loop only needs a
        # single np.dot() per candidate instead of two norm computations. Use a
        # fresh local so we don't shadow the `embedding` parameter — keeps the
        # invariant "normalisation happens here, not silently for callers" clear.
        _emb_norm = float(np.linalg.norm(embedding))
        query_unit = embedding / _emb_norm if _emb_norm > 0.0 else embedding

        for row in rows:
            existing_node = self._row_to_node(row)
            if not _scope_matches(
                existing_node,
                agent_id=node.agent_id,
                project=node.project,
                session_id=node.session_id,
            ):
                continue
            if not compatible_node_types(node.node_type, existing_node.node_type):
                continue
            existing_label = normalize_text(existing_node.label)
            existing_content = normalize_text(existing_node.content)

            # ── Layer 0: entity-key hard block ────────────────────────
            # If both nodes name a specific technology AND those technologies
            # are different (but in the same category), block the merge.
            # e.g. "use PostgreSQL" vs "use MySQL" — similar sentence, different choice.
            node_entity = extract_choice_entity(node.content)
            existing_entity = extract_choice_entity(existing_node.content)
            if (
                node_entity is not None
                and existing_entity is not None
                and node_entity[1] == existing_entity[1]  # same category
                and node_entity[0] != existing_entity[0]  # different entity
                and not describes_rejected_or_limited_option(node.content)
                and not describes_rejected_or_limited_option(existing_node.content)
            ):
                continue  # never merge "postgres" node with "mysql" node

            # ── Layer 0b: numeric-conflict guard ───────────────────────
            # Same entity BUT different critical number (e.g. JWT 15min vs 1hr).
            # Conflicting numbers signal distinct facts, not duplicates.
            # Also applies to non-entity facts that have conflicting numbers.
            if contains_conflicting_numbers(node.content, existing_node.content) and (
                node_entity is None or existing_entity is None or node_entity[0] == existing_entity[0]
            ):
                continue
            if contains_conflicting_months(node.content, existing_node.content):
                continue

            if normalized_content == existing_content:
                return existing_node, "exact_content", 1.0

            # ── Layer 2: substring containment (cheap, catches rephrased subsets)
            if len(normalized_content) >= 10 and len(existing_content) >= 10:
                if normalized_content in existing_content or existing_content in normalized_content:
                    return existing_node, "content_substring", 0.98

            # ── Layer 3: semantic similarity (expensive — compute embedding once) ─
            existing_embedding = self._decode_embedding(row["embedding"])
            if existing_embedding is None:
                continue
            # Fast dot() — both vectors are unit-norm here, so this equals cosine.
            similarity = float(np.dot(query_unit, existing_embedding / (np.linalg.norm(existing_embedding) or 1.0)))
            label_score = label_similarity(node.label, existing_node.label)
            acronym_match = is_acronym_match(node.label, existing_node.label)

            if normalized_label == existing_label and similarity >= self.dedup_same_label_threshold:
                return existing_node, "same_label_high_similarity", similarity
            if acronym_match and similarity >= max(self.dedup_same_label_threshold - 0.25, 0.55):
                return existing_node, "acronym_entity_match", similarity
            if label_score >= 0.92 and similarity >= max(self.dedup_same_label_threshold - 0.2, 0.6):
                return existing_node, "label_entity_match", similarity

            # ── Layer 3b: same-entity aggressive merge ──────────────────
            # If both nodes reference the SAME named entity, lower the cosine
            # threshold significantly — "fastapi was chosen" and "we chose fastapi
            # because async" should merge even at cosine ~0.65.
            # The numeric-conflict guard (Layer 0b) already blocked cases where
            # the same entity appears with different critical numbers.
            if (
                node_entity is not None
                and existing_entity is not None
                and node_entity[0] == existing_entity[0]  # identical entity token
                and similarity >= 0.60
            ):
                return existing_node, "same_entity_merge", similarity

            # ── Layer 3c: Jaccard-boosted merge (type-aware lower threshold) ──
            # If content words overlap significantly AND cosine is high for the
            # node type, treat as duplicate — catches paraphrase true-dups.
            jaccard = content_token_jaccard(node.content, existing_node.content)
            boosted_threshold = max(type_threshold - 0.05, 0.70)
            if jaccard >= 0.35 and similarity >= boosted_threshold:
                return existing_node, "jaccard_boosted_similarity", similarity

            # ── Layer 3d: entity-less paraphrase merge ─────────────────
            # Some true duplicates share meaning but have no named entity anchor
            # and too little word overlap for the Jaccard gate above.
            if node_entity is None and existing_entity is None:
                paraphrase_score = paraphrase_dedup_score(
                    semantic_similarity=similarity,
                    lexical_overlap=jaccard,
                )
                paraphrase_threshold = max(type_threshold - 0.10, 0.72)
                if paraphrase_score >= paraphrase_threshold:
                    return existing_node, "entityless_paraphrase", paraphrase_score

            concept_overlap = canonical_concept_overlap(node.content, existing_node.content)
            if (
                node_entity is not None
                and existing_entity is not None
                and node_entity[0] == existing_entity[0]
                and concept_overlap >= 0.30
            ):
                return existing_node, "same_entity_concept_overlap", concept_overlap
            if concept_overlap >= 0.50 and similarity >= 0.35:
                return existing_node, "canonical_concept_overlap", concept_overlap

            # ── Layer 3e: pure cosine fallback (conservative global threshold) ─
            if similarity >= self.dedup_similarity_threshold:
                if best_match is None or similarity > best_match[1]:
                    best_match = (existing_node, similarity)

        if best_match is None:
            return None

        return best_match[0], "high_similarity", best_match[1]

    def _merge_duplicate_node(
        self,
        connection: sqlite3.Connection,
        *,
        existing_node: Node,
        incoming_node: Node,
    ) -> Node:
        merged_tags = list(dict.fromkeys([*existing_node.tags, *incoming_node.tags]))
        updated_source_prompt = existing_node.source_prompt or incoming_node.source_prompt
        updated_source_turn_pair_id = existing_node.source_turn_pair_id or incoming_node.source_turn_pair_id
        merged_metadata = dict(existing_node.metadata)
        for key, value in incoming_node.metadata.items():
            if key not in merged_metadata:
                merged_metadata[key] = value
        merged_evidence = merge_evidence_records(existing_node.evidence_records, incoming_node.evidence_records)
        merged_valid_from, merged_valid_to = merge_validity_windows(
            existing_node.valid_from,
            incoming_node.valid_from,
            existing_node.valid_to,
            incoming_node.valid_to,
        )
        # Track all phrasings that have been merged into this canonical node.
        # The incoming content is a new alias unless it's already the canonical content
        # or already present in the alias list.
        merged_aliases = list(
            dict.fromkeys(
                [
                    *existing_node.aliases,
                    *(
                        [incoming_node.content]
                        if incoming_node.content != existing_node.content
                        and incoming_node.content not in existing_node.aliases
                        else []
                    ),
                ]
            )
        )
        updated_at = utc_now()
        connection.execute(
            """
            UPDATE nodes
            SET agent_id = ?, project = ?, session_id = ?, context_window_id = COALESCE(context_window_id, ?),
                tags = ?, aliases = ?, metadata = ?, source_prompt = ?, embedding_model_id = ?, embedding_dim = ?, source_turn_pair_id = ?, evidence_records = ?, valid_from = ?, valid_to = ?, updated_at = ?
            WHERE id = ? AND tenant_id = ?
            """,
            (
                _merge_scope_value(existing_node.agent_id, incoming_node.agent_id),
                _merge_scope_value(existing_node.project, incoming_node.project),
                _merge_scope_value(existing_node.session_id, incoming_node.session_id),
                incoming_node.context_window_id,
                json.dumps(merged_tags),
                json.dumps(merged_aliases),
                _encode_metadata(merged_metadata),
                updated_source_prompt,
                existing_node.embedding_model_id or incoming_node.embedding_model_id,
                existing_node.embedding_dim or incoming_node.embedding_dim,
                updated_source_turn_pair_id,
                _encode_evidence_records(merged_evidence),
                merged_valid_from.isoformat() if merged_valid_from is not None else None,
                merged_valid_to.isoformat() if merged_valid_to is not None else None,
                updated_at.isoformat(),
                existing_node.id,
                self.tenant_id,
            ),
        )
        return Node(
            id=existing_node.id,
            tenant_id=existing_node.tenant_id,
            agent_id=_merge_scope_value(existing_node.agent_id, incoming_node.agent_id),
            project=_merge_scope_value(existing_node.project, incoming_node.project),
            session_id=_merge_scope_value(existing_node.session_id, incoming_node.session_id),
            context_window_id=existing_node.context_window_id or incoming_node.context_window_id,
            label=existing_node.label,
            content=existing_node.content,
            node_type=existing_node.node_type,
            tags=merged_tags,
            aliases=merged_aliases,
            source_prompt=updated_source_prompt,
            embedding_model_id=existing_node.embedding_model_id or incoming_node.embedding_model_id,
            embedding_dim=existing_node.embedding_dim or incoming_node.embedding_dim,
            source_turn_pair_id=updated_source_turn_pair_id,
            metadata=merged_metadata,
            evidence_records=merged_evidence,
            valid_from=merged_valid_from,
            valid_to=merged_valid_to,
            created_at=existing_node.created_at,
            updated_at=updated_at,
            access_count=existing_node.access_count,
        )

    def canonicalize_node(
        self,
        node_ids: list[str],
        canonical_id: str,
    ) -> CanonicalizeResult:
        """Manually merge *node_ids* into *canonical_id*.

        All aliases from the merged nodes flow into the canonical node's aliases.
        All edges pointing to/from merged nodes are re-pointed to the canonical node.
        Merged nodes are deleted.  Idempotent: merging an already-merged node is a no-op.
        """
        with self._lock, self._pool.checkout() as connection:
            # Fetch canonical node
            canonical_row = connection.execute(
                "SELECT id, agent_id, project, session_id, context_window_id, label, content, node_type, tags, aliases, source_prompt, metadata, evidence_records, valid_from, valid_to, created_at, updated_at, access_count, embedding, embedding_model_id, embedding_dim, source_turn_pair_id, tenant_id FROM nodes WHERE id = ? AND tenant_id = ?",
                (canonical_id, self.tenant_id),
            ).fetchone()
            if canonical_row is None:
                raise ValidationFailure(f"Canonical node {canonical_id!r} not found.")
            canonical_node = self._row_to_node(canonical_row)

            merged_ids: list[str] = []
            all_aliases: list[str] = list(canonical_node.aliases)
            edges_repointed = 0
            new_aliases: list[str] = []

            for node_id in node_ids:
                if node_id == canonical_id:
                    continue  # idempotent: skip self
                row = connection.execute(
                    "SELECT id, agent_id, project, session_id, context_window_id, label, content, node_type, tags, aliases, source_prompt, metadata, evidence_records, valid_from, valid_to, created_at, updated_at, access_count, embedding, embedding_model_id, embedding_dim, source_turn_pair_id, tenant_id FROM nodes WHERE id = ? AND tenant_id = ?",
                    (node_id, self.tenant_id),
                ).fetchone()
                if row is None:
                    continue  # already deleted — idempotent
                node = self._row_to_node(row)

                # Collect aliases: the node's content + its own aliases
                for phrase in [node.content, *node.aliases]:
                    if phrase and phrase != canonical_node.content and phrase not in all_aliases:
                        all_aliases.append(phrase)
                        new_aliases.append(phrase)

                # Delete any direct edges between node_id and canonical_id to avoid self-loops
                connection.execute(
                    """
                    DELETE FROM edges
                    WHERE ((source_id = ? AND target_id = ?) OR (source_id = ? AND target_id = ?))
                      AND tenant_id = ?
                    """,
                    (node_id, canonical_id, canonical_id, node_id, self.tenant_id),
                )

                # Re-point edges: source_id → canonical_id
                repointed = connection.execute(
                    """
                    UPDATE edges
                    SET source_id = ?
                    WHERE source_id = ? AND tenant_id = ?
                      AND NOT EXISTS (
                          SELECT 1 FROM edges e2
                          WHERE e2.source_id = ? AND e2.target_id = edges.target_id
                            AND e2.relationship = edges.relationship AND e2.tenant_id = edges.tenant_id
                      )
                    """,
                    (canonical_id, node_id, self.tenant_id, canonical_id),
                ).rowcount
                edges_repointed += repointed

                # Re-point edges: target_id → canonical_id
                repointed = connection.execute(
                    """
                    UPDATE edges
                    SET target_id = ?
                    WHERE target_id = ? AND tenant_id = ?
                      AND NOT EXISTS (
                          SELECT 1 FROM edges e2
                          WHERE e2.source_id = edges.source_id AND e2.target_id = ?
                            AND e2.relationship = edges.relationship AND e2.tenant_id = edges.tenant_id
                      )
                    """,
                    (canonical_id, node_id, self.tenant_id, canonical_id),
                ).rowcount
                edges_repointed += repointed

                # Delete any remaining duplicate edges (self-loops or exact duplicates)
                connection.execute(
                    "DELETE FROM edges WHERE (source_id = ? OR target_id = ?) AND tenant_id = ?",
                    (node_id, node_id, self.tenant_id),
                )

                # Delete the merged node
                connection.execute(
                    "DELETE FROM nodes WHERE id = ? AND tenant_id = ?",
                    (node_id, self.tenant_id),
                )
                merged_ids.append(node_id)

            # Persist updated aliases on canonical node
            updated_at = utc_now()
            connection.execute(
                "UPDATE nodes SET aliases = ?, updated_at = ? WHERE id = ? AND tenant_id = ?",
                (json.dumps(all_aliases), updated_at.isoformat(), canonical_id, self.tenant_id),
            )

            # Re-fetch canonical node with updated aliases
            updated_row = connection.execute(
                "SELECT id, agent_id, project, session_id, context_window_id, label, content, node_type, tags, aliases, source_prompt, metadata, evidence_records, valid_from, valid_to, created_at, updated_at, access_count, embedding, embedding_model_id, embedding_dim, source_turn_pair_id, tenant_id FROM nodes WHERE id = ? AND tenant_id = ?",
                (canonical_id, self.tenant_id),
            ).fetchone()
            updated_canonical = self._row_to_node(updated_row)

        return CanonicalizeResult(
            canonical_node=updated_canonical,
            merged_node_ids=merged_ids,
            edges_repointed=edges_repointed,
            aliases_added=new_aliases,
        )

    def dedup_candidates(
        self,
        scope: dict[str, str] | None = None,
        threshold: float = 0.85,
    ) -> DedupCandidatesResult:
        """Return pairs of nodes whose embeddings are above *threshold* but below the
        auto-merge threshold.  Intended for human review before calling canonicalize_node.

        Args:
            scope: Optional dict with keys ``project``, ``agent_id``, ``session_id``.
            threshold: Minimum cosine similarity to report (default 0.85).
        """
        scope = scope or {}
        project = str(scope.get("project", "")).strip()
        agent_id = str(scope.get("agent_id", "")).strip()
        session_id = str(scope.get("session_id", "")).strip()

        filters = ["tenant_id = ?", "embedding IS NOT NULL"]
        params: list[Any] = [self.tenant_id]
        if project:
            filters.append("project = ?")
            params.append(project)
        if session_id:
            filters.append("session_id = ?")
            params.append(session_id)
        if agent_id:
            filters.append("agent_id = ?")
            params.append(agent_id)

        with self._lock, self._pool.checkout() as connection:
            rows = connection.execute(
                f"SELECT id, label, node_type, embedding FROM nodes WHERE {' AND '.join(filters)}",
                tuple(params),
            ).fetchall()

        total = len(rows)
        pairs: list[DedupCandidatePair] = []

        for i in range(total):
            emb_i = self._decode_embedding(rows[i]["embedding"])
            if emb_i is None:
                continue
            type_i = NodeType(rows[i]["node_type"])
            for j in range(i + 1, total):
                type_j = NodeType(rows[j]["node_type"])
                if not compatible_node_types(type_i, type_j):
                    continue
                emb_j = self._decode_embedding(rows[j]["embedding"])
                if emb_j is None:
                    continue
                sim = self.embedding_model.cosine_similarity(emb_i, emb_j)
                # Report pairs above threshold but below the auto-merge threshold
                auto_threshold = type_aware_dedup_threshold(type_i, default=self.dedup_similarity_threshold)
                if threshold <= sim < auto_threshold:
                    pairs.append(
                        DedupCandidatePair(
                            node_id_a=rows[i]["id"],
                            node_id_b=rows[j]["id"],
                            label_a=rows[i]["label"],
                            label_b=rows[j]["label"],
                            similarity=round(sim, 4),
                        )
                    )

        # Sort by descending similarity so the most likely duplicates appear first
        pairs.sort(key=lambda p: p.similarity, reverse=True)
        return DedupCandidatesResult(
            pairs=pairs,
            threshold=threshold,
            total_nodes_scanned=total,
        )

    def _register_conflicts(
        self,
        connection: sqlite3.Connection,
        node: Node,
    ) -> list[ConflictRecord]:
        if node.node_type not in {NodeType.PREFERENCE, NodeType.DECISION}:
            return []

        filters = ["tenant_id = ?", "id != ?"]
        params: list[Any] = [self.tenant_id, node.id]
        if node.project:
            filters.append("project = ?")
            params.append(node.project)
        if node.session_id:
            filters.append("session_id = ?")
            params.append(node.session_id)
        elif node.agent_id:
            filters.append("agent_id = ?")
            params.append(node.agent_id)

        rows = connection.execute(
            f"""
            SELECT id, agent_id, project, session_id, context_window_id, label, content, node_type, tags, source_prompt, metadata,
                   evidence_records, valid_from, valid_to, created_at, updated_at, access_count, embedding, tenant_id
            FROM nodes
            WHERE {" AND ".join(filters)}
            """,
            tuple(params),
        ).fetchall()
        conflicts: list[ConflictRecord] = []
        for row in rows:
            existing_node = self._row_to_node(row)
            if not _scope_matches(
                existing_node,
                agent_id=node.agent_id,
                project=node.project,
                session_id=node.session_id,
            ):
                continue
            reason = detect_conflict_reason(existing_node, node)
            if reason is None:
                continue
            existing_edge = self._find_existing_edge(
                connection,
                source_id=node.id,
                target_id=existing_node.id,
                relationship=RelationType.CONTRADICTS,
            )
            if existing_edge is None:
                edge = Edge(
                    tenant_id=self.tenant_id,
                    source_id=node.id,
                    target_id=existing_node.id,
                    relationship=RelationType.CONTRADICTS,
                    metadata={"origin": "auto-conflict", "reason": reason},
                )
                connection.execute(
                    """
                    INSERT INTO edges (
                        id, tenant_id, source_id, target_id, relationship, weight, metadata, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        edge.id,
                        edge.tenant_id,
                        edge.source_id,
                        edge.target_id,
                        edge.relationship,
                        edge.weight,
                        json.dumps(edge.metadata),
                        edge.created_at.isoformat(),
                    ),
                )
                self._mark_node_superseded(
                    connection, old_node=existing_node, new_node=node, relationship=edge.relationship
                )
            conflicts.append(
                ConflictRecord(
                    other_node_id=existing_node.id,
                    other_node_label=existing_node.label,
                    reason=reason,
                )
            )
        return conflicts

    def _mark_node_superseded(
        self,
        connection: sqlite3.Connection,
        *,
        old_node: Node,
        new_node: Node,
        relationship: str,
    ) -> None:
        metadata = dict(old_node.metadata)
        metadata["superseded_by"] = new_node.id
        metadata["superseded_at"] = utc_now().isoformat()
        metadata["superseded_relationship"] = relationship
        connection.execute(
            "UPDATE nodes SET metadata = ?, updated_at = ? WHERE id = ? AND tenant_id = ?",
            (
                _encode_metadata(metadata),
                metadata["superseded_at"],
                old_node.id,
                self.tenant_id,
            ),
        )

    def _build_conflict_entries(
        self,
        connection: sqlite3.Connection,
        *,
        edges: list[Edge],
        include_resolved: bool,
        limit: int,
    ) -> list[ConflictEntry]:
        node_ids = list(dict.fromkeys([edge.source_id for edge in edges] + [edge.target_id for edge in edges]))
        nodes_by_id = {node.id: node for node in self._fetch_nodes_by_ids(connection, node_ids)}
        entries: list[ConflictEntry] = []
        for edge in edges:
            resolved, resolution_note, resolved_at = self._conflict_resolution_state(edge)
            if resolved and not include_resolved:
                continue
            source_node = nodes_by_id.get(edge.source_id)
            target_node = nodes_by_id.get(edge.target_id)
            if source_node is None or target_node is None:
                continue
            entries.append(
                ConflictEntry(
                    edge=edge,
                    source_node=source_node,
                    target_node=target_node,
                    resolved=resolved,
                    resolution_note=resolution_note,
                    resolved_at=resolved_at,
                )
            )
            if len(entries) >= limit:
                break
        return entries

    def _conflict_resolution_state(self, edge: Edge) -> tuple[bool, str, datetime | None]:
        metadata = edge.metadata or {}
        resolved = bool(metadata.get("resolved"))
        resolution_note = str(metadata.get("resolution_note", "") or "")
        resolved_at_raw = metadata.get("resolved_at")
        resolved_at = _parse_datetime(resolved_at_raw) if resolved_at_raw else None
        return resolved, resolution_note, resolved_at

    def _insert_edge_record(
        self,
        connection: sqlite3.Connection,
        *,
        source_id: str,
        target_id: str,
        relationship: str,
    ) -> Edge:
        edge = Edge(
            tenant_id=self.tenant_id,
            source_id=source_id,
            target_id=target_id,
            relationship=relationship,
        )
        connection.execute(
            """
            INSERT INTO edges (
                id, tenant_id, source_id, target_id, relationship, weight, metadata, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                edge.id,
                edge.tenant_id,
                edge.source_id,
                edge.target_id,
                edge.relationship,
                edge.weight,
                _encode_metadata(edge.metadata),
                edge.created_at.isoformat(),
            ),
        )
        self.emit_audit_event(
            event_type="graph.relationship.created",
            resource_type="edge",
            resource_id=edge.id,
            action="create",
            metadata={"relationship": edge.relationship},
            connection=connection,
        )
        return edge

    def _delete_edge_record(
        self,
        connection: sqlite3.Connection,
        *,
        source_id: str,
        target_id: str,
        relationship: str,
    ) -> bool:
        cursor = connection.execute(
            """
            DELETE FROM edges
            WHERE tenant_id = ? AND source_id = ? AND target_id = ? AND relationship = ?
            """,
            (self.tenant_id, source_id, target_id, normalize_relationship(relationship)),
        )
        return int(cursor.rowcount or 0) > 0

    def _find_existing_edge(
        self,
        connection: sqlite3.Connection,
        *,
        source_id: str,
        target_id: str,
        relationship: str | RelationType,
    ) -> Edge | None:
        row = connection.execute(
            """
            SELECT id, source_id, target_id, relationship, weight, metadata, created_at, tenant_id
            FROM edges
            WHERE tenant_id = ? AND source_id = ? AND target_id = ? AND relationship = ?
            LIMIT 1
            """,
            (self.tenant_id, source_id, target_id, normalize_relationship(relationship)),
        ).fetchone()
        return self._row_to_edge(row) if row is not None else None
