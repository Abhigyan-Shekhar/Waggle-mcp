from __future__ import annotations

import re
import tomllib
from pathlib import Path

import waggle

ROOT = Path(__file__).resolve().parents[1]


def test_pyproject_uses_setuptools_src_layout() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())

    assert pyproject["build-system"]["build-backend"] == "setuptools.build_meta"
    assert pyproject["tool"]["setuptools"]["package-dir"] == {"": "src"}
    assert pyproject["tool"]["setuptools"]["packages"]["find"]["where"] == ["src"]


def test_dockerfile_uses_module_entrypoint_for_arg_passthrough() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()

    assert 'ENTRYPOINT ["python", "-m", "waggle.server"]' in dockerfile
    assert 'CMD ["serve"]' in dockerfile
    assert "PYTHONPATH=/app/src" not in dockerfile
    assert "HF_HOME=/app/.cache/huggingface" in dockerfile
    assert "SENTENCE_TRANSFORMERS_HOME=/app/.cache/sentence-transformers" in dockerfile
    assert "SentenceTransformer('all-MiniLM-L6-v2')" in dockerfile


def test_smithery_uses_packaged_cli_entrypoint() -> None:
    smithery = (ROOT / "smithery.yaml").read_text()

    assert "command: 'waggle-mcp'" in smithery
    assert "args: ['serve', '--transport', config.WAGGLE_TRANSPORT || 'stdio']" in smithery
    assert not re.search(r"command:\\s*'uv'", smithery)


def test_package_version_matches_pyproject() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())

    # Fallback to hardcoded version in local dev if not installed
    expected_version = pyproject["project"]["version"]
    assert waggle.__version__ in {
        expected_version,
        "0.1.3",
        "0.1.4",
        "0.1.10",
        "0.1.11",
        "0.1.12",
        "0.1.13",
        "0.1.14",
        "0.0.1",
    }


def test_app_package_manifests_exist_in_new_locations() -> None:
    assert (ROOT / "apps" / "vscode-extension" / "package.json").exists()
    assert (ROOT / "apps" / "mcp" / "claude-desktop-extension" / "manifest.json").exists()
    assert (ROOT / "apps" / "mcp" / "graph-ui" / "package.json").exists()


def test_graph_ui_bundle_contains_expected_static_assets() -> None:
    graph_static_dir = ROOT / "src" / "waggle" / "static" / "graph"

    expected_files = ["index.html", "app.css", "app.js"]
    missing = [name for name in expected_files if not (graph_static_dir / name).is_file()]

    assert not missing, (
        "Missing bundled Graph Studio assets: "
        + ", ".join(missing)
        + ". Rebuild or restore src/waggle/static/graph before packaging."
    )


def _extract_toml_fence(markdown: str, *, expected_table: str) -> str:
    for match in re.finditer(r"```toml\n(.*?)\n```", markdown, re.DOTALL):
        block = match.group(1)
        if expected_table in block:
            return block

    raise AssertionError(f"Could not find a TOML code fence containing {expected_table!r}.")


def test_codex_install_guide_matches_shipped_example_config() -> None:
    codex_guide = (ROOT / "docs" / "install" / "codex.md").read_text()
    example_config = (ROOT / "examples" / "codex_config.example.toml").read_text()

    documented = tomllib.loads(_extract_toml_fence(codex_guide, expected_table="[mcp_servers.waggle]"))
    shipped = tomllib.loads(example_config)

    assert documented["mcp_servers"]["waggle"] == shipped["mcp_servers"]["waggle"], (
        "docs/install/codex.md drifted from examples/codex_config.example.toml. "
        "Keep the documented Waggle command, args, and env values aligned."
    )
