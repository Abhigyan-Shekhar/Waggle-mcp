"""Import bridge for Graphify (safishamsi/graphify) knowledge graphs.

Graphify turns a codebase into a queryable knowledge graph stored as
``graph.json``.  This module reads that file and imports its nodes and edges
into a Waggle :class:`~waggle.graph.MemoryGraph`, so an assistant can query both
code structure (from Graphify) and conversation memory (from Waggle) in one
graph.

Graphify's exact field names have shifted across releases, so the reader is
deliberately tolerant: it accepts several common aliases for each field and
falls back sensibly when one is missing.

Type mapping
------------
Graphify node kind          -> Waggle NodeType
  function/method/class/        ENTITY
  module/file/variable
  doc/comment/concept/          CONCEPT
  design_rationale
  (anything else)               NOTE

Graphify edge kind          -> Waggle RelationType
  calls/imports/inherits/       DEPENDS_ON
  uses/depends_on
  documents/describes/          DERIVED_FROM
  derived_from
  contains/part_of/member_of    PART_OF
  (anything else)               RELATES_TO

Graphify confidence tag     -> Waggle edge confidence
  EXTRACTED                     explicit
  INFERRED                      inferred
  AMBIGUOUS                     weak
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from waggle.models import ImportResult, NodeType, RelationType

if TYPE_CHECKING:
    from waggle.graph import MemoryGraph


_ENTITY_KINDS = {"function", "method", "class", "module", "file", "variable", "interface", "struct", "enum"}
_CONCEPT_KINDS = {"doc", "documentation", "concept", "comment", "design_rationale", "rationale", "note_concept"}

_DEPENDS_KINDS = {"calls", "imports", "inherits", "uses", "depends_on", "depends", "extends", "implements"}
_DERIVED_KINDS = {"documents", "describes", "derived_from", "derived", "annotates"}
_PART_OF_KINDS = {"contains", "part_of", "member_of", "defines", "declares"}

_CONFIDENCE_MAP = {
    "extracted": "explicit",
    "inferred": "inferred",
    "ambiguous": "weak",
}


def _first(mapping: dict[str, Any], *keys: str, default: Any = "") -> Any:
    """Return the first present, non-empty value among ``keys``."""
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return default


def _map_node_type(raw_kind: str) -> NodeType:
    kind = str(raw_kind).strip().lower()
    if kind in _ENTITY_KINDS:
        return NodeType.ENTITY
    if kind in _CONCEPT_KINDS:
        return NodeType.CONCEPT
    return NodeType.NOTE


def _map_relationship(raw_kind: str) -> RelationType:
    kind = str(raw_kind).strip().lower()
    if kind in _DEPENDS_KINDS:
        return RelationType.DEPENDS_ON
    if kind in _DERIVED_KINDS:
        return RelationType.DERIVED_FROM
    if kind in _PART_OF_KINDS:
        return RelationType.PART_OF
    return RelationType.RELATES_TO


def _map_confidence(raw_confidence: Any) -> str:
    return _CONFIDENCE_MAP.get(str(raw_confidence).strip().lower(), "inferred")


def parse_graphify_document(raw: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Extract normalized node and edge lists from a parsed Graphify document.

    Returns ``(nodes, edges)`` where each entry uses Waggle-friendly keys.
    Nodes without a usable id or label are skipped. Edges referencing unknown
    nodes are filtered out by the caller during import.
    """
    raw_nodes = raw.get("nodes") or raw.get("entities") or []
    raw_edges = raw.get("edges") or raw.get("relationships") or raw.get("links") or []

    nodes: list[dict[str, Any]] = []
    for item in raw_nodes:
        if not isinstance(item, dict):
            continue
        node_id = str(_first(item, "id", "node_id", "key")).strip()
        label = str(_first(item, "name", "label", "title", default=node_id)).strip()
        if not node_id or not label:
            continue
        raw_kind = str(_first(item, "type", "node_type", "kind", "category", default="note"))
        content = str(
            _first(item, "content", "description", "summary", "signature", "docstring", default=label)
        ).strip()
        nodes.append(
            {
                "graphify_id": node_id,
                "label": label[:200],
                "content": content or label,
                "node_type": _map_node_type(raw_kind),
                "raw_kind": raw_kind,
                "file": str(_first(item, "file", "path", "filepath", "location")),
            }
        )

    edges: list[dict[str, Any]] = []
    for item in raw_edges:
        if not isinstance(item, dict):
            continue
        source = str(_first(item, "source", "source_id", "from", "src")).strip()
        target = str(_first(item, "target", "target_id", "to", "dst")).strip()
        if not source or not target:
            continue
        raw_kind = str(_first(item, "type", "relationship", "label", "kind", default="relates_to"))
        edges.append(
            {
                "source": source,
                "target": target,
                "relationship": _map_relationship(raw_kind),
                "raw_kind": raw_kind,
                "confidence": _map_confidence(_first(item, "confidence", "tag", default="inferred")),
            }
        )

    return nodes, edges


def import_graphify_json(
    graph: MemoryGraph,
    input_path: str | Path,
    *,
    project: str = "",
    agent_id: str = "",
    session_id: str = "",
) -> ImportResult:
    """Import a Graphify ``graph.json`` into a Waggle MemoryGraph.

    Code entities become ENTITY nodes, docs/rationale become CONCEPT nodes, and
    relationships are mapped to Waggle relation types with confidence carried
    over from Graphify's EXTRACTED/INFERRED/AMBIGUOUS tags.  Imported items are
    tagged ``metadata["graphify_source"] = True`` for provenance.  Duplicate
    nodes (by Waggle's own dedup) are reused rather than re-created.

    Edges whose endpoints did not import (e.g. dangling references in the source
    document) are skipped silently.
    """
    path = Path(input_path).expanduser()
    raw = json.loads(path.read_text(encoding="utf-8"))
    nodes, edges = parse_graphify_document(raw)

    # Map Graphify node id -> resolved Waggle node id (may differ after dedup)
    id_map: dict[str, str] = {}
    nodes_created = 0
    nodes_reused = 0

    for node in nodes:
        metadata = {
            "graphify_source": True,
            "graphify_kind": node["raw_kind"],
        }
        if node["file"]:
            metadata["graphify_file"] = node["file"]
        result = graph.add_node(
            label=node["label"],
            content=node["content"],
            node_type=node["node_type"],
            tags=["graphify"],
            project=project,
            agent_id=agent_id,
            session_id=session_id,
            metadata=metadata,
        )
        id_map[node["graphify_id"]] = result.node.id
        if result.created:
            nodes_created += 1
        else:
            nodes_reused += 1

    edges_created = 0
    for edge in edges:
        source_id = id_map.get(edge["source"])
        target_id = id_map.get(edge["target"])
        if not source_id or not target_id or source_id == target_id:
            continue
        graph.add_edge(
            source_id=source_id,
            target_id=target_id,
            relationship=edge["relationship"],
            confidence=edge["confidence"],
            metadata={"graphify_source": True, "graphify_kind": edge["raw_kind"]},
        )
        edges_created += 1

    return ImportResult(
        input_path=str(path),
        tenant_id=graph.tenant_id,
        nodes_created=nodes_created,
        nodes_updated=nodes_reused,
        edges_created=edges_created,
    )
