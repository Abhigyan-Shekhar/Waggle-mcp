from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from waggle.models import NodeStoreResult, NodeType

DEFAULT_INCLUDE_PATTERNS = (
    "README.md",
    "README",
    "AGENTS.md",
    "CLAUDE.md",
    "CONTRIBUTING.md",
    "CHANGELOG.md",
    "SECURITY.md",
    "SUPPORT.md",
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "requirements.txt",
    "uv.lock",
)
DEFAULT_MAX_FILE_BYTES = 64 * 1024
DEFAULT_MAX_FILES = 24


class BootstrapGraph(Protocol):
    def add_node(
        self,
        *,
        label: str,
        content: str,
        node_type: NodeType,
        tags: list[str] | None = None,
        source_prompt: str = "",
        agent_id: str = "",
        project: str = "",
        session_id: str = "",
        metadata: dict[str, object] | None = None,
    ) -> NodeStoreResult: ...


@dataclass(frozen=True)
class BootstrapCandidate:
    label: str
    content: str
    path: str
    node_type: NodeType
    tags: list[str]
    metadata: dict[str, object]


@dataclass(frozen=True)
class BootstrapResult:
    root_path: str
    project: str
    candidates: list[BootstrapCandidate]
    nodes_created: int
    nodes_updated: int


def plan_repository_bootstrap(
    root_path: str | Path,
    *,
    include_git: bool = True,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_files: int = DEFAULT_MAX_FILES,
) -> list[BootstrapCandidate]:
    root = Path(root_path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"Bootstrap path is not a directory: {root}")

    candidates: list[BootstrapCandidate] = []
    for path in _iter_bootstrap_files(root, max_files=max_files):
        text = _read_text_file(path, max_file_bytes=max_file_bytes)
        if not text:
            continue
        relative = path.relative_to(root).as_posix()
        candidates.append(
            BootstrapCandidate(
                label=_label_for_file(relative),
                content=_content_for_file(relative, text),
                path=relative,
                node_type=_node_type_for_file(relative),
                tags=["project-bootstrap", "repository", *_tags_for_file(relative)],
                metadata={
                    "source": "waggle_bootstrap",
                    "path": relative,
                    "bytes_read": len(text.encode("utf-8")),
                },
            )
        )

    if include_git:
        git_candidate = _git_summary_candidate(root)
        if git_candidate is not None:
            candidates.append(git_candidate)

    return candidates


def bootstrap_repository(
    graph: BootstrapGraph,
    root_path: str | Path,
    *,
    project: str | None = None,
    agent_id: str = "waggle-bootstrap",
    session_id: str = "repository-bootstrap",
    include_git: bool = True,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_files: int = DEFAULT_MAX_FILES,
    dry_run: bool = False,
) -> BootstrapResult:
    root = Path(root_path).expanduser().resolve()
    project_name = project or root.name
    candidates = plan_repository_bootstrap(
        root,
        include_git=include_git,
        max_file_bytes=max_file_bytes,
        max_files=max_files,
    )
    created = 0
    updated = 0

    if not dry_run:
        for candidate in candidates:
            result = graph.add_node(
                label=candidate.label,
                content=candidate.content,
                node_type=candidate.node_type,
                tags=candidate.tags,
                source_prompt=f"waggle bootstrap {root}",
                agent_id=agent_id,
                project=project_name,
                session_id=session_id,
                metadata=candidate.metadata,
            )
            if result.created:
                created += 1
            else:
                updated += 1

    return BootstrapResult(
        root_path=str(root),
        project=project_name,
        candidates=candidates,
        nodes_created=created,
        nodes_updated=updated,
    )


def _iter_bootstrap_files(root: Path, *, max_files: int) -> list[Path]:
    paths: list[Path] = []
    for pattern in DEFAULT_INCLUDE_PATTERNS:
        direct = root / pattern
        if direct.is_file():
            paths.append(direct)
    docs = root / "docs"
    if docs.is_dir():
        for path in sorted(docs.rglob("*.md")):
            if _is_ignored_path(path):
                continue
            paths.append(path)
            if len(paths) >= max_files:
                break
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.resolve()
        if resolved in seen or not _is_inside(root, resolved):
            continue
        seen.add(resolved)
        unique.append(resolved)
        if len(unique) >= max_files:
            break
    return unique


def _read_text_file(path: Path, *, max_file_bytes: int) -> str:
    try:
        data = path.read_bytes()[:max_file_bytes]
    except OSError:
        return ""
    if b"\x00" in data:
        return ""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return ""
    return text.strip()


def _git_summary_candidate(root: Path) -> BootstrapCandidate | None:
    if not (root / ".git").exists():
        return None
    lines: list[str] = []
    branch = _run_git(root, "branch", "--show-current")
    if branch:
        lines.append(f"Current branch: {branch}")
    commits = _run_git(root, "log", "--oneline", "-5")
    if commits:
        lines.append("Recent commits:")
        lines.extend(f"- {line}" for line in commits.splitlines())
    status = _run_git(root, "status", "--short")
    if status:
        lines.append("Working tree has uncommitted changes.")
    else:
        lines.append("Working tree is clean.")
    if not lines:
        return None
    return BootstrapCandidate(
        label="Repository git summary",
        content="\n".join(lines),
        path=".git",
        node_type=NodeType.NOTE,
        tags=["project-bootstrap", "repository", "git"],
        metadata={"source": "waggle_bootstrap", "path": ".git"},
    )


def _run_git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _label_for_file(relative_path: str) -> str:
    basename = Path(relative_path).name
    if basename.lower().startswith("readme"):
        return "Project README"
    if basename == "AGENTS.md":
        return "Agent instructions"
    if basename == "CLAUDE.md":
        return "Claude instructions"
    if basename == "pyproject.toml":
        return "Python project configuration"
    if basename == "package.json":
        return "Node project configuration"
    if basename == "CHANGELOG.md":
        return "Project changelog"
    return f"Repository file: {relative_path}"


def _content_for_file(relative_path: str, text: str) -> str:
    return f"Source file: {relative_path}\n\n{text}"


def _node_type_for_file(relative_path: str) -> NodeType:
    basename = Path(relative_path).name
    if basename in {"AGENTS.md", "CLAUDE.md"}:
        return NodeType.PREFERENCE
    if basename in {"pyproject.toml", "package.json", "Cargo.toml", "go.mod", "requirements.txt"}:
        return NodeType.FACT
    return NodeType.NOTE


def _tags_for_file(relative_path: str) -> list[str]:
    basename = Path(relative_path).name.lower()
    tags: list[str] = []
    if relative_path.startswith("docs/"):
        tags.append("docs")
    if basename in {"agents.md", "claude.md"}:
        tags.append("agent-instructions")
    if basename in {"pyproject.toml", "package.json", "cargo.toml", "go.mod", "requirements.txt"}:
        tags.append("project-config")
    return tags


def _is_ignored_path(path: Path) -> bool:
    ignored_parts = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", "coverage"}
    return any(part in ignored_parts for part in path.parts)


def _is_inside(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
