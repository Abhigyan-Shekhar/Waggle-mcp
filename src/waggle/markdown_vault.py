from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from waggle.models import Edge, EvidenceRecord, Node, normalize_relationship

_FRONTMATTER_DELIM = "---"
_RELATION_RE = re.compile(
    r"^\s*-\s*(?P<deleted>~~)?\[\[(?P<relationship>[^:\]]+)::(?P<label>[^\]|]+)(?:\|(?P<alias>[^\]]+))?\]\](?P=deleted)?(?:\s*<!--\s*node_id:(?P<node_id>[a-f0-9-]+)\s*-->)?\s*$",
    re.IGNORECASE,
)
_SECTION_HEADING_RE = re.compile(r"^## (?P<title>Content|Evidence|Relations)\s*$", re.MULTILINE)


@dataclass
class VaultRelationEdit:
    relationship: str
    target_label: str
    target_node_id: str = ""
    deleted: bool = False


@dataclass
class VaultDocument:
    path: Path
    frontmatter: dict[str, Any]
    label: str
    content: str
    relations: list[VaultRelationEdit] = field(default_factory=list)
    evidence_lines: list[str] = field(default_factory=list)


def slugify(value: str) -> str:
    lowered = value.strip().lower()
    lowered = re.sub(r"[^a-z0-9]+", "-", lowered)
    lowered = re.sub(r"-{2,}", "-", lowered).strip("-")
    return lowered or "node"


def vault_filename(node: Node) -> str:
    return f"{slugify(node.label)}--{node.id}.md"


def render_frontmatter(payload: dict[str, Any]) -> str:
    lines = [_FRONTMATTER_DELIM]
    for key, value in payload.items():
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=True)}")
    lines.append(_FRONTMATTER_DELIM)
    return "\n".join(lines) + "\n"


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith(f"{_FRONTMATTER_DELIM}\n"):
        return {}, text
    parts = text.split(f"\n{_FRONTMATTER_DELIM}\n", 1)
    if len(parts) != 2:
        return {}, text
    raw_frontmatter = parts[0].splitlines()[1:]
    body = parts[1]
    payload: dict[str, Any] = {}
    for line in raw_frontmatter:
        if ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        value = raw_value.strip()
        try:
            payload[key.strip()] = json.loads(value)
        except json.JSONDecodeError:
            payload[key.strip()] = value.strip('"')
    return payload, body


def render_node_document(node: Node, edges: list[Edge], node_by_id: dict[str, Node]) -> str:
    frontmatter = render_frontmatter(
        {
            "node_id": node.id,
            "tenant_id": node.tenant_id,
            "agent_id": node.agent_id,
            "project": node.project,
            "session_id": node.session_id,
            "node_type": node.node_type.value,
            "tags": node.tags,
            "created_at": node.created_at.isoformat(),
            "updated_at": node.updated_at.isoformat(),
            "valid_from": node.valid_from.isoformat() if node.valid_from else None,
            "valid_to": node.valid_to.isoformat() if node.valid_to else None,
        }
    )
    relation_lines: list[str] = []
    for edge in edges:
        if edge.source_id != node.id:
            continue
        target = node_by_id.get(edge.target_id)
        target_label = target.label if target is not None else edge.target_id
        relation_lines.append(f"- [[{edge.relationship}::{target_label}]] <!-- node_id:{edge.target_id} -->")
    evidence_lines = [
        f"- [{record.source_role or 'unknown'} turn {record.turn_index}] {record.source_text}"
        for record in node.evidence_records[:5]
    ]
    body_lines = [
        frontmatter,
        f"# {node.label}",
        "",
        "## Content",
        node.content,
        "",
        "## Evidence",
        *(evidence_lines or ["- No evidence recorded."]),
        "",
        "## Relations",
        *(relation_lines or ["- No relations recorded."]),
        "",
    ]
    return "\n".join(body_lines)


def parse_node_document(path: Path) -> VaultDocument | None:
    raw_text = path.read_text(encoding="utf-8")
    frontmatter, body = parse_frontmatter(raw_text)
    if not frontmatter.get("node_id"):
        return None
    lines = body.splitlines()
    label = next((line[2:].strip() for line in lines if line.startswith("# ")), path.stem)
    sections: dict[str, str] = {}
    matches = list(_SECTION_HEADING_RE.finditer(body))
    for index, match in enumerate(matches):
        section = match.group("title").lower()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        sections[section] = body[start:end].strip()
    content = sections.get("content", "").strip()
    relations: list[VaultRelationEdit] = []
    for line in sections.get("relations", "").splitlines():
        match = _RELATION_RE.match(line.strip())
        if not match:
            continue
        relations.append(
            VaultRelationEdit(
                relationship=normalize_relationship(match.group("relationship")),
                target_label=(match.group("alias") or match.group("label") or "").strip(),
                target_node_id=(match.group("node_id") or "").strip(),
                deleted=bool(match.group("deleted")),
            )
        )
    evidence_lines = [line.strip() for line in sections.get("evidence", "").splitlines() if line.strip()]
    return VaultDocument(
        path=path,
        frontmatter=frontmatter,
        label=label or path.stem,
        content=content,
        relations=relations,
        evidence_lines=evidence_lines,
    )


def iter_vault_documents(root_path: str | Path) -> list[VaultDocument]:
    root = Path(root_path).expanduser()
    if not root.exists():
        return []
    documents: list[VaultDocument] = []
    for path in sorted(root.rglob("*.md")):
        document = parse_node_document(path)
        if document is not None:
            documents.append(document)
    return documents


def evidence_from_lines(lines: list[str]) -> list[EvidenceRecord]:
    records: list[EvidenceRecord] = []
    for index, line in enumerate(lines):
        text = line.strip().lstrip("- ").strip()
        if not text:
            continue
        records.append(
            EvidenceRecord(
                turn_index=index,
                source_role="vault",
                source_text=text,
            )
        )
    return records
