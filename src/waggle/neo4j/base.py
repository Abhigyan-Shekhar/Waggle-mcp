from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from waggle.neo4j import Neo4jMemoryGraph

import networkx as nx
import numpy as np

from waggle.errors import ValidationFailure
from waggle.graph.base import (
    MemoryGraphBase,
    _decode_evidence_records,
    _decode_metadata,
    _encode_evidence_records,
    _parse_datetime,
)
from waggle.models import (
    Node,
    NodeType,
    TenantRecord,
    TranscriptRecord,
    utc_now,
)

SCHEMA_VERSION = 2


class Neo4jMemoryGraphBase(MemoryGraphBase):
    def __init__(
        self,
        *,
        uri: str,
        username: str,
        password: str,
        database: str | None,
        embedding_model: Any,
        tenant_id: str = "local-default",
        dedup_similarity_threshold: float = 0.97,
        dedup_same_label_threshold: float = 0.9,
        export_dir: str | Path | None = None,
        api_key_environment: str = "test",
        _driver: Any | None = None,
        _owns_driver: bool = True,
    ) -> None:
        try:
            from neo4j import GraphDatabase
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "Neo4j backend requested but the neo4j package is not installed. "
                'Install it with `pip install -e ".[neo4j]"`.'
            ) from exc

        self._driver = _driver or GraphDatabase.driver(uri, auth=(username, password))
        self._owns_driver = _owns_driver
        self._uri = uri
        self._username = username
        self._password = password
        self.database = database or None
        self.embedding_model = embedding_model
        self.tenant_id = tenant_id.strip() or "local-default"
        self.dedup_similarity_threshold = dedup_similarity_threshold
        self.dedup_same_label_threshold = dedup_same_label_threshold
        self.export_dir = Path(export_dir).expanduser() if export_dir is not None else Path.cwd() / "exports"
        self.api_key_environment = api_key_environment
        self._lock = threading.RLock()
        self._initialize_database()

    def _session(self):
        return self._driver.session(database=self.database) if self.database else self._driver.session()

    def _initialize_database(self) -> None:
        with self._lock, self._session() as session:
            session.run(
                """
                CREATE CONSTRAINT waggle_node_id IF NOT EXISTS
                FOR (n:MemoryNode) REQUIRE n.id IS UNIQUE
                """
            ).consume()
            session.run(
                """
                CREATE CONSTRAINT waggle_edge_id IF NOT EXISTS
                FOR ()-[r:MEMORY_EDGE]-() REQUIRE r.id IS UNIQUE
                """
            ).consume()
            session.run(
                """
                CREATE CONSTRAINT waggle_transcript_id IF NOT EXISTS
                FOR (t:MemoryTranscript) REQUIRE t.id IS UNIQUE
                """
            ).consume()
            session.run(
                """
                CREATE CONSTRAINT waggle_tenant_id IF NOT EXISTS
                FOR (t:GraphTenant) REQUIRE t.tenant_id IS UNIQUE
                """
            ).consume()
            session.run(
                """
                CREATE CONSTRAINT waggle_api_key_id IF NOT EXISTS
                FOR (a:GraphApiKey) REQUIRE a.api_key_id IS UNIQUE
                """
            ).consume()
            session.run(
                """
                CREATE CONSTRAINT waggle_retention_policy_tenant IF NOT EXISTS
                FOR (p:GraphRetentionPolicy) REQUIRE p.tenant_id IS UNIQUE
                """
            ).consume()
            session.run(
                """
                CREATE CONSTRAINT waggle_retention_run_id IF NOT EXISTS
                FOR (r:GraphRetentionPruneRun) REQUIRE r.run_id IS UNIQUE
                """
            ).consume()
            session.run(
                """
                CREATE CONSTRAINT waggle_audit_event_id IF NOT EXISTS
                FOR (a:GraphAuditEvent) REQUIRE a.event_id IS UNIQUE
                """
            ).consume()
            session.run(
                """
                CREATE INDEX waggle_node_tenant_updated IF NOT EXISTS
                FOR (n:MemoryNode) ON (n.tenant_id, n.updated_at)
                """
            ).consume()
            session.run(
                """
                CREATE INDEX waggle_node_tenant_type IF NOT EXISTS
                FOR (n:MemoryNode) ON (n.tenant_id, n.node_type)
                """
            ).consume()
            session.run(
                """
                CREATE INDEX waggle_transcript_tenant_observed IF NOT EXISTS
                FOR (t:MemoryTranscript) ON (t.tenant_id, t.observed_at)
                """
            ).consume()
            session.run(
                """
                CREATE INDEX waggle_transcript_tenant_session_turn IF NOT EXISTS
                FOR (t:MemoryTranscript) ON (t.tenant_id, t.session_id, t.turn_index)
                """
            ).consume()
            session.run(
                """
                CREATE INDEX waggle_transcript_tenant_project IF NOT EXISTS
                FOR (t:MemoryTranscript) ON (t.tenant_id, t.project)
                """
            ).consume()
            session.run(
                """
                CREATE INDEX waggle_transcript_tenant_agent IF NOT EXISTS
                FOR (t:MemoryTranscript) ON (t.tenant_id, t.agent_id)
                """
            ).consume()
            session.run(
                """
                CREATE INDEX waggle_api_key_hash IF NOT EXISTS
                FOR (a:GraphApiKey) ON (a.key_hash)
                """
            ).consume()
            session.run(
                """
                MATCH (n:MemoryNode)
                WHERE n.tenant_id IS NULL
                SET n.tenant_id = $tenant_id
                """,
                tenant_id=self.tenant_id,
            ).consume()
            session.run(
                """
                MATCH ()-[r:MEMORY_EDGE]->()
                WHERE r.tenant_id IS NULL
                SET r.tenant_id = $tenant_id
                """,
                tenant_id=self.tenant_id,
            ).consume()
            self.ensure_tenant(self.tenant_id)

    def for_tenant(self, tenant_id: str) -> Neo4jMemoryGraph:
        from waggle.neo4j import Neo4jMemoryGraph

        return Neo4jMemoryGraph(
            uri=self._uri,
            username=self._username,
            password=self._password,
            database=self.database,
            embedding_model=self.embedding_model,
            tenant_id=tenant_id,
            dedup_similarity_threshold=self.dedup_similarity_threshold,
            dedup_same_label_threshold=self.dedup_same_label_threshold,
            export_dir=self.export_dir,
            api_key_environment=self.api_key_environment,
            _driver=self._driver,
            _owns_driver=False,
        )

    def ensure_tenant(self, tenant_id: str, name: str = "") -> TenantRecord:
        normalized_tenant_id = tenant_id.strip()
        if not normalized_tenant_id:
            raise ValidationFailure("Tenant ID cannot be empty.")
        created_at = utc_now()
        with self._lock, self._session() as session:
            record = session.run(
                """
                MERGE (t:GraphTenant {tenant_id: $tenant_id})
                ON CREATE SET t.name = $name, t.status = 'active', t.created_at = $created_at
                ON MATCH SET t.name = CASE WHEN $name <> '' THEN $name ELSE t.name END
                RETURN t.tenant_id AS tenant_id, t.name AS name, t.status AS status, t.created_at AS created_at
                """,
                tenant_id=normalized_tenant_id,
                name=name.strip(),
                created_at=created_at.isoformat(),
            ).single()
        return TenantRecord(
            tenant_id=record["tenant_id"],
            name=record["name"] or "",
            status=record["status"],
            created_at=_parse_datetime(record["created_at"]),
        )

    def close(self) -> None:
        if self._owns_driver:
            self._driver.close()

    def _require_node(self, session: Any, node_id: str) -> None:
        if self._fetch_node(session, node_id) is None:
            raise ValueError(f"Node not found: {node_id}")

    def _fetch_node(self, session: Any, node_id: str) -> Node | None:
        record = session.run(
            """
            MATCH (n:MemoryNode {tenant_id: $tenant_id, id: $id})
            RETURN n
            """,
            tenant_id=self.tenant_id,
            id=node_id,
        ).single()
        if record is None:
            return None
        return self._node_from_props(record["n"])

    def _node_create_params(self, *, node: Node, embedding: np.ndarray) -> dict[str, Any]:
        return {
            "id": node.id,
            "tenant_id": node.tenant_id,
            "agent_id": node.agent_id,
            "project": node.project,
            "session_id": node.session_id,
            "label": node.label,
            "content": node.content,
            "node_type": node.node_type.value,
            "tags": node.tags,
            "embedding": embedding.astype(np.float32).tolist(),
            "source_prompt": node.source_prompt,
            "evidence_records": _encode_evidence_records(node.evidence_records),
            "valid_from": node.valid_from.isoformat() if node.valid_from is not None else None,
            "valid_to": node.valid_to.isoformat() if node.valid_to is not None else None,
            "created_at": node.created_at.isoformat(),
            "updated_at": node.updated_at.isoformat(),
            "access_count": node.access_count,
        }

    def _node_from_props(self, props: Any) -> Node:
        return Node(
            id=props["id"],
            tenant_id=props.get("tenant_id") or self.tenant_id,
            agent_id=props.get("agent_id") or "",
            project=props.get("project") or "",
            session_id=props.get("session_id") or "",
            label=props["label"],
            content=props["content"],
            node_type=NodeType(props["node_type"]),
            tags=list(props.get("tags") or []),
            source_prompt=props.get("source_prompt") or "",
            evidence_records=_decode_evidence_records(props.get("evidence_records")),
            valid_from=_parse_datetime(props["valid_from"]) if props.get("valid_from") else None,
            valid_to=_parse_datetime(props["valid_to"]) if props.get("valid_to") else None,
            created_at=_parse_datetime(props["created_at"]),
            updated_at=_parse_datetime(props["updated_at"]),
            access_count=int(props.get("access_count") or 0),
        )

    def _transcript_from_props(self, props: Any) -> TranscriptRecord:
        return TranscriptRecord(
            id=props["id"],
            tenant_id=props.get("tenant_id") or self.tenant_id,
            agent_id=props.get("agent_id") or "",
            project=props.get("project") or "",
            session_id=props.get("session_id") or "",
            observed_at=_parse_datetime(props["observed_at"]),
            turn_index=int(props.get("turn_index") or 0),
            role=props.get("role") or "",
            transcript_text=props["transcript_text"],
            metadata=_decode_metadata(props.get("metadata")),
        )

    def _transcript_scope_matches(
        self,
        record: TranscriptRecord,
        *,
        agent_id: str = "",
        project: str = "",
        session_id: str = "",
    ) -> bool:
        normalized_agent = agent_id.strip().lower()
        normalized_project = project.strip().lower()
        normalized_session = session_id.strip().lower()
        if normalized_agent and record.agent_id.strip().lower() != normalized_agent:
            return False
        if normalized_project and record.project.strip().lower() != normalized_project:
            return False
        return not (normalized_session and record.session_id.strip().lower() != normalized_session)

    def _parse_optional_datetime(self, raw: Any) -> datetime | None:
        if raw in (None, ""):
            return None
        if isinstance(raw, datetime):
            return raw if raw.tzinfo is not None else raw.replace(tzinfo=UTC)
        try:
            return _parse_datetime(str(raw))
        except ValueError:
            return None

    def _load_graph(self, session: Any) -> nx.DiGraph:
        graph = nx.DiGraph()
        for record in session.run(
            "MATCH (n:MemoryNode {tenant_id: $tenant_id}) RETURN n.id AS id",
            tenant_id=self.tenant_id,
        ):
            graph.add_node(record["id"])
        for record in session.run(
            """
            MATCH (source:MemoryNode {tenant_id: $tenant_id})-[:MEMORY_EDGE {tenant_id: $tenant_id}]->(target:MemoryNode {tenant_id: $tenant_id})
            RETURN source.id AS source_id, target.id AS target_id
            """,
            tenant_id=self.tenant_id,
        ):
            graph.add_edge(record["source_id"], record["target_id"])
        return graph

    def _fetch_nodes_by_ids(self, session: Any, node_ids: list[str]) -> list[Node]:
        if not node_ids:
            return []
        rows = {
            record["n"]["id"]: self._node_from_props(record["n"])
            for record in session.run(
                """
                MATCH (n:MemoryNode {tenant_id: $tenant_id})
                WHERE n.id IN $node_ids
                RETURN n
                """,
                tenant_id=self.tenant_id,
                node_ids=node_ids,
            )
        }
        return [rows[node_id] for node_id in node_ids if node_id in rows]
