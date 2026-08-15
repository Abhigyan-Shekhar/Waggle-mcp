from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def test_markdown_link_checker_ignores_fenced_code_blocks(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / "check_markdown_links.py"
    temp_script = tmp_path / "scripts" / "check_markdown_links.py"
    temp_script.parent.mkdir()

    shutil.copy(script, temp_script)

    readme = tmp_path / "README.md"
    readme.write_text(
        "# Example\n\n"
        "```text\n"
        "https://example.invalid/not-a-documentation-link\n"
        "```\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [sys.executable, str(temp_script)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "All markdown links are valid." in completed.stdout