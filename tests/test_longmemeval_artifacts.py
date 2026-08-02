from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

MODULE_PATH = SCRIPTS / "validate_longmemeval_artifacts.py"
SPEC = importlib.util.spec_from_file_location("validate_longmemeval_artifacts", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

PLAN_PATH = SCRIPTS / "plan_longmemeval_run.py"
PLAN_SPEC = importlib.util.spec_from_file_location("plan_longmemeval_run", PLAN_PATH)
assert PLAN_SPEC and PLAN_SPEC.loader
PLAN_MODULE = importlib.util.module_from_spec(PLAN_SPEC)
sys.modules[PLAN_SPEC.name] = PLAN_MODULE
PLAN_SPEC.loader.exec_module(PLAN_MODULE)

BUDGET_PATH = SCRIPTS / "project_longmemeval_budget.py"
BUDGET_SPEC = importlib.util.spec_from_file_location("project_longmemeval_budget", BUDGET_PATH)
assert BUDGET_SPEC and BUDGET_SPEC.loader
BUDGET_MODULE = importlib.util.module_from_spec(BUDGET_SPEC)
sys.modules[BUDGET_SPEC.name] = BUDGET_MODULE
BUDGET_SPEC.loader.exec_module(BUDGET_MODULE)

MOCK_RUNNER_PATH = SCRIPTS / "run_longmemeval_mock_phase.py"
MOCK_RUNNER_SPEC = importlib.util.spec_from_file_location("run_longmemeval_mock_phase", MOCK_RUNNER_PATH)
assert MOCK_RUNNER_SPEC and MOCK_RUNNER_SPEC.loader
MOCK_RUNNER_MODULE = importlib.util.module_from_spec(MOCK_RUNNER_SPEC)
sys.modules[MOCK_RUNNER_SPEC.name] = MOCK_RUNNER_MODULE
MOCK_RUNNER_SPEC.loader.exec_module(MOCK_RUNNER_MODULE)

SUMMARY_PATH = SCRIPTS / "summarize_longmemeval_results.py"
SUMMARY_SPEC = importlib.util.spec_from_file_location("summarize_longmemeval_results", SUMMARY_PATH)
assert SUMMARY_SPEC and SUMMARY_SPEC.loader
SUMMARY_MODULE = importlib.util.module_from_spec(SUMMARY_SPEC)
sys.modules[SUMMARY_SPEC.name] = SUMMARY_MODULE
SUMMARY_SPEC.loader.exec_module(SUMMARY_MODULE)

PREFLIGHT_PATH = SCRIPTS / "preflight_longmemeval_run.py"
PREFLIGHT_SPEC = importlib.util.spec_from_file_location("preflight_longmemeval_run", PREFLIGHT_PATH)
assert PREFLIGHT_SPEC and PREFLIGHT_SPEC.loader
PREFLIGHT_MODULE = importlib.util.module_from_spec(PREFLIGHT_SPEC)
sys.modules[PREFLIGHT_SPEC.name] = PREFLIGHT_MODULE
PREFLIGHT_SPEC.loader.exec_module(PREFLIGHT_MODULE)

LEDGER_PATH = SCRIPTS / "export_longmemeval_cost_ledger.py"
LEDGER_SPEC = importlib.util.spec_from_file_location("export_longmemeval_cost_ledger", LEDGER_PATH)
assert LEDGER_SPEC and LEDGER_SPEC.loader
LEDGER_MODULE = importlib.util.module_from_spec(LEDGER_SPEC)
sys.modules[LEDGER_SPEC.name] = LEDGER_MODULE
LEDGER_SPEC.loader.exec_module(LEDGER_MODULE)

PIPELINE_PATH = SCRIPTS / "run_longmemeval_artifact_pipeline.py"
PIPELINE_SPEC = importlib.util.spec_from_file_location("run_longmemeval_artifact_pipeline", PIPELINE_PATH)
assert PIPELINE_SPEC and PIPELINE_SPEC.loader
PIPELINE_MODULE = importlib.util.module_from_spec(PIPELINE_SPEC)
sys.modules[PIPELINE_SPEC.name] = PIPELINE_MODULE
PIPELINE_SPEC.loader.exec_module(PIPELINE_MODULE)


def _row(**overrides):
    payload = {
        "case_id": "case-001",
        "suite": "longmemeval_s",
        "split": "mock",
        "category": "SSU",
        "condition": "waggle_full",
        "reader_model": "gemini-2.5-flash",
        "judge_model": "gpt-4o",
        "dataset_sha256": "a" * 64,
        "prompt_version": "prompt-v1",
        "run_artifact": "runs/mock-001.jsonl",
        "gold_support_ids": ["s1"],
        "retrieved_support_ids": ["s1", "s2"],
        "context_tokens": 512,
        "input_tokens": 900,
        "output_tokens": 80,
        "answer": "The user prefers concise answers.",
        "judge_result": {"score": 1, "rationale": "Exact."},
        "latency_seconds": 1.25,
        "cost_usd": 0.01,
        "official_table_eligible": True,
    }
    payload.update(overrides)
    return payload


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _dataset_case(case_id: str, category: str) -> dict[str, str]:
    return {
        "case_id": case_id,
        "category": category,
        "question": f"Question for {case_id}",
        "answer": "Answer",
        "gold_support_ids": [f"{case_id}-s1"],
        "sessions": [
            {"session_id": f"{case_id}-s1", "messages": [{"role": "user", "content": "Relevant fact."}]},
            {"session_id": f"{case_id}-s2", "messages": [{"role": "assistant", "content": "Distractor fact."}]},
        ],
    }


def test_validator_accepts_valid_mock_row(tmp_path: Path) -> None:
    jsonl = tmp_path / "results.jsonl"
    _write_jsonl(jsonl, [_row()])

    assert MODULE.main([str(jsonl)]) == 0


def test_validator_blocks_heldout_rows_by_default(tmp_path: Path) -> None:
    jsonl = tmp_path / "results.jsonl"
    _write_jsonl(jsonl, [_row(split="heldout")])

    assert MODULE.main([str(jsonl)]) == 1
    assert MODULE.main([str(jsonl), "--allow-heldout"]) == 0


def test_validator_rejects_supplementary_rows_in_official_table(tmp_path: Path) -> None:
    jsonl = tmp_path / "results.jsonl"
    _write_jsonl(
        jsonl,
        [
            _row(
                suite="supplementary_stress",
                split="stress",
                category="agent_decision_memory",
                official_table_eligible=True,
            )
        ],
    )

    assert MODULE.main([str(jsonl)]) == 1


def test_validator_enforces_single_paid_budget_cap(tmp_path: Path) -> None:
    jsonl = tmp_path / "results.jsonl"
    _write_jsonl(jsonl, [_row(cost_usd=100.0), _row(case_id="case-002", cost_usd=81.0)])

    assert MODULE.main([str(jsonl), "--max-paid-cost", "180"]) == 1


def test_lisa_facing_plan_does_not_cite_readme_benchmark_number() -> None:
    text = (ROOT / "docs" / "longmemeval-systems-paper-plan.md").read_text(encoding="utf-8")

    assert "97.4" not in text
    assert "89.0" not in text
    assert "$180 total" in text
    assert "Qwen/Qwen3.7-Plus full-context is not pre-approved" in text


def test_split_planner_writes_id_only_splits_and_validator_manifest(tmp_path: Path) -> None:
    cases = []
    for category in ["SSU", "SSA", "SSP", "KU", "TR", "MS"]:
        for index in range(4):
            cases.append(_dataset_case(f"{category}-{index}", category))
    dataset = tmp_path / "longmemeval_s.json"
    dataset.write_text(json.dumps(cases), encoding="utf-8")
    output_dir = tmp_path / "run"

    assert (
        PLAN_MODULE.main(
            [
                str(dataset),
                "--output-dir",
                str(output_dir),
                "--mock-size",
                "6",
                "--heldout-size",
                "6",
                "--run-id",
                "test-run",
            ]
        )
        == 0
    )

    split_plan = json.loads((output_dir / "split-plan.json").read_text(encoding="utf-8"))
    manifest = output_dir / "run-manifest.json"
    assert len(split_plan["splits"]["mock"]) == 6
    assert len(split_plan["splits"]["heldout"]) == 6
    assert set(split_plan["splits"]) == {"mock", "heldout", "tune"}
    assert all(set(item) == {"case_id", "category"} for item in split_plan["splits"]["heldout"])
    manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert (
        manifest_payload["retrieval_config"]["flat_vector"]["embedding_model"]
        == manifest_payload["retrieval_config"]["waggle_full"]["embedding_model"]
    )
    assert (
        manifest_payload["retrieval_config"]["flat_vector"]["chunking_policy"]
        == manifest_payload["retrieval_config"]["waggle_full"]["chunking_policy"]
    )
    assert manifest_payload["ingestion_protocol"] == "session-by-session"
    assert manifest_payload["answering_prompt_style"] == "supermemory-longmembench-appendix-v1"
    assert manifest_payload["judge_protocol"] == "longmemeval-paper-question-specific-prompts"
    assert MODULE.validate_manifest(manifest, max_paid_cost=180.0) == []


def test_budget_projector_uses_mock_tokens_for_qwen_full_context_gate(tmp_path: Path) -> None:
    jsonl = tmp_path / "mock.jsonl"
    _write_jsonl(
        jsonl,
        [
            _row(
                condition="full_context",
                reader_model="Qwen/Qwen3.7-Plus",
                input_tokens=1_000_000,
                output_tokens=1_000,
                cost_usd=0.0,
            ),
            _row(
                case_id="case-002",
                condition="full_context",
                reader_model="Qwen/Qwen3.7-Plus",
                input_tokens=1_000_000,
                output_tokens=1_000,
                cost_usd=0.0,
            ),
        ],
    )

    projection = BUDGET_MODULE.project_budget(
        rows=MODULE.load_jsonl(jsonl)[0],
        targets=[("full_context", "Qwen/Qwen3.7-Plus", 500, 0.32, 1.28)],
        fixed_cost=0.0,
        cap=180.0,
    )

    assert projection["projected_total_cost_usd"] == 160.64
    assert projection["fits_cap"] is True


def test_budget_projector_fails_when_projection_exceeds_single_cap(tmp_path: Path) -> None:
    jsonl = tmp_path / "mock.jsonl"
    _write_jsonl(
        jsonl,
        [
            _row(
                condition="full_context",
                reader_model="Qwen/Qwen3.7-Plus",
                input_tokens=1_200_000,
                output_tokens=1_000,
                cost_usd=0.0,
            )
        ],
    )

    assert (
        BUDGET_MODULE.main(
            [
                str(jsonl),
                "--target",
                "full_context,Qwen/Qwen3.7-Plus,500,0.32,1.28",
                "--cap",
                "180",
            ]
        )
        == 2
    )


def test_mock_phase_runner_emits_validator_compatible_dry_run_rows(tmp_path: Path) -> None:
    cases = []
    for category in ["SSU", "SSA", "SSP", "KU", "TR", "MS"]:
        for index in range(2):
            cases.append(_dataset_case(f"{category}-{index}", category))
    dataset = tmp_path / "longmemeval_s.json"
    dataset.write_text(json.dumps(cases), encoding="utf-8")
    output_dir = tmp_path / "run"
    assert (
        PLAN_MODULE.main(
            [
                str(dataset),
                "--output-dir",
                str(output_dir),
                "--mock-size",
                "6",
                "--heldout-size",
                "3",
            ]
        )
        == 0
    )

    results = output_dir / "results.jsonl"
    assert (
        MOCK_RUNNER_MODULE.main(
            [
                str(dataset),
                "--split-plan",
                str(output_dir / "split-plan.json"),
                "--output",
                str(results),
                "--condition",
                "full_context",
                "--condition",
                "waggle_full",
            ]
        )
        == 0
    )

    rows, errors = MODULE.load_jsonl(results)
    assert errors == []
    assert len(rows) == 12
    assert {row["condition"] for row in rows} == {"full_context", "waggle_full"}
    assert {row["reader_model"] for row in rows} == {"dry-run-reader"}
    assert {row["official_table_eligible"] for row in rows} == {False}
    assert {row["retrieval_trace"]["ingestion_protocol"] for row in rows} == {"session-by-session"}
    assert {row["retrieval_trace"]["answering_prompt_style"] for row in rows} == {
        "supermemory-longmembench-appendix-v1"
    }
    assert MODULE.main([str(results)]) == 0


def test_summarizer_keeps_official_mock_and_stress_rows_separate(tmp_path: Path) -> None:
    jsonl = tmp_path / "results.jsonl"
    _write_jsonl(
        jsonl,
        [
            _row(
                case_id="official-1",
                condition="waggle_full",
                category="SSU",
                gold_support_ids=["s1", "s2"],
                retrieved_support_ids=["s1", "s2", "s3"],
                judge_result={"score": 1},
                official_table_eligible=True,
            ),
            _row(
                case_id="mock-1",
                condition="flat_vector",
                category="SSU",
                gold_support_ids=["s1", "s2"],
                retrieved_support_ids=["s1", "s3"],
                judge_result={"score": 0},
                official_table_eligible=False,
            ),
            _row(
                case_id="stress-1",
                suite="supplementary_stress",
                split="stress",
                category="cross_session_chains",
                condition="waggle_full",
                gold_support_ids=["x1"],
                retrieved_support_ids=["x2", "x1"],
                judge_result={"score": 0.5},
                official_table_eligible=False,
            ),
        ],
    )

    rows = SUMMARY_MODULE._load_valid_rows(jsonl, allow_heldout=False, max_paid_cost=180.0)
    summary = SUMMARY_MODULE.summarize(rows)

    assert summary["official_longmemeval"]["overall"]["rows"] == 1
    assert summary["non_official_longmemeval"]["overall"]["rows"] == 1
    assert summary["supplementary_stress"]["overall"]["rows"] == 1
    official_waggle = summary["official_longmemeval"]["by_condition"]["waggle_full"]
    assert official_waggle["retrieval"]["support_hit_at_5"] == 1.0
    assert official_waggle["retrieval"]["exact_support_at_5"] == 1.0
    mock_flat = summary["non_official_longmemeval"]["by_condition"]["flat_vector"]
    assert mock_flat["retrieval"]["support_hit_at_5"] == 1.0
    assert mock_flat["retrieval"]["exact_support_at_5"] == 0.0


def test_summarizer_writes_json_and_markdown(tmp_path: Path) -> None:
    jsonl = tmp_path / "results.jsonl"
    output_json = tmp_path / "summary.json"
    output_md = tmp_path / "summary.md"
    _write_jsonl(jsonl, [_row(official_table_eligible=True)])

    assert (
        SUMMARY_MODULE.main(
            [
                str(jsonl),
                "--output-json",
                str(output_json),
                "--output-md",
                str(output_md),
            ]
        )
        == 0
    )
    assert json.loads(output_json.read_text(encoding="utf-8"))["row_count"] == 1
    markdown = output_md.read_text(encoding="utf-8")
    assert "Official LongMemEval-S" in markdown
    assert "Supplementary Stress" in markdown


def test_preflight_passes_complete_dry_run_artifact_set(tmp_path: Path) -> None:
    cases = []
    for category in ["SSU", "SSA", "SSP", "KU", "TR", "MS"]:
        for index in range(20):
            cases.append(_dataset_case(f"{category}-{index}", category))
    dataset = tmp_path / "longmemeval_s.json"
    dataset.write_text(json.dumps(cases), encoding="utf-8")
    output_dir = tmp_path / "run"

    assert (
        PLAN_MODULE.main(
            [
                str(dataset),
                "--output-dir",
                str(output_dir),
                "--mock-size",
                "12",
                "--heldout-size",
                "100",
                "--run-id",
                "preflight-test",
            ]
        )
        == 0
    )

    errors = PREFLIGHT_MODULE.check_preflight(
        manifest_path=output_dir / "run-manifest.json",
        split_plan=output_dir / "split-plan.json",
        budget_projection=None,
        env={},
        max_paid_cost=180.0,
        skip_key_check=True,
    )

    assert errors == []


def test_preflight_blocks_missing_keys_and_bad_budget_projection(tmp_path: Path) -> None:
    dataset = tmp_path / "longmemeval_s.json"
    dataset.write_text(json.dumps([_dataset_case("case-1", "SSU")]), encoding="utf-8")
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    manifest = {
        "run_id": "paid-test",
        "created_at": "2026-07-06T00:00:00+00:00",
        "dataset_path": str(dataset),
        "dataset_sha256": PLAN_MODULE._sha256(dataset),
        "prompt_version": "longmemeval-systems-v1",
        "answering_prompt_style": "supermemory-longmembench-appendix-v1",
        "judge_protocol": "longmemeval-paper-question-specific-prompts",
        "ingestion_protocol": "session-by-session",
        "conditions": ["waggle_full"],
        "models": {"reader": "Qwen/Qwen3.7-Plus", "judge": "gpt-4o"},
        "retrieval_config": {
            "waggle_full": {
                "embedding_model": "all-MiniLM-L6-v2",
                "chunking_policy": "longmemeval-session-chunks-v1",
                "ingestion_granularity": "session",
                "retrieval_unit": "memory_then_chunk",
                "answer_context_mode": "memory_plus_source_chunk",
                "memory_generation": "contextual_atomic_facts",
                "temporal_fields": ["documentDate", "eventDate"],
            }
        },
        "result_jsonl": str(output_dir / "results.jsonl"),
        "projected_total_paid_cost_usd": 179.0,
        "max_total_paid_cost_usd": 180.0,
        "heldout_policy": "heldout rows are not inspected until final evaluation",
    }
    manifest_path = output_dir / "run-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    projection_path = output_dir / "budget.json"
    projection_path.write_text(
        json.dumps({"fits_cap": False, "projected_total_cost_usd": 181.0}),
        encoding="utf-8",
    )

    errors = PREFLIGHT_MODULE.check_preflight(
        manifest_path=manifest_path,
        split_plan=None,
        budget_projection=projection_path,
        env={},
        max_paid_cost=180.0,
        skip_key_check=False,
    )

    assert "missing provider API keys: OPENAI_API_KEY, TOGETHER_API_KEY" in errors
    assert "budget projection does not fit cap" in errors
    assert "budget projection $181.00 exceeds $180.00" in errors


def test_preflight_blocks_retrieval_config_parity_drift(tmp_path: Path) -> None:
    dataset = tmp_path / "longmemeval_s.json"
    dataset.write_text(json.dumps([_dataset_case("case-1", "SSU")]), encoding="utf-8")
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    manifest = {
        "run_id": "parity-test",
        "created_at": "2026-07-06T00:00:00+00:00",
        "dataset_path": str(dataset),
        "dataset_sha256": PLAN_MODULE._sha256(dataset),
        "prompt_version": "longmemeval-systems-v1",
        "answering_prompt_style": "supermemory-longmembench-appendix-v1",
        "judge_protocol": "longmemeval-paper-question-specific-prompts",
        "ingestion_protocol": "session-by-session",
        "conditions": ["flat_vector", "waggle_full"],
        "models": {"reader": "dry-run-reader", "judge": "dry-run-judge"},
        "retrieval_config": {
            "flat_vector": {
                "embedding_model": "all-MiniLM-L6-v2",
                "chunking_policy": "longmemeval-session-chunks-v1",
                "ingestion_granularity": "session",
                "retrieval_unit": "chunk_only",
                "answer_context_mode": "source_chunk_only",
                "memory_generation": "none",
                "temporal_fields": [],
            },
            "waggle_full": {
                "embedding_model": "text-embedding-3-small",
                "chunking_policy": "longmemeval-session-chunks-v1",
                "ingestion_granularity": "session",
                "retrieval_unit": "memory_then_chunk",
                "answer_context_mode": "memory_plus_source_chunk",
                "memory_generation": "contextual_atomic_facts",
                "temporal_fields": ["documentDate", "eventDate"],
            },
        },
        "result_jsonl": str(output_dir / "results.jsonl"),
        "projected_total_paid_cost_usd": 0.0,
        "max_total_paid_cost_usd": 180.0,
        "heldout_policy": "heldout rows are not inspected until final evaluation",
    }
    manifest_path = output_dir / "run-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    errors = PREFLIGHT_MODULE.check_preflight(
        manifest_path=manifest_path,
        split_plan=None,
        budget_projection=None,
        env={},
        max_paid_cost=180.0,
        skip_key_check=True,
    )

    assert "manifest: retrieval-assisted conditions must share embedding_model and chunking_policy" in errors
    assert "retrieval-assisted conditions must share embedding_model and chunking_policy" in errors


def test_cost_ledger_groups_spend_by_model_and_condition(tmp_path: Path) -> None:
    jsonl = tmp_path / "results.jsonl"
    output_json = tmp_path / "cost-ledger.json"
    output_md = tmp_path / "cost-ledger.md"
    _write_jsonl(
        jsonl,
        [
            _row(
                case_id="case-1",
                reader_model="llama-3.3-70b",
                condition="waggle_full",
                input_tokens=100,
                output_tokens=10,
                context_tokens=50,
                cost_usd=1.25,
            ),
            _row(
                case_id="case-2",
                reader_model="llama-3.3-70b",
                condition="flat_vector",
                input_tokens=200,
                output_tokens=20,
                context_tokens=75,
                cost_usd=2.25,
            ),
            _row(
                case_id="case-3",
                reader_model="Qwen/Qwen3.7-Plus",
                condition="waggle_full",
                input_tokens=300,
                output_tokens=30,
                context_tokens=80,
                cost_usd=3.5,
            ),
        ],
    )

    assert (
        LEDGER_MODULE.main(
            [
                str(jsonl),
                "--output-json",
                str(output_json),
                "--output-md",
                str(output_md),
                "--max-paid-cost",
                "180",
            ]
        )
        == 0
    )

    ledger = json.loads(output_json.read_text(encoding="utf-8"))
    assert ledger["total_cost_usd"] == 7.0
    assert ledger["remaining_budget_usd"] == 173.0
    assert ledger["by_reader_model"]["llama-3.3-70b"]["cost_usd"] == 3.5
    assert ledger["by_condition"]["waggle_full"]["rows"] == 2
    assert ledger["by_reader_condition"]["Qwen/Qwen3.7-Plus|waggle_full"]["input_tokens"] == 300
    markdown = output_md.read_text(encoding="utf-8")
    assert "LongMemEval Cost Ledger" in markdown
    assert "By Reader Model" in markdown


def test_cost_ledger_refuses_to_export_over_cap(tmp_path: Path) -> None:
    jsonl = tmp_path / "results.jsonl"
    output_json = tmp_path / "cost-ledger.json"
    _write_jsonl(jsonl, [_row(cost_usd=181.0)])

    assert (
        LEDGER_MODULE.main(
            [
                str(jsonl),
                "--output-json",
                str(output_json),
                "--max-paid-cost",
                "180",
            ]
        )
        == 1
    )
    assert not output_json.exists()


def test_artifact_pipeline_runs_end_to_end_in_dry_run(tmp_path: Path) -> None:
    cases = []
    for category in ["SSU", "SSA", "SSP", "KU", "TR", "MS"]:
        for index in range(20):
            cases.append(_dataset_case(f"{category}-{index}", category))
    dataset = tmp_path / "longmemeval_s.json"
    dataset.write_text(json.dumps(cases), encoding="utf-8")
    output_dir = tmp_path / "pipeline"

    assert (
        PIPELINE_MODULE.main(
            [
                str(dataset),
                "--output-dir",
                str(output_dir),
                "--mock-size",
                "6",
                "--heldout-size",
                "100",
                "--condition",
                "full_context",
                "--condition",
                "waggle_full",
            ]
        )
        == 0
    )

    expected = [
        "split-plan.json",
        "run-manifest.json",
        "results.jsonl",
        "cost-ledger.json",
        "cost-ledger.md",
        "summary.json",
        "summary.md",
    ]
    for filename in expected:
        assert (output_dir / filename).exists()

    rows, errors = MODULE.load_jsonl(output_dir / "results.jsonl")
    assert errors == []
    assert len(rows) == 12
    assert {row["condition"] for row in rows} == {"full_context", "waggle_full"}
    ledger = json.loads((output_dir / "cost-ledger.json").read_text(encoding="utf-8"))
    assert ledger["fits_cap"] is True
    assert ledger["totals"]["rows"] == 12
    summary = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["non_official_longmemeval"]["overall"]["rows"] == 12
