from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent

FENCED_CODE_PATTERN = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE_PATTERN = re.compile(r"`[^`\n]+`")

LINK_PATTERN = re.compile(r"\[[^\]]+\]\(([^)]+)\)")

IGNORED_DIRS = {".venv", ".git", "node_modules"}

markdown_files = sorted(
    path
    for path in ROOT.rglob("*.md")
    if not any(part in IGNORED_DIRS for part in path.parts)
)

broken_links = []

for md_file in markdown_files:
    text = md_file.read_text(encoding="utf-8")
    
    # Ignore Markdown examples inside code blocks and inline code spans.
    text = FENCED_CODE_PATTERN.sub("", text)
    text = INLINE_CODE_PATTERN.sub("", text)

    for match in LINK_PATTERN.finditer(text):
        link = match.group(1).strip()

        # Ignore external URLs
        if (
            link.startswith("http://")
            or link.startswith("https://")
            or link.startswith("mailto:")
            or link.startswith("#")
        ):
            continue

        # Remove anchor part
        parsed = urlsplit(link)
        link_path = parsed.path

        if not link_path:
            continue

        resolved_path = (md_file.parent / link_path).resolve()

        try:
            relative_path = resolved_path.relative_to(ROOT)
        except ValueError:
            broken_links.append(
                (
                    md_file.relative_to(ROOT),
                    link,
                    resolved_path,
                )
            )
            continue

        if not resolved_path.exists():
            broken_links.append(
                (
                    md_file.relative_to(ROOT),
                    link,
                    relative_path,
                )
            )

if broken_links:
    print("\nBroken markdown links found:\n")

    for source_file, link, resolved in broken_links:
        print(f"{source_file}")
        print(f"  Link: {link}")
        print(f"  Resolved path: {resolved}")
        print()

    sys.exit(1)

print("All markdown links are valid.")