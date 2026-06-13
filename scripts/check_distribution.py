from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
INSTALL_DOCS = [
    "README.md",
    "smithery.md",
    "vscode.md",
    "claude-code.md",
    "claude-desktop.md",
    "codex.md",
    "cursor.md",
    "antigravity.md",
    "generic-mcp.md",
    "troubleshooting.md",
]
CRITICAL_DOCS = [
    ROOT / "README.md",
    ROOT / "docs" / "install" / "README.md",
    ROOT / "docs" / "install" / "smithery.md",
    ROOT / "docs" / "install" / "codex.md",
    ROOT / "docs" / "install" / "cursor.md",
    ROOT / "docs" / "install" / "claude-code.md",
    ROOT / "docs" / "install" / "claude-desktop.md",
    ROOT / "docs" / "install" / "generic-mcp.md",
    ROOT / "docs" / "security.md",
]
EXPECTED_COMMAND = 'waggle-mcp'
EXPECTED_ARGS = '["serve", "--transport", "stdio"]'


def main() -> int:
    failures: list[str] = []

    smithery_path = ROOT / "smithery.yaml"
    if not smithery_path.exists():
        failures.append("Missing smithery.yaml")
    else:
        try:
            smithery_payload = yaml.safe_load(smithery_path.read_text())
        except yaml.YAMLError as exc:
            failures.append(f"smithery.yaml failed to parse: {exc}")
        else:
            start_command = smithery_payload.get("startCommand") if isinstance(smithery_payload, dict) else None
            if not isinstance(start_command, dict) or start_command.get("type") != "stdio":
                failures.append("smithery.yaml does not define startCommand.type=stdio")

    install_dir = ROOT / "docs" / "install"
    for page in INSTALL_DOCS:
        page_path = install_dir / page
        if not page_path.exists():
            failures.append(f"Missing install doc: {page_path.relative_to(ROOT)}")

    docs_to_scan = [ROOT / "README.md", *sorted(install_dir.glob("*.md"))]
    for doc_path in docs_to_scan:
        text = doc_path.read_text()
        if EXPECTED_COMMAND not in text:
            failures.append(f"{doc_path.relative_to(ROOT)} does not mention {EXPECTED_COMMAND}")
        if doc_path.name != "README.md" and "serve --transport stdio" not in text and EXPECTED_ARGS not in text:
            failures.append(f"{doc_path.relative_to(ROOT)} does not document stdio startup")

    package_json_path = ROOT / "apps" / "vscode-extension" / "package.json"
    if not package_json_path.exists():
        failures.append("Missing apps/vscode-extension/package.json")
    else:
        package_json = json.loads(package_json_path.read_text())
        if package_json.get("name") != "waggle-memory":
            failures.append("VS Code extension package.json has unexpected name")

    workflow_path = ROOT / ".github" / "workflows" / "release-binaries.yml"
    if not workflow_path.exists():
        failures.append("Missing .github/workflows/release-binaries.yml")

    claude_extension_manifest = ROOT / "apps" / "mcp" / "claude-desktop-extension" / "manifest.json"
    if not claude_extension_manifest.exists():
        failures.append("Missing apps/mcp/claude-desktop-extension/manifest.json")

    markdown_link_pattern = re.compile(r"\[[^\]]+\]\((?!https?://|mailto:|#)([^)]+)\)")
    for markdown_path in [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]:
        text = markdown_path.read_text()
        for relative_target in markdown_link_pattern.findall(text):
            if relative_target.startswith("/"):
                continue
            target = (markdown_path.parent / relative_target).resolve()
            if not target.exists():
                failures.append(
                    f"Broken local markdown link in {markdown_path.relative_to(ROOT)}: {relative_target}"
                )

    todo_pattern = re.compile(r"\bTODO\b", re.IGNORECASE)
    for doc_path in CRITICAL_DOCS:
        if not doc_path.exists():
            continue
        text = doc_path.read_text()
        if todo_pattern.search(text) and "future" not in text.lower():
            failures.append(f"Unmarked TODO found in critical doc: {doc_path.relative_to(ROOT)}")

    server_source = (ROOT / "src" / "waggle" / "server.py").read_text()
    if '"graph-studio"' not in server_source or '"open-studio"' not in server_source:
        failures.append("CLI aliases graph-studio/open-studio are missing from src/waggle/server.py")
    if '--transport", "stdio"' not in server_source and "--transport" not in server_source:
        failures.append("CLI transport override not found in src/waggle/server.py")

    if failures:
        print("Distribution validation failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print("Distribution validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
