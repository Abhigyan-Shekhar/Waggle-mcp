#!/usr/bin/env python3
"""Sync repository labels from .github/labels.yml.

This script is intentionally dependency-light so it can run in GitHub Actions
without requiring a separate bootstrap step beyond Python itself.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

API_VERSION = "2022-11-28"
DEFAULT_LABEL_FILE = Path(".github/labels.yml")


@dataclass(frozen=True)
class LabelDefinition:
    name: str
    color: str
    description: str


def parse_simple_yaml(path: Path) -> list[LabelDefinition]:
    """Parse the constrained list-of-maps format used in .github/labels.yml."""
    labels: list[LabelDefinition] = []
    current: dict[str, str] | None = None

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        if line.startswith("- "):
            if current:
                labels.append(_coerce_label(current, path))
            current = {}
            key, value = _parse_key_value(line[2:], path)
            current[key] = value
            continue

        if current is None:
            raise ValueError(f"{path}: expected a list item starting with '- '")

        key, value = _parse_key_value(line, path)
        current[key] = value

    if current:
        labels.append(_coerce_label(current, path))

    seen: set[str] = set()
    for label in labels:
        lowered = label.name.lower()
        if lowered in seen:
            raise ValueError(f"{path}: duplicate label name {label.name!r}")
        seen.add(lowered)

    return labels


def _parse_key_value(line: str, path: Path) -> tuple[str, str]:
    if ":" not in line:
        raise ValueError(f"{path}: expected 'key: value' line, got {line!r}")
    key, value = line.split(":", 1)
    return key.strip(), value.strip().strip("\"'")


def _coerce_label(raw: dict[str, str], path: Path) -> LabelDefinition:
    missing = {"name", "color", "description"} - raw.keys()
    if missing:
        missing_list = ", ".join(sorted(missing))
        raise ValueError(f"{path}: label entry missing required keys: {missing_list}")
    color = raw["color"].lstrip("#")
    if len(color) != 6:
        raise ValueError(f"{path}: label {raw['name']!r} has invalid color {raw['color']!r}")
    return LabelDefinition(name=raw["name"], color=color, description=raw["description"])


class GitHubLabelsClient:
    def __init__(self, repository: str, token: str) -> None:
        self.repository = repository
        self.token = token
        self.base_url = f"https://api.github.com/repos/{repository}/labels"

    def list_labels(self) -> dict[str, dict[str, str]]:
        labels: dict[str, dict[str, str]] = {}
        page = 1

        while True:
            url = f"{self.base_url}?per_page=100&page={page}"
            payload = self._request_json("GET", url)
            if not payload:
                return labels
            for item in payload:
                labels[item["name"].lower()] = item
            page += 1

    def create_label(self, label: LabelDefinition) -> None:
        self._request_json(
            "POST",
            self.base_url,
            {"name": label.name, "color": label.color, "description": label.description},
        )

    def update_label(self, existing_name: str, label: LabelDefinition) -> None:
        encoded = urllib.parse.quote(existing_name, safe="")
        self._request_json(
            "PATCH",
            f"{self.base_url}/{encoded}",
            {"new_name": label.name, "color": label.color, "description": label.description},
        )

    def delete_label(self, existing_name: str) -> None:
        encoded = urllib.parse.quote(existing_name, safe="")
        self._request("DELETE", f"{self.base_url}/{encoded}")

    def _request_json(self, method: str, url: str, body: dict[str, str] | None = None):
        response = self._request(method, url, body)
        if not response:
            return None
        return json.loads(response)

    def _request(self, method: str, url: str, body: dict[str, str] | None = None) -> str:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("Authorization", f"Bearer {self.token}")
        request.add_header("X-GitHub-Api-Version", API_VERSION)
        request.add_header("User-Agent", "waggle-label-sync")
        if data is not None:
            request.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(request) as response:
                return response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GitHub API {method} {url} failed: {exc.code} {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"GitHub API {method} {url} failed: {exc.reason}") from exc


def sync_labels(
    desired: list[LabelDefinition],
    existing: dict[str, dict[str, str]],
    *,
    client: GitHubLabelsClient,
    dry_run: bool,
    delete_missing: bool,
) -> list[str]:
    actions: list[str] = []
    desired_names = {label.name.lower() for label in desired}

    for label in desired:
        current = existing.get(label.name.lower())
        if current is None:
            actions.append(f"create:{label.name}")
            if not dry_run:
                client.create_label(label)
            continue

        current_color = str(current.get("color", "")).lstrip("#").lower()
        current_description = (current.get("description") or "").strip()
        needs_update = (
            current.get("name") != label.name
            or current_color != label.color.lower()
            or current_description != label.description
        )
        if needs_update:
            actions.append(f"update:{label.name}")
            if not dry_run:
                client.update_label(current["name"], label)

    if delete_missing:
        for existing_label in existing.values():
            if existing_label["name"].lower() not in desired_names:
                actions.append(f"delete:{existing_label['name']}")
                if not dry_run:
                    client.delete_label(existing_label["name"])

    return actions


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repository",
        default=os.environ.get("GITHUB_REPOSITORY"),
        help="Repository in owner/name form. Defaults to GITHUB_REPOSITORY.",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("GITHUB_TOKEN"),
        help="GitHub token. Defaults to GITHUB_TOKEN.",
    )
    parser.add_argument(
        "--labels-file",
        type=Path,
        default=DEFAULT_LABEL_FILE,
        help="Path to the label definition file.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned actions without mutating GitHub.",
    )
    parser.add_argument(
        "--delete-missing",
        action="store_true",
        help="Delete repository labels that are not defined in the label file.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.repository:
        print("error: --repository or GITHUB_REPOSITORY is required", file=sys.stderr)
        return 2
    if not args.token:
        print("error: --token or GITHUB_TOKEN is required", file=sys.stderr)
        return 2

    desired = parse_simple_yaml(args.labels_file)
    client = GitHubLabelsClient(args.repository, args.token)
    existing = client.list_labels()
    actions = sync_labels(
        desired,
        existing,
        client=client,
        dry_run=args.dry_run,
        delete_missing=args.delete_missing,
    )

    if actions:
        for action in actions:
            print(action)
    else:
        print("labels already in sync")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
