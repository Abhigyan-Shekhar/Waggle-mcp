from __future__ import annotations

import json
from pathlib import Path

from scripts.longmemeval_full.draw_fresh_slice import main


def _case(case_id: str, category: str = "single-session-user") -> dict[str, str]:
    return {"question_id": case_id, "question_type": category}


def test_draw_excludes_previously_spent_m_named_benchmark(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source.json"
    benchmarks = tmp_path / "benchmarks"
    runs = tmp_path / "runs"
    output = benchmarks / "fresh.json"
    manifest = benchmarks / "fresh_manifest.json"
    benchmarks.mkdir()
    runs.mkdir()
    source.write_text(json.dumps([_case("spent"), _case("fresh")]), encoding="utf-8")
    (benchmarks / "longmemeval_m_previous_slice.json").write_text(
        json.dumps([_case("spent")]),
        encoding="utf-8",
    )
    (benchmarks / "longmemeval_s_cleaned.json").write_text(
        json.dumps([_case("spent"), _case("fresh")]),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "draw_fresh_slice",
            "--source",
            str(source),
            "--output",
            str(output),
            "--manifest",
            str(manifest),
            "--runs-root",
            str(runs),
            "--benchmarks-root",
            str(benchmarks),
            "--size",
            "1",
        ],
    )

    assert main() == 0
    selected = json.loads(output.read_text(encoding="utf-8"))
    metadata = json.loads(manifest.read_text(encoding="utf-8"))
    assert [row["question_id"] for row in selected] == ["fresh"]
    assert metadata["excluded_case_count"] == 1
    assert metadata["spent_overlap"] == []


def test_draw_accepts_an_explicit_available_category_subset(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source.json"
    benchmarks = tmp_path / "benchmarks"
    runs = tmp_path / "runs"
    output = benchmarks / "fresh.json"
    manifest = benchmarks / "fresh_manifest.json"
    benchmarks.mkdir()
    runs.mkdir()
    source.write_text(
        json.dumps([_case("ssa", "single-session-assistant"), _case("tr", "temporal-reasoning")]),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "draw_fresh_slice",
            "--source",
            str(source),
            "--output",
            str(output),
            "--manifest",
            str(manifest),
            "--runs-root",
            str(runs),
            "--benchmarks-root",
            str(benchmarks),
            "--size",
            "2",
            "--categories",
            "single-session-assistant,temporal-reasoning",
        ],
    )

    assert main() == 0
    metadata = json.loads(manifest.read_text(encoding="utf-8"))
    assert metadata["categories"] == ["single-session-assistant", "temporal-reasoning"]
    assert metadata["category_counts"] == {
        "single-session-assistant": 1,
        "temporal-reasoning": 1,
    }
