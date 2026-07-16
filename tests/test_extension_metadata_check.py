from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

# Load check_extension_metadata script dynamically
MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "check_extension_metadata.py"
SPEC = importlib.util.spec_from_file_location("check_extension_metadata", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_metadata_match(tmp_path: Path) -> None:
    manifest_file = tmp_path / "manifest.json"
    manifest_file.write_text(json.dumps({"version": "1.2.3"}), encoding="utf-8")

    package_file = tmp_path / "package.json"
    package_file.write_text(json.dumps({"version": "1.2.3"}), encoding="utf-8")

    exit_code = MODULE.check_metadata(manifest_file, package_file)
    assert exit_code == 0


def test_metadata_mismatch(tmp_path: Path) -> None:
    manifest_file = tmp_path / "manifest.json"
    manifest_file.write_text(json.dumps({"version": "1.2.3"}), encoding="utf-8")

    package_file = tmp_path / "package.json"
    package_file.write_text(json.dumps({"version": "1.2.4"}), encoding="utf-8")

    exit_code = MODULE.check_metadata(manifest_file, package_file)
    assert exit_code == 1


def test_metadata_missing_version(tmp_path: Path) -> None:
    manifest_file = tmp_path / "manifest.json"
    manifest_file.write_text(json.dumps({}), encoding="utf-8")

    package_file = tmp_path / "package.json"
    package_file.write_text(json.dumps({"version": "1.2.3"}), encoding="utf-8")

    exit_code = MODULE.check_metadata(manifest_file, package_file)
    assert exit_code == 1


def test_metadata_missing_files(tmp_path: Path) -> None:
    manifest_file = tmp_path / "manifest.json"
    package_file = tmp_path / "package.json"

    # Both missing
    exit_code = MODULE.check_metadata(manifest_file, package_file)
    assert exit_code == 2

    # Manifest exists but package missing
    manifest_file.write_text(json.dumps({"version": "1.2.3"}), encoding="utf-8")
    exit_code = MODULE.check_metadata(manifest_file, package_file)
    assert exit_code == 2


def test_metadata_malformed_json(tmp_path: Path) -> None:
    manifest_file = tmp_path / "manifest.json"
    manifest_file.write_text("invalid json", encoding="utf-8")

    package_file = tmp_path / "package.json"
    package_file.write_text(json.dumps({"version": "1.2.3"}), encoding="utf-8")

    exit_code = MODULE.check_metadata(manifest_file, package_file)
    assert exit_code == 2


def test_metadata_non_dict_json(tmp_path: Path) -> None:
    manifest_file = tmp_path / "manifest.json"
    package_file = tmp_path / "package.json"

    # manifest.json is a list, package.json is a dict
    manifest_file.write_text(json.dumps([{"version": "1.2.3"}]), encoding="utf-8")
    package_file.write_text(json.dumps({"version": "1.2.3"}), encoding="utf-8")
    exit_code = MODULE.check_metadata(manifest_file, package_file)
    assert exit_code == 2

    # manifest.json is a dict, package.json is a string
    manifest_file.write_text(json.dumps({"version": "1.2.3"}), encoding="utf-8")
    package_file.write_text(json.dumps("version: 1.2.3"), encoding="utf-8")
    exit_code = MODULE.check_metadata(manifest_file, package_file)
    assert exit_code == 2


def test_main_entrypoint(tmp_path: Path) -> None:
    manifest_file = tmp_path / "manifest.json"
    manifest_file.write_text(json.dumps({"version": "1.0.0"}), encoding="utf-8")

    package_file = tmp_path / "package.json"
    package_file.write_text(json.dumps({"version": "1.0.0"}), encoding="utf-8")

    exit_code = MODULE.main(["--manifest", str(manifest_file), "--package", str(package_file), "--verbose"])
    assert exit_code == 0
