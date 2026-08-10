from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TypeAlias
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID, uuid5

from pydantic import BaseModel, Field

from waggle.abhi import load_abhi_document
from waggle.errors import ValidationFailure
from waggle.graph import MemoryGraph
from waggle.models import EvidenceRecord, Node, NodeType, RelationType

GitHubEventType = Literal["issue", "pull-request", "discussion", "release", "push", "generic"]
JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]

DEFAULT_MAX_INPUT_BYTES = 1_048_576
MAX_GENERIC_DEPTH = 8
MAX_GENERIC_ITEMS = 100
MAX_GENERIC_SCALAR_CHARS = 4_000
MAX_GENERIC_RENDERED_CHARS = 32_000
MAX_EVENT_TEXT_CHARS = 32_000
MAX_PUSH_COMMITS = 50
GITHUB_EVENT_NAMESPACE = UUID("b22ecba3-b920-5ac6-8f5a-1a1c87ed96a8")

_EVENT_ALIASES: dict[str, GitHubEventType] = {
    "issue": "issue",
    "issues": "issue",
    "pull-request": "pull-request",
    "pull_request": "pull-request",
    "discussion": "discussion",
    "release": "release",
    "push": "push",
    "generic": "generic",
    "workflow_dispatch": "generic",
}

_SENSITIVE_KEY = re.compile(
    r"(?i)(?:password|passwd|pwd|secret|token|authorization|credential|private[_ -]?key|cookie|signing[_ -]?key)"
)
_SENSITIVE_QUERY_KEY = re.compile(r"(?i)(?:token|secret|password|signature|key|authorization|credential)")
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9._-]{8,}\.[A-Za-z0-9._-]{8,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{8,}=*"),
    re.compile(
        r"(?i)\b(?:api[_ -]?key|secret[_ -]?key|access[_ -]?token|password|passwd|pwd)\b"
        r"\s*[:=]\s*['\"]?[^\s'\"]+"
    ),
)
_URL_PATTERN = re.compile(r"https?://[^\s<>]+")


class GitHubRepository(BaseModel):
    name: str
    database_id: str = ""
    url: str = ""


class GitHubActor(BaseModel):
    login: str
    database_id: str = ""
    url: str = ""


class NormalizedGitHubChild(BaseModel):
    key: str
    label: str
    content: str
    source_url: str = ""
    occurred_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class NormalizedGitHubEvent(BaseModel):
    event_type: GitHubEventType
    action: str = ""
    subject_key: str
    label: str
    content: str
    repository: GitHubRepository
    actor: GitHubActor | None = None
    source_url: str = ""
    occurred_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)
    children: list[NormalizedGitHubChild] = Field(default_factory=list)


class GitHubEventIngestionResult(BaseModel):
    status: Literal["ingested"] = "ingested"
    event_type: GitHubEventType
    repository: str
    project: str
    event_node_id: str
    node_ids: list[str] = Field(default_factory=list)
    edge_ids: list[str] = Field(default_factory=list)
    nodes_added: int = 0
    edges_added: int = 0


class GitHubEventCommandResult(BaseModel):
    status: Literal["ingested", "unsupported"]
    event_type: str
    repository: str
    project: str
    context_file: str
    checkpoint_file: str
    nodes_added: int = 0
    edges_added: int = 0
    checkpoint_nodes: int = 0
    checkpoint_edges: int = 0


def _sanitize_url(raw: str) -> str:
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return "[REDACTED]"
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return raw
    host = parsed.hostname
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    retained_query = [(key, value) for key, value in parse_qsl(parsed.query) if not _SENSITIVE_QUERY_KEY.search(key)]
    return urlunsplit((parsed.scheme, host, parsed.path, urlencode(retained_query), parsed.fragment))


def sanitize_text(value: str, *, max_chars: int) -> str:
    sanitized = _URL_PATTERN.sub(lambda match: _sanitize_url(match.group(0)), str(value))
    for pattern in _SECRET_PATTERNS:
        sanitized = pattern.sub("[REDACTED]", sanitized)
    if len(sanitized) > max_chars:
        return f"{sanitized[: max_chars - 1]}…"
    return sanitized


def load_event_payload(path: Path, *, max_input_bytes: int = DEFAULT_MAX_INPUT_BYTES) -> dict[str, Any]:
    if max_input_bytes < 1:
        raise ValidationFailure("max_input_bytes must be positive.")
    try:
        with path.open("rb") as handle:
            raw = handle.read(max_input_bytes + 1)
    except FileNotFoundError as exc:
        raise ValidationFailure(f"GitHub event file not found: {path}") from exc
    except OSError as exc:
        raise ValidationFailure(f"Could not read GitHub event file: {exc}") from exc
    if len(raw) > max_input_bytes:
        raise ValidationFailure(f"GitHub event input exceeds {max_input_bytes} bytes.")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationFailure("GitHub event input must be valid JSON encoded as UTF-8.") from exc
    if not isinstance(payload, dict):
        raise ValidationFailure("GitHub event input must be a JSON object.")
    return payload


def normalize_event_type(explicit: str, github_event_name: str, payload: Mapping[str, Any]) -> str | None:
    normalized_explicit = explicit.strip().lower()
    if normalized_explicit:
        return _EVENT_ALIASES.get(normalized_explicit)
    normalized_environment = github_event_name.strip().lower()
    if normalized_environment:
        return _EVENT_ALIASES.get(normalized_environment)
    if isinstance(payload.get("issue"), Mapping):
        return "issue"
    if isinstance(payload.get("pull_request"), Mapping):
        return "pull-request"
    if isinstance(payload.get("discussion"), Mapping):
        return "discussion"
    if isinstance(payload.get("release"), Mapping):
        return "release"
    if "ref" in payload and "after" in payload:
        return "push"
    return None


def _sanitize_generic_value(value: Any, *, depth: int, item_counter: list[int]) -> JsonValue:
    if depth > MAX_GENERIC_DEPTH:
        raise ValidationFailure(f"Generic event exceeds maximum nesting depth of {MAX_GENERIC_DEPTH}.")
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return sanitize_text(value, max_chars=MAX_GENERIC_SCALAR_CHARS)
    if isinstance(value, Mapping):
        result: dict[str, JsonValue] = {}
        for raw_key in sorted(value, key=str):
            key = str(raw_key)
            if _SENSITIVE_KEY.search(key):
                continue
            item_counter[0] += 1
            if item_counter[0] > MAX_GENERIC_ITEMS:
                raise ValidationFailure(f"Generic event exceeds maximum retained items of {MAX_GENERIC_ITEMS}.")
            result[sanitize_text(key, max_chars=200)] = _sanitize_generic_value(
                value[raw_key], depth=depth + 1, item_counter=item_counter
            )
        return result
    if isinstance(value, list | tuple):
        result_list: list[JsonValue] = []
        for item in value:
            item_counter[0] += 1
            if item_counter[0] > MAX_GENERIC_ITEMS:
                raise ValidationFailure(f"Generic event exceeds maximum retained items of {MAX_GENERIC_ITEMS}.")
            result_list.append(_sanitize_generic_value(item, depth=depth + 1, item_counter=item_counter))
        return result_list
    return sanitize_text(str(value), max_chars=MAX_GENERIC_SCALAR_CHARS)


def sanitize_generic_payload(payload: Mapping[str, Any]) -> dict[str, JsonValue]:
    sanitized = _sanitize_generic_value(payload, depth=0, item_counter=[0])
    if not isinstance(sanitized, dict):  # pragma: no cover - Mapping input guarantees this
        raise ValidationFailure("Generic event input must be an object.")
    rendered = json.dumps(sanitized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(rendered) > MAX_GENERIC_RENDERED_CHARS:
        raise ValidationFailure(f"Generic event exceeds rendered limit of {MAX_GENERIC_RENDERED_CHARS} characters.")
    return sanitized


def _mapping(payload: Mapping[str, Any], key: str, event_type: str) -> Mapping[str, Any]:
    value = payload.get(key)
    if not isinstance(value, Mapping):
        raise ValidationFailure(f"{event_type} event payload must contain an '{key}' object.")
    return value


def _optional_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _optional_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _text(value: Any, *, max_chars: int = MAX_EVENT_TEXT_CHARS) -> str:
    return sanitize_text("" if value is None else str(value), max_chars=max_chars).strip()


def _identifier(value: Any, *, field: str, event_type: str) -> str:
    normalized = _text(value, max_chars=500)
    if not normalized:
        raise ValidationFailure(f"{event_type} event payload is missing {field}.")
    return normalized


def _timestamp(value: Any, *, field: str, event_type: str) -> datetime:
    raw = _identifier(value, field=field, event_type=event_type)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationFailure(f"{event_type} event has invalid {field} timestamp.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _repository(payload: Mapping[str, Any], repository: str) -> GitHubRepository:
    repo = payload.get("repository")
    repo_mapping = repo if isinstance(repo, Mapping) else {}
    name = _identifier(repository or repo_mapping.get("full_name"), field="repository", event_type="GitHub")
    return GitHubRepository(
        name=name,
        database_id=_text(repo_mapping.get("id"), max_chars=100),
        url=_text(repo_mapping.get("html_url"), max_chars=2_000),
    )


def _actor(payload: Mapping[str, Any]) -> GitHubActor | None:
    sender = payload.get("sender")
    if not isinstance(sender, Mapping):
        return None
    login = _text(sender.get("login"), max_chars=200)
    if not login:
        return None
    return GitHubActor(
        login=login,
        database_id=_text(sender.get("id"), max_chars=100),
        url=_text(sender.get("html_url"), max_chars=2_000),
    )


def _content(title: str, body: str, details: list[str]) -> str:
    sections = [title, *[detail for detail in details if detail]]
    if body:
        sections.append(body)
    return "\n\n".join(sections)


def _normalize_issue(payload: Mapping[str, Any], repository: str) -> NormalizedGitHubEvent:
    issue = _mapping(payload, "issue", "issue")
    number = _identifier(issue.get("number"), field="number", event_type="issue")
    title = _identifier(issue.get("title"), field="title", event_type="issue")
    labels = [
        _text(item.get("name"), max_chars=200)
        for item in issue.get("labels", [])
        if isinstance(item, Mapping) and _text(item.get("name"), max_chars=200)
    ]
    occurred_at = _timestamp(issue.get("updated_at") or issue.get("created_at"), field="updated_at", event_type="issue")
    action = _text(payload.get("action"), max_chars=100)
    source_url = _text(issue.get("html_url"), max_chars=2_000)
    return NormalizedGitHubEvent(
        event_type="issue",
        action=action,
        subject_key=f"issue:{number}",
        label=f"Issue #{number}: {title}",
        content=_content(title, _text(issue.get("body")), [f"Action: {action}", f"State: {_text(issue.get('state'))}"]),
        repository=_repository(payload, repository),
        actor=_actor(payload),
        source_url=source_url,
        occurred_at=occurred_at,
        metadata={
            "database_id": _text(issue.get("id"), max_chars=100),
            "number": number,
            "state": _text(issue.get("state"), max_chars=100),
            "labels": labels,
            "source_url": source_url,
        },
    )


def _normalize_pull_request(payload: Mapping[str, Any], repository: str) -> NormalizedGitHubEvent:
    pull = _mapping(payload, "pull_request", "pull-request")
    number = _identifier(pull.get("number") or payload.get("number"), field="number", event_type="pull-request")
    title = _identifier(pull.get("title"), field="title", event_type="pull-request")
    occurred_at = _timestamp(
        pull.get("updated_at") or pull.get("created_at"), field="updated_at", event_type="pull-request"
    )
    action = _text(payload.get("action"), max_chars=100)
    source_url = _text(pull.get("html_url"), max_chars=2_000)
    base = _optional_mapping(pull.get("base"))
    head = _optional_mapping(pull.get("head"))
    return NormalizedGitHubEvent(
        event_type="pull-request",
        action=action,
        subject_key=f"pull-request:{number}",
        label=f"Pull request #{number}: {title}",
        content=_content(title, _text(pull.get("body")), [f"Action: {action}", f"State: {_text(pull.get('state'))}"]),
        repository=_repository(payload, repository),
        actor=_actor(payload),
        source_url=source_url,
        occurred_at=occurred_at,
        metadata={
            "database_id": _text(pull.get("id"), max_chars=100),
            "number": number,
            "state": _text(pull.get("state"), max_chars=100),
            "draft": bool(pull.get("draft", False)),
            "base_ref": _text(base.get("ref"), max_chars=500),
            "base_sha": _text(base.get("sha"), max_chars=100),
            "head_ref": _text(head.get("ref"), max_chars=500),
            "head_sha": _text(head.get("sha"), max_chars=100),
            "source_url": source_url,
        },
    )


def _normalize_discussion(payload: Mapping[str, Any], repository: str) -> NormalizedGitHubEvent:
    discussion = _mapping(payload, "discussion", "discussion")
    number = _identifier(discussion.get("number"), field="number", event_type="discussion")
    title = _identifier(discussion.get("title"), field="title", event_type="discussion")
    occurred_at = _timestamp(
        discussion.get("updated_at") or discussion.get("created_at"), field="updated_at", event_type="discussion"
    )
    category = _optional_mapping(discussion.get("category"))
    action = _text(payload.get("action"), max_chars=100)
    source_url = _text(discussion.get("html_url"), max_chars=2_000)
    return NormalizedGitHubEvent(
        event_type="discussion",
        action=action,
        subject_key=f"discussion:{number}",
        label=f"Discussion #{number}: {title}",
        content=_content(title, _text(discussion.get("body")), [f"Action: {action}", f"Category: {_text(category.get('name'))}"]),
        repository=_repository(payload, repository),
        actor=_actor(payload),
        source_url=source_url,
        occurred_at=occurred_at,
        metadata={
            "database_id": _text(discussion.get("id"), max_chars=100),
            "number": number,
            "state": _text(discussion.get("state"), max_chars=100),
            "category": _text(category.get("name"), max_chars=200),
            "source_url": source_url,
        },
    )


def _normalize_release(payload: Mapping[str, Any], repository: str) -> NormalizedGitHubEvent:
    release = _mapping(payload, "release", "release")
    release_id = _identifier(release.get("id") or release.get("tag_name"), field="id or tag", event_type="release")
    tag = _identifier(release.get("tag_name"), field="tag_name", event_type="release")
    name = _text(release.get("name"), max_chars=1_000) or tag
    occurred_at = _timestamp(
        release.get("published_at") or release.get("created_at"), field="published_at", event_type="release"
    )
    action = _text(payload.get("action"), max_chars=100)
    source_url = _text(release.get("html_url"), max_chars=2_000)
    return NormalizedGitHubEvent(
        event_type="release",
        action=action,
        subject_key=f"release:{release_id}",
        label=f"Release {tag}: {name}",
        content=_content(name, _text(release.get("body")), [f"Action: {action}", f"Tag: {tag}"]),
        repository=_repository(payload, repository),
        actor=_actor(payload),
        source_url=source_url,
        occurred_at=occurred_at,
        metadata={
            "database_id": release_id,
            "tag_name": tag,
            "draft": bool(release.get("draft", False)),
            "prerelease": bool(release.get("prerelease", False)),
            "source_url": source_url,
        },
    )


def _normalize_push(payload: Mapping[str, Any], repository: str) -> NormalizedGitHubEvent:
    ref = _identifier(payload.get("ref"), field="ref", event_type="push")
    after = _identifier(payload.get("after"), field="after", event_type="push")
    head = _optional_mapping(payload.get("head_commit"))
    occurred_at = _timestamp(head.get("timestamp"), field="head_commit.timestamp", event_type="push")
    source_url = _text(payload.get("compare"), max_chars=2_000)
    children: list[NormalizedGitHubChild] = []
    raw_commits = payload.get("commits")
    commits = raw_commits if isinstance(raw_commits, list) else []
    for commit in commits[:MAX_PUSH_COMMITS]:
        if not isinstance(commit, Mapping):
            continue
        commit_id = _identifier(commit.get("id"), field="commit id", event_type="push")
        commit_timestamp = _timestamp(commit.get("timestamp"), field="commit timestamp", event_type="push")
        commit_url = _text(commit.get("url"), max_chars=2_000)
        children.append(
            NormalizedGitHubChild(
                key=f"commit:{commit_id}",
                label=f"Commit {commit_id[:12]}",
                content=_text(commit.get("message")),
                source_url=commit_url,
                occurred_at=commit_timestamp,
                metadata={"sha": commit_id, "source_url": commit_url},
            )
        )
    return NormalizedGitHubEvent(
        event_type="push",
        action="push",
        subject_key=f"push:{ref}:{after}",
        label=f"Push to {ref}",
        content=_content(
            f"Push to {ref}",
            _text(head.get("message")),
            [f"Before: {_text(payload.get('before'), max_chars=100)}", f"After: {after}"],
        ),
        repository=_repository(payload, repository),
        actor=_actor(payload),
        source_url=source_url,
        occurred_at=occurred_at,
        metadata={
            "ref": ref,
            "before": _text(payload.get("before"), max_chars=100),
            "after": after,
            "created": bool(payload.get("created", False)),
            "deleted": bool(payload.get("deleted", False)),
            "forced": bool(payload.get("forced", False)),
            "commit_count": len(commits),
            "commits_retained": len(children),
            "source_url": source_url,
        },
        children=children,
    )


def _normalize_generic(payload: Mapping[str, Any], repository: str) -> NormalizedGitHubEvent:
    sanitized = sanitize_generic_payload(payload)
    rendered = json.dumps(sanitized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    occurred_at = _timestamp(sanitized.get("timestamp"), field="timestamp", event_type="generic")
    title = _text(sanitized.get("title"), max_chars=1_000) or "Generic GitHub workflow event"
    return NormalizedGitHubEvent(
        event_type="generic",
        action="manual",
        subject_key=f"generic:{digest}",
        label=title,
        content=f"{title}\n\n{rendered}",
        repository=GitHubRepository(name=_identifier(repository, field="repository", event_type="generic")),
        occurred_at=occurred_at,
        metadata={"payload_sha256": digest, "sanitized_payload": sanitized},
    )


def normalize_github_event(
    payload: Mapping[str, Any], *, event_type: str, repository: str
) -> NormalizedGitHubEvent:
    normalized_type = _EVENT_ALIASES.get(event_type.strip().lower())
    if normalized_type is None:
        raise ValidationFailure(f"Unsupported GitHub event type: {event_type or 'unknown'}.")
    normalizers = {
        "issue": _normalize_issue,
        "pull-request": _normalize_pull_request,
        "discussion": _normalize_discussion,
        "release": _normalize_release,
        "push": _normalize_push,
        "generic": _normalize_generic,
    }
    return normalizers[normalized_type](payload, repository)


def stable_id(kind: str, *parts: str) -> str:
    normalized = "\x1f".join([kind.strip().lower(), *[str(part).strip() for part in parts]])
    return str(uuid5(GITHUB_EVENT_NAMESPACE, normalized))


def _get_node_or_none(graph: MemoryGraph, node_id: str) -> Node | None:
    try:
        return graph.get_node(node_id)
    except ValueError as exc:
        if str(exc) == f"Node not found: {node_id}":
            return None
        raise


def _upsert_node(
    graph: MemoryGraph,
    *,
    node_id: str,
    label: str,
    content: str,
    node_type: NodeType,
    tags: list[str],
    metadata: dict[str, Any],
    project: str,
    session_id: str,
    occurred_at: datetime,
    evidence_records: list[EvidenceRecord],
) -> tuple[Node, bool]:
    existing = _get_node_or_none(graph, node_id)
    if existing is None:
        stored = graph.add_node(
            node_id=node_id,
            label=label,
            content=content,
            node_type=node_type,
            tags=tags,
            metadata=metadata,
            project=project,
            session_id=session_id,
            evidence_records=evidence_records,
            valid_from=occurred_at,
            created_at=occurred_at,
            updated_at=occurred_at,
        )
        return stored.node, True
    if (
        existing.label != label
        or existing.content != content
        or existing.tags != tags
        or existing.metadata != metadata
        or existing.evidence_records != evidence_records
        or existing.valid_from != occurred_at
    ):
        existing = graph.update_node(
            node_id=node_id,
            label=label,
            content=content,
            tags=tags,
            metadata=metadata,
            evidence_records=evidence_records,
            valid_from=occurred_at,
            updated_at=occurred_at,
        )
    return existing, False


def _evidence(node_id: str, content: str, occurred_at: datetime, session_id: str) -> list[EvidenceRecord]:
    return [
        EvidenceRecord(
            evidence_id=stable_id("evidence", node_id, occurred_at.isoformat()),
            session_id=session_id,
            turn_index=0,
            source_role="github-event",
            source_text=content,
            observed_at=occurred_at,
        )
    ]


def ingest_normalized_event(
    graph: MemoryGraph,
    event: NormalizedGitHubEvent,
    *,
    project: str,
    session_id: str = "",
) -> GitHubEventIngestionResult:
    normalized_project = project.strip()
    if not normalized_project:
        raise ValidationFailure("GitHub event ingestion requires a project.")

    repository_identity = event.repository.database_id or event.repository.name
    repository_node_id = stable_id("repository", repository_identity)
    actor_node_id = (
        stable_id("actor", event.actor.database_id or event.actor.login) if event.actor is not None else None
    )
    event_node_id = stable_id("event", event.repository.name, event.event_type, event.subject_key)
    existing_edge_ids = {str(edge["id"]) for edge in graph.get_graph_snapshot().get("edges", [])}
    node_ids: list[str] = []
    edge_ids: list[str] = []
    nodes_added = 0
    edges_added = 0

    repository_metadata: dict[str, Any] = {
        "repository": event.repository.name,
        "database_id": event.repository.database_id,
        "source_url": event.repository.url,
    }
    repository_node, created = _upsert_node(
        graph,
        node_id=repository_node_id,
        label=event.repository.name,
        content=f"GitHub repository {event.repository.name}",
        node_type=NodeType.ENTITY,
        tags=["github", "github-repository"],
        metadata=repository_metadata,
        project=normalized_project,
        session_id=session_id,
        occurred_at=event.occurred_at,
        evidence_records=_evidence(
            repository_node_id,
            f"GitHub repository {event.repository.name}",
            event.occurred_at,
            session_id,
        ),
    )
    node_ids.append(repository_node.id)
    nodes_added += int(created)

    if event.actor is not None and actor_node_id is not None:
        actor_metadata: dict[str, Any] = {
            "actor": event.actor.login,
            "database_id": event.actor.database_id,
            "source_url": event.actor.url,
        }
        actor_node, created = _upsert_node(
            graph,
            node_id=actor_node_id,
            label=event.actor.login,
            content=f"GitHub actor {event.actor.login}",
            node_type=NodeType.ENTITY,
            tags=["github", "github-actor"],
            metadata=actor_metadata,
            project=normalized_project,
            session_id=session_id,
            occurred_at=event.occurred_at,
            evidence_records=_evidence(
                actor_node_id,
                f"GitHub actor {event.actor.login}",
                event.occurred_at,
                session_id,
            ),
        )
        node_ids.append(actor_node.id)
        nodes_added += int(created)

    child_node_ids: list[str] = []
    for child in sorted(event.children, key=lambda item: (item.occurred_at, item.key)):
        child_node_id = stable_id("event-child", event.repository.name, event.event_type, child.key)
        child_metadata = {
            **child.metadata,
            "repository": event.repository.name,
            "event_type": event.event_type,
            "parent_subject_key": event.subject_key,
            "source_url": child.source_url,
        }
        child_node, created = _upsert_node(
            graph,
            node_id=child_node_id,
            label=child.label,
            content=child.content,
            node_type=NodeType.NOTE,
            tags=["github", "github-event", "github-commit"],
            metadata=child_metadata,
            project=normalized_project,
            session_id=session_id,
            occurred_at=child.occurred_at,
            evidence_records=_evidence(child_node_id, child.content, child.occurred_at, session_id),
        )
        child_node_ids.append(child_node.id)
        node_ids.append(child_node.id)
        nodes_added += int(created)

    event_metadata: dict[str, Any] = {
        **event.metadata,
        "repository": event.repository.name,
        "event_type": event.event_type,
        "action": event.action,
        "actor": event.actor.login if event.actor is not None else "",
        "subject_key": event.subject_key,
        "source_url": event.source_url,
        "occurred_at": event.occurred_at.isoformat(),
    }
    event_node, created = _upsert_node(
        graph,
        node_id=event_node_id,
        label=event.label,
        content=event.content,
        node_type=NodeType.NOTE,
        tags=["github", "github-event", f"github-event:{event.event_type}"],
        metadata=event_metadata,
        project=normalized_project,
        session_id=session_id,
        occurred_at=event.occurred_at,
        evidence_records=_evidence(event_node_id, event.content, event.occurred_at, session_id),
    )
    node_ids.append(event_node.id)
    nodes_added += int(created)

    edge_specs: list[tuple[str, str, RelationType]] = [
        (event_node_id, repository_node_id, RelationType.PART_OF),
    ]
    if actor_node_id is not None:
        edge_specs.append((event_node_id, actor_node_id, RelationType.DERIVED_FROM))
    edge_specs.extend((child_node_id, event_node_id, RelationType.PART_OF) for child_node_id in child_node_ids)
    for source_id, target_id, relationship in edge_specs:
        edge_id = stable_id("edge", source_id, target_id, relationship.value)
        edge = graph.add_edge(
            edge_id=edge_id,
            source_id=source_id,
            target_id=target_id,
            relationship=relationship,
            metadata={"source": "github-event"},
            created_at=event.occurred_at,
        )
        edge_ids.append(edge.id)
        if edge.id not in existing_edge_ids:
            existing_edge_ids.add(edge.id)
            edges_added += 1

    return GitHubEventIngestionResult(
        event_type=event.event_type,
        repository=event.repository.name,
        project=normalized_project,
        event_node_id=event_node_id,
        node_ids=node_ids,
        edge_ids=edge_ids,
        nodes_added=nodes_added,
        edges_added=edges_added,
    )


def validate_export_scope(scope: str, *, project: str, session_id: str, since_date: str) -> str:
    normalized = scope.strip().lower() or "project"
    if normalized not in {"all", "project", "session", "since-date"}:
        raise ValidationFailure("scope must be one of: all, project, session, since-date.")
    if normalized in {"project", "session"} and not project.strip():
        raise ValidationFailure(f"{normalized} scope requires --project.")
    if normalized == "session" and not session_id.strip():
        raise ValidationFailure("session scope requires --session-id.")
    if normalized == "since-date" and not since_date.strip():
        raise ValidationFailure("since-date scope requires --since-date.")
    return normalized


def render_context_handoff(
    document: Mapping[str, Any],
    *,
    status: str,
    event_type: str | None,
    repository: str,
    project: str,
) -> str:
    manifest = _optional_mapping(document.get("manifest"))
    raw_nodes = _optional_list(document.get("nodes"))
    raw_edges = _optional_list(document.get("edges"))
    nodes = [node for node in raw_nodes if isinstance(node, Mapping)]
    edges = [edge for edge in raw_edges if isinstance(edge, Mapping)]
    node_labels = {str(node.get("id", "")): str(node.get("label", "")) for node in nodes}
    lines = [
        "# Waggle GitHub Context Handoff",
        "",
        "Use this checkpoint and summary as portable repository context for downstream AI workflows.",
        "",
        "## Handoff metadata",
        "",
        f"- Status: `{status}`",
        f"- Event type: `{event_type or 'unknown'}`",
        f"- Repository: `{repository}`",
        f"- Project: `{project}`",
        f"- Scope: `{manifest.get('scope', '')}`",
        f"- Nodes: {len(nodes)}",
        f"- Edges: {len(edges)}",
        "",
    ]
    if status == "unsupported":
        lines.extend(
            [
                "## Summary",
                "",
                "This event type is not supported, so no event context was added to memory.",
                "The checkpoint still contains the requested existing Waggle scope.",
                "",
            ]
        )
    if nodes:
        lines.extend(["## Memory", ""])
        for node in sorted(
            nodes,
            key=lambda item: (str(item.get("node_type", "")), str(item.get("label", "")), str(item.get("id", ""))),
        ):
            label = str(node.get("label", "")).strip()
            node_type = str(node.get("node_type", "note")).strip()
            content = str(node.get("content", "")).strip()
            lines.append(f"### {label}")
            lines.append("")
            lines.append(f"Type: `{node_type}`")
            lines.append("")
            lines.append(content)
            lines.append("")
    if edges:
        lines.extend(["## Relationships", ""])
        for edge in sorted(
            edges,
            key=lambda item: (
                str(item.get("source_id", "")),
                str(item.get("target_id", "")),
                str(item.get("relationship", "")),
            ),
        ):
            source = node_labels.get(str(edge.get("source_id", "")), str(edge.get("source_id", "")))
            target = node_labels.get(str(edge.get("target_id", "")), str(edge.get("target_id", "")))
            lines.append(f"- {source} --`{edge.get('relationship', '')}`--> {target}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _temporary_sibling(destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_path = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    os.close(descriptor)
    return Path(raw_path)


def ingest_github_event(
    graph: MemoryGraph,
    *,
    event_path: Path,
    event_type: str,
    github_event_name: str,
    repository: str,
    project: str,
    scope: str,
    session_id: str,
    since_date: str,
    output_context: Path,
    output_checkpoint: Path,
    max_input_bytes: int = DEFAULT_MAX_INPUT_BYTES,
) -> GitHubEventCommandResult:
    normalized_repository = repository.strip()
    normalized_project = project.strip()
    if not normalized_repository:
        raise ValidationFailure("GitHub event ingestion requires --repository.")
    normalized_scope = validate_export_scope(
        scope,
        project=normalized_project,
        session_id=session_id,
        since_date=since_date,
    )
    payload = load_event_payload(event_path, max_input_bytes=max_input_bytes)
    resolved_type = normalize_event_type(event_type, github_event_name, payload)
    if event_type.strip() and resolved_type is None:
        raise ValidationFailure(f"Unsupported explicit GitHub event type: {event_type}.")

    status: Literal["ingested", "unsupported"] = "unsupported"
    nodes_added = 0
    edges_added = 0
    if resolved_type is not None:
        normalized_event = normalize_github_event(
            payload,
            event_type=resolved_type,
            repository=normalized_repository,
        )
        ingestion = ingest_normalized_event(
            graph,
            normalized_event,
            project=normalized_project,
            session_id=session_id,
        )
        status = "ingested"
        nodes_added = ingestion.nodes_added
        edges_added = ingestion.edges_added

    context_destination = output_context.expanduser().resolve()
    checkpoint_destination = output_checkpoint.expanduser().resolve()
    if context_destination == checkpoint_destination:
        raise ValidationFailure("--output-context and --output-checkpoint must be different paths.")
    temporary_context = _temporary_sibling(context_destination)
    temporary_checkpoint = _temporary_sibling(checkpoint_destination)
    try:
        graph.export_abhi(
            output_path=temporary_checkpoint,
            project=normalized_project,
            session_id=session_id,
            scope=normalized_scope,
            since_date=since_date,
            include_embeddings=True,
        )
        document = load_abhi_document(temporary_checkpoint)
        rendered = render_context_handoff(
            document,
            status=status,
            event_type=resolved_type or github_event_name.strip() or None,
            repository=normalized_repository,
            project=normalized_project,
        )
        temporary_context.write_text(rendered, encoding="utf-8")
        temporary_context.chmod(0o600)
        os.replace(temporary_checkpoint, checkpoint_destination)
        os.replace(temporary_context, context_destination)
    finally:
        temporary_checkpoint.unlink(missing_ok=True)
        temporary_context.unlink(missing_ok=True)

    manifest = _optional_mapping(document.get("manifest"))
    counts = _optional_mapping(manifest.get("counts"))
    return GitHubEventCommandResult(
        status=status,
        event_type=resolved_type or github_event_name.strip() or "unknown",
        repository=normalized_repository,
        project=normalized_project,
        context_file=str(context_destination),
        checkpoint_file=str(checkpoint_destination),
        nodes_added=nodes_added,
        edges_added=edges_added,
        checkpoint_nodes=int(counts.get("nodes", 0) or 0),
        checkpoint_edges=int(counts.get("edges", 0) or 0),
    )
