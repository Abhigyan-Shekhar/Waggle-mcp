from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import numpy as np

from waggle.evidence import merge_evidence_records, merge_validity_windows
from waggle.graph.base import (
    MemoryGraphBase,
    _decode_evidence_records,
    _decode_list,
    _decode_metadata,
    _decode_string_list,
    _encode_evidence_records,
    _encode_metadata,
    _parse_datetime,
    _scope_matches,
    decode_embedding_blob,
)
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
from waggle.models import (
    ConflictEntry,
    ConflictRecord,
    Edge,
    EvidenceRecord,
    Node,
    NodeStoreResult,
    NodeType,
    RelationType,
    normalize_relationship,
    utc_now,
)

_UI_STATE_CACHE: dict[tuple[str, str, str, str], dict[str, Any]] = {}


def _default_ui_state() -> dict[str, Any]:
    return {
        "positions": {},
        "zoom": 1.0,
        "viewport": {"center_x": 0, "center_y": 0},
        "groups": [],
        "collapsed_groups": [],
        "selected_nodes": [],
    }


class Neo4jMutationMixin(MemoryGraphBase):
    def add_node(
        self,
        *,
        node_id: str | None = None,
        label: str,
        content: str,
        node_type: NodeType,
        tags: list[str] | None = None,
        source_prompt: str = "",
        agent_id: str = "",
        project: str = "",
        session_id: str = "",
        evidence_records: list[EvidenceRecord] | None = None,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
    ) -> NodeStoreResult:
        node_kwargs: dict[str, Any] = {}
        if node_id is not None and str(node_id).strip():
            node_kwargs["id"] = str(node_id).strip()
        node = Node(
            **node_kwargs,
            tenant_id=self.tenant_id,
            agent_id=agent_id,
            project=project,
            session_id=session_id,
            label=label,
            content=content,
            node_type=node_type,
            tags=tags or [],
            source_prompt=source_prompt,
            evidence_records=evidence_records or [],
            valid_from=valid_from,
            valid_to=valid_to,
        )
        embedding = self.embedding_model.embed(node.content)

        with self._lock, self._session() as session:
            existing = [
                self._node_from_props(record["n"])
                for record in session.run(
                    """
                    MATCH (n:MemoryNode {tenant_id: $tenant_id, node_type: $node_type})
                    RETURN n
                    """,
                    tenant_id=self.tenant_id,
                    node_type=node.node_type.value,
                )
            ]
            duplicate = self._find_duplicate_node(existing_nodes=existing, node=node, embedding=embedding)
            if duplicate is not None:
                existing_node, dedup_reason, similarity = duplicate
                merged_node = self._merge_duplicate_node(
                    session,
                    existing_node=existing_node,
                    incoming_node=node,
                )
                return NodeStoreResult(
                    node=merged_node,
                    created=False,
                    dedup_reason=dedup_reason,
                    similarity=similarity,
                )

            session.run(
                """
                CREATE (n:MemoryNode {
                    id: $id,
                    tenant_id: $tenant_id,
                    agent_id: $agent_id,
                    project: $project,
                    session_id: $session_id,
                    label: $label,
                    content: $content,
                    node_type: $node_type,
                    tags: $tags,
                    embedding: $embedding,
                    source_prompt: $source_prompt,
                    evidence_records: $evidence_records,
                    valid_from: $valid_from,
                    valid_to: $valid_to,
                    created_at: $created_at,
                    updated_at: $updated_at,
                    access_count: $access_count
                })
                """,
                **self._node_create_params(node=node, embedding=embedding),
            ).consume()
            conflicts = self._register_conflicts(session, node)
        return NodeStoreResult(node=node, created=True, conflicts=conflicts)

    def add_edge(
        self,
        *,
        edge_id: str | None = None,
        source_id: str,
        target_id: str,
        relationship: str | RelationType,
        weight: float = 1.0,
        metadata: dict[str, Any] | None = None,
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
        with self._lock, self._session() as session:
            self._require_node(session, edge.source_id)
            self._require_node(session, edge.target_id)
            existing_edge = self._find_existing_edge(
                session,
                source_id=edge.source_id,
                target_id=edge.target_id,
                relationship=edge.relationship,
            )
            if existing_edge is not None:
                return existing_edge
            session.run(
                """
                MATCH (source:MemoryNode {tenant_id: $tenant_id, id: $source_id})
                MATCH (target:MemoryNode {tenant_id: $tenant_id, id: $target_id})
                CREATE (source)-[:MEMORY_EDGE {
                    id: $id,
                    tenant_id: $tenant_id,
                    relationship: $relationship,
                    weight: $weight,
                    metadata: $metadata,
                    created_at: $created_at
                }]->(target)
                """,
                id=edge.id,
                tenant_id=self.tenant_id,
                source_id=edge.source_id,
                target_id=edge.target_id,
                relationship=edge.relationship,
                weight=edge.weight,
                metadata=_encode_metadata(edge.metadata),
                created_at=edge.created_at.isoformat(),
            ).consume()
        return edge

    def get_ui_state(
        self,
        *,
        project: str = "",
        agent_id: str = "",
        session_id: str = "",
    ) -> dict[str, Any]:
        key = (self.tenant_id, project.strip(), agent_id.strip(), session_id.strip())
        with self._lock, self._session() as session:
            record = session.run(
                """
                MATCH (ui:GraphUIState {
                    tenant_id: $tenant_id,
                    project: $project,
                    agent_id: $agent_id,
                    session_id: $session_id
                })
                RETURN ui.positions AS positions,
                       ui.zoom AS zoom,
                       ui.viewport AS viewport,
                       ui.groups AS groups,
                       ui.collapsed_groups AS collapsed_groups,
                       ui.selected_nodes AS selected_nodes
                LIMIT 1
                """,
                tenant_id=self.tenant_id,
                project=project.strip(),
                agent_id=agent_id.strip(),
                session_id=session_id.strip(),
            ).single()
        if record is None:
            return json.loads(json.dumps(_UI_STATE_CACHE.get(key, _default_ui_state())))
        value = {
            "positions": _decode_metadata(record["positions"]),
            "zoom": float(record["zoom"]) if record["zoom"] is not None else 1.0,
            "viewport": _decode_metadata(record["viewport"]) or {"center_x": 0, "center_y": 0},
            "groups": _decode_list(record["groups"]),
            "collapsed_groups": _decode_string_list(record["collapsed_groups"]),
            "selected_nodes": _decode_string_list(record["selected_nodes"]),
        }
        _UI_STATE_CACHE[key] = json.loads(json.dumps(value))
        return json.loads(json.dumps(value))

    def save_ui_state(
        self,
        *,
        project: str = "",
        agent_id: str = "",
        session_id: str = "",
        positions: dict[str, Any] | None = None,
        zoom: float | None = None,
        viewport: dict[str, Any] | None = None,
        groups: list[dict[str, Any]] | None = None,
        collapsed_groups: list[str] | None = None,
        selected_nodes: list[str] | None = None,
    ) -> dict[str, Any]:
        key = (self.tenant_id, project.strip(), agent_id.strip(), session_id.strip())
        current = self.get_ui_state(project=project, agent_id=agent_id, session_id=session_id)
        merged = {
            "positions": positions if positions is not None else current["positions"],
            "zoom": float(zoom if zoom is not None else current["zoom"]),
            "viewport": viewport if viewport is not None else current["viewport"],
            "groups": groups if groups is not None else current["groups"],
            "collapsed_groups": collapsed_groups if collapsed_groups is not None else current["collapsed_groups"],
            "selected_nodes": selected_nodes if selected_nodes is not None else current["selected_nodes"],
        }
        with self._lock, self._session() as session:
            session.run(
                """
                MERGE (ui:GraphUIState {
                    tenant_id: $tenant_id,
                    project: $project,
                    agent_id: $agent_id,
                    session_id: $session_id
                })
                SET ui.positions = $positions,
                    ui.zoom = $zoom,
                    ui.viewport = $viewport,
                    ui.groups = $groups,
                    ui.collapsed_groups = $collapsed_groups,
                    ui.selected_nodes = $selected_nodes,
                    ui.updated_at = $updated_at
                """,
                tenant_id=self.tenant_id,
                project=project.strip(),
                agent_id=agent_id.strip(),
                session_id=session_id.strip(),
                positions=_encode_metadata(merged["positions"]),
                zoom=merged["zoom"],
                viewport=_encode_metadata(merged["viewport"]),
                groups=json.dumps(merged["groups"], sort_keys=True),
                collapsed_groups=json.dumps(merged["collapsed_groups"], sort_keys=True),
                selected_nodes=json.dumps(merged["selected_nodes"], sort_keys=True),
                updated_at=utc_now().isoformat(),
            ).consume()
        _UI_STATE_CACHE[key] = json.loads(json.dumps(merged))
        return merged

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

        with self._lock, self._session() as session:
            node = self._fetch_node(session, node_id)

            if node is None:
                raise ValueError(f"Node not found: {node_id}")

            updated_node = Node(
                id=node.id,
                tenant_id=node.tenant_id,
                agent_id=agent_id if agent_id is not None else node.agent_id,
                project=project if project is not None else node.project,
                session_id=session_id if session_id is not None else node.session_id,
                label=label if label is not None else node.label,
                content=content if content is not None else node.content,
                node_type=node.node_type,
                tags=tags if tags is not None else node.tags,
                source_prompt=node.source_prompt,
                evidence_records=evidence_records if evidence_records is not None else node.evidence_records,
                valid_from=valid_from if valid_from is not None else node.valid_from,
                valid_to=valid_to if valid_to is not None else node.valid_to,
                created_at=node.created_at,
                updated_at=utc_now(),
                access_count=node.access_count,
            )

            embedding = None
            if content is not None:
                embedding = self.embedding_model.embed(updated_node.content).astype(np.float32).tolist()

            session.run(
                """
                MATCH (n:MemoryNode {tenant_id: $tenant_id, id: $id})
                SET n.label = $label,
                    n.content = $content,
                    n.tags = $tags,
                    n.agent_id = $agent_id,
                    n.project = $project,
                    n.session_id = $session_id,
                    n.valid_from = $valid_from,
                    n.valid_to = $valid_to,
                    n.evidence_records = $evidence_records,
                    n.updated_at = $updated_at,
                    n.embedding = CASE
                        WHEN $embedding IS NULL THEN n.embedding
                        ELSE $embedding
                    END
                """,
                id=updated_node.id,
                tenant_id=self.tenant_id,
                label=updated_node.label,
                content=updated_node.content,
                tags=updated_node.tags,
                agent_id=updated_node.agent_id,
                project=updated_node.project,
                session_id=updated_node.session_id,
                valid_from=updated_node.valid_from.isoformat() if updated_node.valid_from else None,
                valid_to=updated_node.valid_to.isoformat() if updated_node.valid_to else None,
                evidence_records=_encode_evidence_records(updated_node.evidence_records),
                updated_at=updated_node.updated_at.isoformat(),
                embedding=embedding,
            ).consume()

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

        with self._lock, self._session() as session:
            existing = session.run(
                """
                MATCH (source:MemoryNode {tenant_id: $tenant_id})-[r:MEMORY_EDGE {tenant_id: $tenant_id, id: $id}]->(target:MemoryNode {tenant_id: $tenant_id})
                RETURN r.id AS id, source.id AS source_id, target.id AS target_id,
                       r.relationship AS relationship, r.weight AS weight,
                       r.metadata AS metadata, r.created_at AS created_at
                LIMIT 1
                """,
                tenant_id=self.tenant_id,
                id=edge_id,
            ).single()
            if existing is None:
                raise ValueError(f"Edge not found: {edge_id}")
            edge = Edge(
                id=existing["id"],
                tenant_id=self.tenant_id,
                source_id=existing["source_id"],
                target_id=existing["target_id"],
                relationship=existing["relationship"],
                weight=float(existing["weight"]),
                metadata=_decode_metadata(existing["metadata"]),
                created_at=_parse_datetime(existing["created_at"]),
            )
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
            self._require_node(session, updated_edge.source_id)
            self._require_node(session, updated_edge.target_id)
            existing_duplicate = self._find_existing_edge(
                session,
                source_id=updated_edge.source_id,
                target_id=updated_edge.target_id,
                relationship=updated_edge.relationship,
            )
            if existing_duplicate is not None and existing_duplicate.id != updated_edge.id:
                session.run(
                    """
                    MATCH (:MemoryNode {tenant_id: $tenant_id})-[r:MEMORY_EDGE {tenant_id: $tenant_id, id: $id}]->(:MemoryNode {tenant_id: $tenant_id})
                    DELETE r
                    """,
                    tenant_id=self.tenant_id,
                    id=edge_id,
                ).consume()
                return existing_duplicate

            session.run(
                """
                MATCH (old_source:MemoryNode {tenant_id: $tenant_id})-[r:MEMORY_EDGE {tenant_id: $tenant_id, id: $id}]->(old_target:MemoryNode {tenant_id: $tenant_id})
                MATCH (new_source:MemoryNode {tenant_id: $tenant_id, id: $source_id})
                MATCH (new_target:MemoryNode {tenant_id: $tenant_id, id: $target_id})
                CREATE (new_source)-[:MEMORY_EDGE {
                    id: $id,
                    tenant_id: $tenant_id,
                    relationship: $relationship,
                    weight: $weight,
                    metadata: $metadata,
                    created_at: $created_at
                }]->(new_target)
                DELETE r
                """,
                id=updated_edge.id,
                tenant_id=self.tenant_id,
                source_id=updated_edge.source_id,
                target_id=updated_edge.target_id,
                relationship=updated_edge.relationship,
                weight=updated_edge.weight,
                metadata=_encode_metadata(updated_edge.metadata),
                created_at=updated_edge.created_at.isoformat(),
            ).consume()
            return updated_edge

    def delete_edge(self, *, edge_id: str) -> Edge:
        with self._lock, self._session() as session:
            existing = session.run(
                """
                MATCH (source:MemoryNode {tenant_id: $tenant_id})-[r:MEMORY_EDGE {tenant_id: $tenant_id, id: $id}]->(target:MemoryNode {tenant_id: $tenant_id})
                RETURN r.id AS id, source.id AS source_id, target.id AS target_id,
                       r.relationship AS relationship, r.weight AS weight,
                       r.metadata AS metadata, r.created_at AS created_at
                LIMIT 1
                """,
                tenant_id=self.tenant_id,
                id=edge_id,
            ).single()
            if existing is None:
                raise ValueError(f"Edge not found: {edge_id}")
            edge = Edge(
                id=existing["id"],
                tenant_id=self.tenant_id,
                source_id=existing["source_id"],
                target_id=existing["target_id"],
                relationship=existing["relationship"],
                weight=float(existing["weight"]),
                metadata=_decode_metadata(existing["metadata"]),
                created_at=_parse_datetime(existing["created_at"]),
            )
            session.run(
                """
                MATCH (:MemoryNode {tenant_id: $tenant_id})-[r:MEMORY_EDGE {tenant_id: $tenant_id, id: $id}]->(:MemoryNode {tenant_id: $tenant_id})
                DELETE r
                """,
                tenant_id=self.tenant_id,
                id=edge_id,
            ).consume()
            return edge

    def delete_node(self, *, node_id: str) -> Node:
        with self._lock, self._session() as session:
            node = self._fetch_node(session, node_id)
            if node is None:
                raise ValueError(f"Node not found: {node_id}")
            session.run(
                """
                MATCH (n:MemoryNode {tenant_id: $tenant_id, id: $id})
                DETACH DELETE n
                """,
                tenant_id=self.tenant_id,
                id=node_id,
            ).consume()
            return node

    def resolve_conflict(
        self,
        *,
        edge_id: str,
        resolution_note: str = "",
    ) -> ConflictEntry:
        with self._lock, self._session() as session:
            record = session.run(
                """
                MATCH (source:MemoryNode {tenant_id: $tenant_id})-[r:MEMORY_EDGE {tenant_id: $tenant_id, id: $edge_id}]->(target:MemoryNode {tenant_id: $tenant_id})
                RETURN r.id AS id, source.id AS source_id, target.id AS target_id,
                       r.relationship AS relationship, r.weight AS weight,
                       r.metadata AS metadata, r.created_at AS created_at
                LIMIT 1
                """,
                tenant_id=self.tenant_id,
                edge_id=edge_id,
            ).single()
            if record is None:
                raise ValueError(f"Conflict edge not found: {edge_id}")
            edge = Edge(
                id=record["id"],
                tenant_id=self.tenant_id,
                source_id=record["source_id"],
                target_id=record["target_id"],
                relationship=record["relationship"],
                weight=float(record["weight"]),
                metadata=_decode_metadata(record["metadata"]),
                created_at=_parse_datetime(record["created_at"]),
            )
            if edge.relationship not in {RelationType.CONTRADICTS.value, RelationType.UPDATES.value}:
                raise ValueError("Only contradicts or updates edges can be resolved.")

            metadata = dict(edge.metadata)
            metadata["resolved"] = True
            metadata["resolved_at"] = utc_now().isoformat()
            if resolution_note.strip():
                metadata["resolution_note"] = resolution_note.strip()

            session.run(
                """
                MATCH ()-[r:MEMORY_EDGE {tenant_id: $tenant_id, id: $edge_id}]->()
                SET r.metadata = $metadata
                """,
                tenant_id=self.tenant_id,
                edge_id=edge_id,
                metadata=_encode_metadata(metadata),
            ).consume()
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
            entries = self._build_conflict_entries(
                session,
                edges=[updated_edge],
                include_resolved=True,
                limit=1,
            )
        if not entries:
            raise ValueError(f"Resolved conflict could not be loaded: {edge_id}")
        return entries[0]

    def _find_duplicate_node(
        self,
        *,
        existing_nodes: list[Node],
        node: Node,
        embedding: np.ndarray,
    ) -> tuple[Node, str, float | None] | None:
        normalized_label = normalize_text(node.label)
        normalized_content = normalize_text(node.content)
        type_threshold = type_aware_dedup_threshold(
            node.node_type,
            default=self.dedup_similarity_threshold,
        )
        best_match: tuple[Node, float] | None = None

        for existing_node in existing_nodes:
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

            node_entity = extract_choice_entity(node.content)
            existing_entity = extract_choice_entity(existing_node.content)
            if (
                node_entity is not None
                and existing_entity is not None
                and node_entity[1] == existing_entity[1]
                and node_entity[0] != existing_entity[0]
                and not describes_rejected_or_limited_option(node.content)
                and not describes_rejected_or_limited_option(existing_node.content)
            ):
                continue
            if contains_conflicting_numbers(node.content, existing_node.content) and (
                node_entity is None or existing_entity is None or node_entity[0] == existing_entity[0]
            ):
                continue
            if contains_conflicting_months(node.content, existing_node.content):
                continue

            if normalized_content == existing_content:
                return existing_node, "exact_content", 1.0
            if len(normalized_content) >= 10 and len(existing_content) >= 10:
                if normalized_content in existing_content or existing_content in normalized_content:
                    return existing_node, "content_substring", 0.98

            existing_embedding = self.embedding_model.embed(existing_node.content)
            similarity = self.embedding_model.cosine_similarity(embedding, existing_embedding)
            label_score = label_similarity(node.label, existing_node.label)
            acronym_match = is_acronym_match(node.label, existing_node.label)
            if normalized_label == existing_label and similarity >= self.dedup_same_label_threshold:
                return existing_node, "same_label_high_similarity", similarity
            if acronym_match and similarity >= max(self.dedup_same_label_threshold - 0.25, 0.55):
                return existing_node, "acronym_entity_match", similarity
            if label_score >= 0.92 and similarity >= max(self.dedup_same_label_threshold - 0.2, 0.6):
                return existing_node, "label_entity_match", similarity
            if (
                node_entity is not None
                and existing_entity is not None
                and node_entity[0] == existing_entity[0]
                and similarity >= 0.60
            ):
                return existing_node, "same_entity_merge", similarity

            jaccard = content_token_jaccard(node.content, existing_node.content)
            boosted_threshold = max(type_threshold - 0.05, 0.70)
            if jaccard >= 0.35 and similarity >= boosted_threshold:
                return existing_node, "jaccard_boosted_similarity", similarity
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

            if similarity >= self.dedup_similarity_threshold:
                if best_match is None or similarity > best_match[1]:
                    best_match = (existing_node, similarity)

        if best_match is None:
            return None
        return best_match[0], "high_similarity", best_match[1]

    def _merge_duplicate_node(self, session: Any, *, existing_node: Node, incoming_node: Node) -> Node:
        merged_tags = list(dict.fromkeys([*existing_node.tags, *incoming_node.tags]))
        updated_source_prompt = existing_node.source_prompt or incoming_node.source_prompt
        merged_evidence = merge_evidence_records(existing_node.evidence_records, incoming_node.evidence_records)
        merged_valid_from, merged_valid_to = merge_validity_windows(
            existing_node.valid_from,
            incoming_node.valid_from,
            existing_node.valid_to,
            incoming_node.valid_to,
        )
        updated_at = utc_now()
        session.run(
            """
            MATCH (n:MemoryNode {id: $id})
            WHERE n.tenant_id = $tenant_id
            SET n.tags = $tags,
                n.source_prompt = $source_prompt,
                n.evidence_records = $evidence_records,
                n.valid_from = $valid_from,
                n.valid_to = $valid_to,
                n.updated_at = $updated_at
            """,
            id=existing_node.id,
            tenant_id=self.tenant_id,
            tags=merged_tags,
            source_prompt=updated_source_prompt,
            evidence_records=_encode_evidence_records(merged_evidence),
            valid_from=merged_valid_from.isoformat() if merged_valid_from is not None else None,
            valid_to=merged_valid_to.isoformat() if merged_valid_to is not None else None,
            updated_at=updated_at.isoformat(),
        ).consume()
        return Node(
            id=existing_node.id,
            tenant_id=existing_node.tenant_id,
            agent_id=existing_node.agent_id,
            project=existing_node.project,
            session_id=existing_node.session_id,
            label=existing_node.label,
            content=existing_node.content,
            node_type=existing_node.node_type,
            tags=merged_tags,
            source_prompt=updated_source_prompt,
            evidence_records=merged_evidence,
            valid_from=merged_valid_from,
            valid_to=merged_valid_to,
            created_at=existing_node.created_at,
            updated_at=updated_at,
            access_count=existing_node.access_count,
        )

    def _register_conflicts(self, session: Any, node: Node) -> list[ConflictRecord]:
        if node.node_type not in {NodeType.PREFERENCE, NodeType.DECISION}:
            return []
        existing_nodes = [
            self._node_from_props(record["n"])
            for record in session.run(
                """
                MATCH (n:MemoryNode {tenant_id: $tenant_id})
                WHERE n.id <> $node_id
                RETURN n
                """,
                tenant_id=self.tenant_id,
                node_id=node.id,
            )
        ]
        conflicts: list[ConflictRecord] = []
        for existing_node in existing_nodes:
            reason = detect_conflict_reason(existing_node, node)
            if reason is None:
                continue
            if (
                self._find_existing_edge(
                    session,
                    source_id=node.id,
                    target_id=existing_node.id,
                    relationship=RelationType.CONTRADICTS,
                )
                is None
            ):
                edge = Edge(
                    tenant_id=self.tenant_id,
                    source_id=node.id,
                    target_id=existing_node.id,
                    relationship=RelationType.CONTRADICTS,
                    metadata={"origin": "auto-conflict", "reason": reason},
                )
                session.run(
                    """
                    MATCH (source:MemoryNode {tenant_id: $tenant_id, id: $source_id})
                    MATCH (target:MemoryNode {tenant_id: $tenant_id, id: $target_id})
                    CREATE (source)-[:MEMORY_EDGE {
                        id: $id,
                        tenant_id: $tenant_id,
                        relationship: $relationship,
                        weight: $weight,
                        metadata: $metadata,
                        created_at: $created_at
                    }]->(target)
                    """,
                    id=edge.id,
                    tenant_id=self.tenant_id,
                    source_id=edge.source_id,
                    target_id=edge.target_id,
                    relationship=edge.relationship,
                    weight=edge.weight,
                    metadata=_encode_metadata(edge.metadata),
                    created_at=edge.created_at.isoformat(),
                ).consume()
            conflicts.append(
                ConflictRecord(
                    other_node_id=existing_node.id,
                    other_node_label=existing_node.label,
                    reason=reason,
                )
            )
        return conflicts

    def _find_existing_edge(
        self,
        session: Any,
        *,
        source_id: str,
        target_id: str,
        relationship: str | RelationType,
    ) -> Edge | None:
        record = session.run(
            """
            MATCH (source:MemoryNode {tenant_id: $tenant_id, id: $source_id})-[r:MEMORY_EDGE {tenant_id: $tenant_id, relationship: $relationship}]->(target:MemoryNode {tenant_id: $tenant_id, id: $target_id})
            RETURN r.id AS id, source.id AS source_id, target.id AS target_id,
                   r.relationship AS relationship, r.weight AS weight, r.metadata AS metadata, r.created_at AS created_at
            LIMIT 1
            """,
            tenant_id=self.tenant_id,
            source_id=source_id,
            target_id=target_id,
            relationship=normalize_relationship(relationship),
        ).single()
        if record is None:
            return None
        return Edge(
            id=record["id"],
            tenant_id=self.tenant_id,
            source_id=record["source_id"],
            target_id=record["target_id"],
            relationship=record["relationship"],
            weight=float(record["weight"]),
            metadata=_decode_metadata(record["metadata"]),
            created_at=_parse_datetime(record["created_at"]),
        )

    def _delete_edge_record(
        self,
        *,
        source_id: str,
        target_id: str,
        relationship: str,
    ) -> bool:
        with self._lock, self._session() as session:
            summary = session.run(
                """
                MATCH (source:MemoryNode {tenant_id: $tenant_id, id: $source_id})-[r:MEMORY_EDGE {tenant_id: $tenant_id, relationship: $relationship}]->(target:MemoryNode {tenant_id: $tenant_id, id: $target_id})
                DELETE r
                """,
                tenant_id=self.tenant_id,
                source_id=source_id,
                target_id=target_id,
                relationship=normalize_relationship(relationship),
            ).consume()
        return int(summary.counters.relationships_deleted or 0) > 0

    def _insert_snapshot_node(self, session: Any, raw_node: dict[str, Any]) -> None:
        embedding_bytes = raw_node.get("embedding")
        raw = decode_embedding_blob(embedding_bytes) if isinstance(embedding_bytes, bytes) else None
        if raw is not None and len(raw) % np.dtype(np.float32).itemsize == 0:
            embedding = np.frombuffer(raw, dtype=np.float32).astype(np.float32).tolist()
        else:
            embedding = self.embedding_model.embed(raw_node["content"]).astype(np.float32).tolist()
        session.run(
            """
            CREATE (n:MemoryNode {
                id: $id, tenant_id: $tenant_id, agent_id: $agent_id, project: $project, session_id: $session_id,
                label: $label, content: $content, node_type: $node_type,
                tags: $tags, embedding: $embedding, source_prompt: $source_prompt,
                evidence_records: $evidence_records, valid_from: $valid_from, valid_to: $valid_to,
                created_at: $created_at, updated_at: $updated_at, access_count: $access_count
            })
            """,
            id=raw_node["id"],
            tenant_id=self.tenant_id,
            agent_id=raw_node.get("agent_id", ""),
            project=raw_node.get("project", ""),
            session_id=raw_node.get("session_id", ""),
            label=raw_node["label"],
            content=raw_node["content"],
            node_type=raw_node["node_type"],
            tags=raw_node.get("tags", []),
            embedding=embedding,
            source_prompt=raw_node.get("source_prompt", ""),
            evidence_records=_encode_evidence_records(_decode_evidence_records(raw_node.get("evidence_records"))),
            valid_from=raw_node.get("valid_from"),
            valid_to=raw_node.get("valid_to"),
            created_at=raw_node["created_at"],
            updated_at=raw_node["updated_at"],
            access_count=int(raw_node.get("access_count", 0)),
        ).consume()

    def _update_snapshot_node(self, session: Any, raw_node: dict[str, Any]) -> None:
        embedding_bytes = raw_node.get("embedding")
        raw = decode_embedding_blob(embedding_bytes) if isinstance(embedding_bytes, bytes) else None
        if raw is not None and len(raw) % np.dtype(np.float32).itemsize == 0:
            embedding = np.frombuffer(raw, dtype=np.float32).astype(np.float32).tolist()
        else:
            embedding = self.embedding_model.embed(raw_node["content"]).astype(np.float32).tolist()
        session.run(
            """
            MATCH (n:MemoryNode {tenant_id: $existing_tenant_id, id: $id})
            SET n.tenant_id = $tenant_id,
                n.agent_id = $agent_id,
                n.project = $project,
                n.session_id = $session_id,
                n.label = $label,
                n.content = $content,
                n.node_type = $node_type,
                n.tags = $tags,
                n.embedding = $embedding,
                n.source_prompt = $source_prompt,
                n.evidence_records = $evidence_records,
                n.valid_from = $valid_from,
                n.valid_to = $valid_to,
                n.created_at = $created_at,
                n.updated_at = $updated_at,
                n.access_count = $access_count
            """,
            id=raw_node["id"],
            existing_tenant_id=self.tenant_id,
            tenant_id=self.tenant_id,
            agent_id=raw_node.get("agent_id", ""),
            project=raw_node.get("project", ""),
            session_id=raw_node.get("session_id", ""),
            label=raw_node["label"],
            content=raw_node["content"],
            node_type=raw_node["node_type"],
            tags=raw_node.get("tags", []),
            embedding=embedding,
            source_prompt=raw_node.get("source_prompt", ""),
            evidence_records=_encode_evidence_records(_decode_evidence_records(raw_node.get("evidence_records"))),
            valid_from=raw_node.get("valid_from"),
            valid_to=raw_node.get("valid_to"),
            created_at=raw_node["created_at"],
            updated_at=raw_node["updated_at"],
            access_count=int(raw_node.get("access_count", 0)),
        ).consume()

    def _insert_snapshot_edge(self, session: Any, raw_edge: dict[str, Any]) -> None:
        self._require_node(session, raw_edge["source_id"])
        self._require_node(session, raw_edge["target_id"])
        session.run(
            """
            MATCH (source:MemoryNode {tenant_id: $tenant_id, id: $source_id})
            MATCH (target:MemoryNode {tenant_id: $tenant_id, id: $target_id})
            CREATE (source)-[:MEMORY_EDGE {
                id: $id, tenant_id: $tenant_id, relationship: $relationship, weight: $weight,
                metadata: $metadata, created_at: $created_at
            }]->(target)
            """,
            id=raw_edge["id"],
            tenant_id=self.tenant_id,
            source_id=raw_edge["source_id"],
            target_id=raw_edge["target_id"],
            relationship=raw_edge["relationship"],
            weight=float(raw_edge.get("weight", 1.0)),
            metadata=_encode_metadata(raw_edge.get("metadata")),
            created_at=raw_edge["created_at"],
        ).consume()

    def _update_snapshot_edge(self, session: Any, raw_edge: dict[str, Any]) -> None:
        self._require_node(session, raw_edge["source_id"])
        self._require_node(session, raw_edge["target_id"])
        session.run(
            """
            MATCH ()-[r:MEMORY_EDGE {tenant_id: $tenant_id, id: $id}]->()
            DELETE r
            """,
            tenant_id=self.tenant_id,
            id=raw_edge["id"],
        ).consume()
        self._insert_snapshot_edge(session, raw_edge)
