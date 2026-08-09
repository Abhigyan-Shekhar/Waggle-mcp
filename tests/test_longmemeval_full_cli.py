from __future__ import annotations

import json
from pathlib import Path

from scripts.longmemeval_full.run import main
from scripts.longmemeval_full.validate_artifacts import validate_output_dir


def test_full_capability_dry_run_writes_required_artifacts(tmp_path: Path) -> None:
    output_dir = tmp_path / "full-capability"

    assert (
        main(
            [
                "--dry-run",
                "--limit",
                "1",
                "--conditions",
                "flat_transcript_vector,waggle_production_context",
                "--reader-context-budget",
                "256",
                "--output-dir",
                str(output_dir),
            ]
        )
        == 0
    )

    expected = {
        "config.json",
        "frozen_case_manifest.json",
        "results.jsonl",
        "retrieval_traces.jsonl",
        "tool_traces.jsonl",
        "reader_requests.jsonl",
        "judge_requests.jsonl",
        "summary.csv",
        "category_summary.csv",
        "budget_summary.csv",
        "failure_analysis.csv",
        "context_efficiency.csv",
        "graph_contribution.csv",
        "FULL_CAPABILITY_EVALUATION_REPORT.md",
    }
    assert expected.issubset({path.name for path in output_dir.iterdir()})
    rows = [json.loads(line) for line in (output_dir / "results.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {row["condition"] for row in rows} == {"flat_transcript_vector", "waggle_production_context"}
    assert all(row["final_context_tokens"] <= 256 for row in rows)
    config = json.loads((output_dir / "config.json").read_text(encoding="utf-8"))
    assert config["secondary_judge_policy"] == "none"
    assert validate_output_dir(output_dir) == []


def test_non_dry_run_is_blocked_before_paid_calls(tmp_path: Path) -> None:
    output_dir = tmp_path / "paid-blocked"
    try:
        main(["--limit", "1", "--output-dir", str(output_dir)])
    except SystemExit as exc:
        assert "requires --allow-paid" in str(exc)
    else:
        raise AssertionError("non-dry-run should be blocked")


def test_case_ids_select_exact_cases_and_are_frozen(tmp_path: Path) -> None:
    output_dir = tmp_path / "selected-cases"

    assert (
        main(
            [
                "--dry-run",
                "--case-ids",
                "stress_v2_fixture_002,stress_v2_fixture_001",
                "--conditions",
                "flat_transcript_vector",
                "--output-dir",
                str(output_dir),
            ]
        )
        == 0
    )

    manifest = json.loads((output_dir / "frozen_case_manifest.json").read_text(encoding="utf-8"))
    config = json.loads((output_dir / "config.json").read_text(encoding="utf-8"))
    assert [row["case_id"] for row in manifest["cases"]] == [
        "stress_v2_fixture_001",
        "stress_v2_fixture_002",
    ]
    assert config["case_ids"] == "stress_v2_fixture_002,stress_v2_fixture_001"
