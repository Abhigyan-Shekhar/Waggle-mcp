"""Project identity and lightweight repository context for WebMCP.

Projects and repository observations are stored as normal Waggle graph nodes.
This deliberately avoids a second project catalogue or retrieval system while
keeping source-derived observations distinguishable from human-governed memory.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tomllib
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from html import unescape
from itertools import islice
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from waggle.errors import ValidationFailure
from waggle.models import NodeType, RelationType

_REGISTRY_TAG = "webmcp:project"
_OBSERVATION_TAG = "webmcp:repository-observation"
_MAX_FILE_BYTES = 128 * 1024
_MAX_README_CHARS = 1_200
_MAX_COMMITS = 8
_PROJECT_LOCK = RLock()


@dataclass(frozen=True, slots=True)
class ProjectIdentity:
    project_id: str
    project_name: str
    project_root: str
    git_remote: str
    repository: str
    identity_source: str
    identity_key: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def _run_git(root: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def normalize_git_remote(remote: str) -> str:
    """Return a credential-free, transport-independent repository identity."""

    value = str(remote or "").strip()
    if not value:
        return ""
    scp_match = re.fullmatch(r"(?:[^@/]+@)?([^:/]+):(.+)", value)
    if scp_match and "://" not in value:
        host, path = scp_match.groups()
        value = f"https://{host}/{path}"
    elif "://" not in value:
        # A filesystem remote remains a path identity, not a synthetic URL.
        return str(Path(value).expanduser().resolve()).rstrip("/")

    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    port = f":{parsed.port}" if parsed.port else ""
    path = re.sub(r"/+", "/", parsed.path).rstrip("/")
    if path.lower().endswith(".git"):
        path = path[:-4]
    return urlunsplit(("https", f"{host}{port}", path, "", "")).rstrip("/")


def resolve_project_identity(workspace_path: str | Path) -> ProjectIdentity:
    supplied = Path(workspace_path).expanduser()
    if not supplied.exists():
        raise ValidationFailure(f"workspace_path does not exist: {supplied}")
    supplied = supplied.resolve()
    if supplied.is_file():
        supplied = supplied.parent

    git_root_text = _run_git(supplied, "rev-parse", "--show-toplevel")
    git_root = Path(git_root_text).resolve() if git_root_text else None
    project_root = git_root or supplied
    raw_remote = _run_git(project_root, "remote", "get-url", "origin") if git_root else ""
    if git_root and not raw_remote:
        remotes = _run_git(project_root, "remote").splitlines()
        if remotes:
            raw_remote = _run_git(project_root, "remote", "get-url", sorted(remotes)[0])
    if raw_remote and "://" not in raw_remote and ":" not in raw_remote:
        raw_remote = str((project_root / raw_remote).resolve())
    normalized_remote = normalize_git_remote(raw_remote)

    if normalized_remote:
        identity_source = "git_remote"
        identity_value = normalized_remote.lower()
        repository = normalized_remote
        project_name = normalized_remote.rsplit("/", 1)[-1]
    elif git_root is not None:
        identity_source = "git_root"
        identity_value = str(git_root)
        repository = ""
        project_name = git_root.name
    else:
        identity_source = "workspace_path"
        identity_value = str(project_root)
        repository = ""
        project_name = project_root.name

    identity_key = f"{identity_source}:{identity_value}"
    project_id = hashlib.sha256(identity_key.encode("utf-8")).hexdigest()
    return ProjectIdentity(
        project_id=project_id,
        project_name=project_name or "Untitled project",
        project_root=str(project_root),
        git_remote=normalized_remote,
        repository=repository,
        identity_source=identity_source,
        identity_key=identity_key,
    )


def _registry_node_id(graph: Any, project_id: str) -> str:
    tenant_key = hashlib.sha256(str(graph.tenant_id).encode()).hexdigest()[:20]
    return f"webmcp-project-{tenant_key}-{project_id}"


def _metadata(node: Any) -> dict[str, Any]:
    value = node.get("metadata", {}) if isinstance(node, dict) else getattr(node, "metadata", {})
    return value if isinstance(value, dict) else {}


def list_registered_projects(graph: Any) -> list[dict[str, Any]]:
    projects: list[dict[str, Any]] = []
    for node in graph.get_graph_snapshot().get("nodes", []):
        metadata = _metadata(node)
        if metadata.get("webmcp_kind") != "project":
            continue
        identity = metadata.get("identity")
        if isinstance(identity, dict):
            projects.append(dict(identity))
    projects.sort(key=lambda item: (str(item.get("project_name", "")).lower(), str(item.get("project_id", ""))))
    return projects


def get_registered_project(graph: Any, project_id: str) -> dict[str, Any] | None:
    project = str(project_id or "").strip()
    if not project:
        return None
    try:
        node = graph.get_node(_registry_node_id(graph, project))
    except ValueError:
        return None
    identity = _metadata(node).get("identity")
    return dict(identity) if isinstance(identity, dict) else None


def resolve_active_project(graph: Any, project_id: str = "", *, fallback: str = "") -> str:
    supplied = str(project_id or "").strip()
    if supplied:
        return supplied
    if fallback and get_registered_project(graph, fallback):
        return fallback
    projects = list_registered_projects(graph)
    if len(projects) == 1:
        return str(projects[0]["project_id"])
    if not projects:
        raise ValidationFailure("No project is active. Register or open a workspace before using Waggle WebMCP.")
    raise ValidationFailure("More than one project is registered. Open a project or supply project_id explicitly.")


def register_project(graph: Any, workspace_path: str | Path, *, refresh: bool = True) -> dict[str, Any]:
    with _PROJECT_LOCK:
        return _register_project(graph, workspace_path, refresh=refresh)


def _register_project(graph: Any, workspace_path: str | Path, *, refresh: bool) -> dict[str, Any]:
    identity = resolve_project_identity(workspace_path)
    node_id = _registry_node_id(graph, identity.project_id)
    created = False
    try:
        existing = graph.get_node(node_id)
        if _metadata(existing).get("identity") != identity.as_dict():
            graph.update_node(node_id=node_id, metadata={**_metadata(existing), "identity": identity.as_dict()})
    except ValueError:
        graph.add_node(
            node_id=node_id,
            label=identity.project_name,
            content=f"Registered repository project {identity.project_name}.",
            node_type=NodeType.ENTITY,
            tags=[_REGISTRY_TAG, "project"],
            project=identity.project_id,
            metadata={
                "webmcp_kind": "project",
                "authority": "source_observation",
                "source_type": "workspace_registration",
                "identity": identity.as_dict(),
            },
            force_new=True,
        )
        created = True
    refresh_result = refresh_project_context(graph, identity.project_id) if refresh else None
    return {"project": identity.as_dict(), "created": created, "refresh": refresh_result}


def _read_text(path: Path) -> str:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_FILE_BYTES:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _clean_markdown(text: str) -> str:
    # Ignore branding/comments without treating HTML markup as project prose.
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    lines: list[str] = []
    in_fence = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not line or line.startswith(("#", "!", "[!")) or re.fullmatch(r"[-=*]{3,}", line):
            if lines:
                break
            continue
        line = re.sub(r"\[([^]]+)]\([^)]*\)", r"\1", line)
        line = " ".join(re.sub(r"[*_`>]", "", line).split())
        if line:
            lines.append(line)
        if sum(len(item) for item in lines) >= _MAX_README_CHARS:
            break
    return " ".join(lines)[:_MAX_README_CHARS].strip()


def _manifest_context(root: Path) -> tuple[list[str], list[str]]:
    stack: list[str] = []
    commands: list[str] = []
    package_json = _read_text(root / "package.json")
    if package_json:
        try:
            package = json.loads(package_json)
        except json.JSONDecodeError:
            package = {}
        stack.append("JavaScript/TypeScript")
        if isinstance(package, dict):
            for dependency_type in ("dependencies", "devDependencies"):
                dependencies = package.get(dependency_type, {})
                if isinstance(dependencies, dict):
                    stack.extend(f"{name} {str(version)[:60]}" for name, version in sorted(dependencies.items())[:25])
        scripts = package.get("scripts", {}) if isinstance(package, dict) else {}
        if isinstance(scripts, dict):
            commands.extend(f"npm run {name}" for name in sorted(scripts)[:12])
    pyproject = _read_text(root / "pyproject.toml")
    if pyproject:
        stack.append("Python")
        try:
            parsed = tomllib.loads(pyproject)
        except tomllib.TOMLDecodeError:
            parsed = {}
        project = parsed.get("project", {})
        project = project if isinstance(project, dict) else {}
        scripts = project.get("scripts", {})
        dependencies = project.get("dependencies", [])
        if isinstance(dependencies, list):
            stack.extend(str(item)[:100] for item in dependencies[:25])
        if isinstance(scripts, dict):
            commands.extend(sorted(str(name) for name in scripts)[:12])
    requirements = _read_text(root / "requirements.txt")
    if requirements:
        stack.append("Python")
        stack.extend(
            line.strip()[:100]
            for line in requirements.splitlines()[:40]
            if re.match(r"^[a-zA-Z][a-zA-Z0-9_.-]*(?:[<>=!~\[]|$)", line.strip())
        )
    markers = {
        "Cargo.toml": "Rust",
        "go.mod": "Go",
        "pom.xml": "Java/Maven",
        "build.gradle": "Java/Gradle",
        "Gemfile": "Ruby",
        "composer.json": "PHP",
    }
    stack.extend(language for file_name, language in markers.items() if (root / file_name).is_file())
    return list(dict.fromkeys(stack)), list(dict.fromkeys(commands))


def _repository_observations(identity: ProjectIdentity) -> list[dict[str, str]]:
    root = Path(identity.project_root)
    readme_path = next(
        (root / name for name in ("README.md", "README.rst", "README.txt", "README") if (root / name).is_file()), None
    )
    purpose = _clean_markdown(_read_text(readme_path)) if readme_path else ""
    readme_text = _read_text(readme_path) if readme_path else ""
    stack, commands = _manifest_context(root)
    ignored = {".git", ".venv", "venv", "node_modules", "dist", "build", "__pycache__"}
    try:
        components = sorted(
            path.name
            for path in root.iterdir()
            if path.is_dir() and path.name not in ignored and not path.name.startswith(".")
        )[:24]
    except OSError:
        components = []
    deployment_files = [
        name
        for name in (
            "Dockerfile",
            "docker-compose.yml",
            "docker-compose.yaml",
            "compose.yml",
            "render.yaml",
            "Procfile",
        )
        if (root / name).is_file()
    ]
    env_example = next(
        (root / name for name in (".env.example", ".env.sample", "example.env") if (root / name).is_file()), None
    )
    service_keys: list[str] = []
    if env_example:
        for line in _read_text(env_example).splitlines():
            match = re.match(r"\s*(?:export\s+)?([A-Z][A-Z0-9_]{1,80})\s*=", line)
            if match:
                service_keys.append(match.group(1))
    branch = _run_git(root, "branch", "--show-current")
    commit_lines = _run_git(root, "log", f"-{_MAX_COMMITS}", "--pretty=format:%h %s").splitlines()
    documentation = []
    for directory in (root / "docs", root / ".github"):
        if directory.is_dir() and not directory.is_symlink():
            with suppress(OSError):
                documentation.extend(str(path.relative_to(root)) for path in islice(directory.glob("*.md"), 20))

    candidates = [
        ("purpose", "Repository purpose", purpose, str(readme_path.relative_to(root)) if readme_path else ""),
        ("components", "Repository components", ", ".join(components), "top-level directories"),
        ("stack", "Technology stack", ", ".join(stack), "project manifests"),
        ("commands", "Development and test commands", ", ".join(commands), "project manifests"),
        ("deployment", "Deployment shape", ", ".join(deployment_files), "deployment files"),
        (
            "services",
            "Configured external service keys",
            ", ".join(sorted(set(service_keys))),
            str(env_example.relative_to(root)) if env_example else "",
        ),
        ("documentation", "Repository documentation", ", ".join(documentation), "docs and .github"),
        ("git_branch", "Current Git branch", branch, ".git"),
        ("recent_commits", "Recent repository changes", " | ".join(commit_lines), ".git/log"),
    ]
    for key, label, pattern in (
        ("architecture", "Repository architecture", r"architecture|local.first|client.server|microservice"),
        (
            "storage",
            "Repository storage architecture",
            r"sqlite|neo4j|postgres|mysql|redis|persistence|storage backend",
        ),
        ("constraints", "Repository constraints", r"must not|never |required|only supports"),
        ("testing", "Repository testing commands", r"pytest|npm test|cargo test|go test|vitest"),
    ):
        excerpts = [
            f"L{number}: {line.strip()[:240]}"
            for number, line in enumerate(readme_text.splitlines(), 1)
            if re.search(pattern, line, re.IGNORECASE)
        ][:5]
        candidates.append((key, label, "\n".join(excerpts), readme_path.name if readme_path else ""))
    for relative_path in sorted(documentation)[:8]:
        text = _read_text(root / relative_path)
        if text:
            # Bounded documentation observations retain a source fingerprint so
            # edits outside the excerpt still register on the next refresh.
            fingerprint = hashlib.sha256(text.encode()).hexdigest()[:16]
            candidates.append(
                (
                    f"doc:{relative_path}",
                    f"Repository document {relative_path}",
                    f"{_clean_markdown(text) or relative_path}\nSource fingerprint: {fingerprint}",
                    relative_path,
                )
            )
    for relative_path in deployment_files:
        text = _read_text(root / relative_path)
        if text:
            # Track deployment changes without copying credentials or arbitrary
            # environment values that may occur inside deployment definitions.
            candidates.append(
                (
                    f"deployment:{relative_path}",
                    f"Deployment definition {relative_path}",
                    f"{relative_path} source fingerprint: {hashlib.sha256(text.encode()).hexdigest()[:16]}",
                    relative_path,
                )
            )
    return [
        {"key": key, "label": label, "content": content, "provenance": provenance}
        for key, label, content, provenance in candidates
        if content
    ]


def _current_observations(graph: Any, project_id: str) -> dict[str, dict[str, Any]]:
    current: dict[str, dict[str, Any]] = {}
    snapshot = graph.get_graph_snapshot(project=project_id)
    superseded = {
        str(edge.get("target_id"))
        for edge in snapshot.get("edges", [])
        if str(edge.get("relationship")) == RelationType.UPDATES.value
    }
    for node in snapshot.get("nodes", []):
        metadata = _metadata(node)
        key = str(metadata.get("observation_key") or "")
        if metadata.get("webmcp_kind") != "repository_observation" or not key or str(node.get("id")) in superseded:
            continue
        current[key] = node
    return current


def _authority_conflicts(
    snapshot: dict[str, Any],
    *,
    category: str,
    observed_content: str,
) -> list[dict[str, str]]:
    superseded = {
        str(edge.get("target_id"))
        for edge in snapshot.get("edges", [])
        if str(edge.get("relationship")) == RelationType.UPDATES.value
    }
    matches: list[dict[str, str]] = []
    category_token = category.replace("_", " ").lower()
    for node in snapshot.get("nodes", []):
        metadata = _metadata(node)
        if metadata.get("authority") == "source_observation" or str(node.get("id")) in superseded:
            continue
        if node.get("valid_to"):
            continue
        haystack = " ".join(
            [
                str(node.get("label", "")),
                " ".join(str(tag) for tag in node.get("tags", [])),
            ]
        ).lower()
        if category_token not in haystack or str(node.get("content", "")).strip() == observed_content.strip():
            continue
        matches.append(
            {
                "authoritative_memory_id": str(node.get("id", "")),
                "message": (
                    f"Repository {category} may differ from authoritative memory; "
                    "review it through the proposal workflow before changing authority."
                ),
            }
        )
    return matches[:10]


def refresh_project_context(graph: Any, project_id: str) -> dict[str, Any]:
    with _PROJECT_LOCK:
        return _refresh_project_context(graph, project_id)


def _refresh_project_context(graph: Any, project_id: str) -> dict[str, Any]:
    project = get_registered_project(graph, project_id)
    if project is None:
        raise ValidationFailure("project_id is not a registered repository project.")
    identity = ProjectIdentity(**{field: str(project.get(field, "")) for field in ProjectIdentity.__dataclass_fields__})
    previous = _current_observations(graph, identity.project_id)
    authority_snapshot = graph.get_graph_snapshot(project=identity.project_id)
    added: list[str] = []
    changed: list[str] = []
    unchanged: list[str] = []
    conflicts: list[dict[str, str]] = []
    now = datetime.now(UTC)

    observations = _repository_observations(identity)
    present_keys = {item["key"] for item in observations}
    for key, prior in previous.items():
        if key not in present_keys:
            observations.append(
                {
                    "key": key,
                    "label": str(prior["label"]),
                    "content": f"Repository no longer contains the previously observed {key} context.",
                    "provenance": str(_metadata(prior).get("provenance", {}).get("path", "")),
                }
            )

    for observation in observations:
        content_hash = hashlib.sha256(observation["content"].encode("utf-8")).hexdigest()
        # A reverted source is a new observation, not a resurrection of an old
        # node whose validity has already closed. UUIDs also isolate tenants.
        node_id = f"repo-observation-{uuid4()}"
        prior = previous.get(observation["key"])
        if prior and str(_metadata(prior).get("content_sha256")) == content_hash:
            unchanged.append(observation["key"])
            continue
        repository_change = (
            {
                "previous_memory_id": str(prior["id"]),
                "message": f"Repository {observation['key']} changed; authoritative Waggle decisions were not rewritten.",
            }
            if prior
            else None
        )
        authority_conflicts = _authority_conflicts(
            authority_snapshot,
            category=observation["key"],
            observed_content=observation["content"],
        )
        stored = graph.add_node(
            node_id=node_id,
            label=observation["label"],
            content=observation["content"],
            node_type=NodeType.FACT,
            tags=[_OBSERVATION_TAG, f"repository:{observation['key']}"],
            project=identity.project_id,
            valid_from=now,
            metadata={
                "webmcp_kind": "repository_observation",
                "authority": "source_observation",
                "source_type": "repository_scan",
                "observation_key": observation["key"],
                "content_sha256": content_hash,
                "provenance": {"path": observation["provenance"], "project_root": identity.project_root},
                **({"repository_change": repository_change} if repository_change else {}),
                **({"authority_conflicts": authority_conflicts} if authority_conflicts else {}),
            },
            force_new=True,
        ).node
        added.append(stored.id)
        conflicts.extend(
            {
                "category": observation["key"],
                "observed_memory_id": stored.id,
                **conflict,
            }
            for conflict in authority_conflicts
        )
        if prior:
            graph.add_edge(
                source_id=stored.id,
                target_id=str(prior["id"]),
                relationship=RelationType.UPDATES,
                metadata={"source": "refresh_project_context", "observation_key": observation["key"]},
            )
            changed.append(observation["key"])
            conflicts.append(
                {
                    "category": observation["key"],
                    "previous_memory_id": str(prior["id"]),
                    "observed_memory_id": stored.id,
                    "message": str(repository_change["message"]),
                }
            )

    return {
        "project": identity.as_dict(),
        "added_memory_ids": added,
        "changed_categories": changed,
        "unchanged_categories": unchanged,
        "conflicts": conflicts,
        "refreshed_at": now.isoformat(),
    }


def repository_context_for_project(graph: Any, project_id: str) -> dict[str, Any]:
    project = get_registered_project(graph, project_id)
    current = _current_observations(graph, project_id)
    snapshot = graph.get_graph_snapshot(project=project_id)
    observations = []
    for key, node in sorted(current.items()):
        metadata = _metadata(node)
        observations.append(
            {
                "memory_id": str(node.get("id", "")),
                "category": key,
                "label": str(node.get("label", "")),
                "content": str(node.get("content", "")),
                "authority": "source_observation",
                "source": "repository_scan",
                "provenance": metadata.get("provenance", {}),
                "repository_change": metadata.get("repository_change"),
                "authority_conflicts": _authority_conflicts(
                    snapshot, category=key, observed_content=str(node.get("content", ""))
                ),
                "updated_at": node.get("updated_at"),
            }
        )
    return {"project": project, "observations": observations}
