from waggle.context_bundle import (
    _estimate_tokens,
    _resolve_output_paths,
)


def test_estimate_tokens_empty_string():
    assert _estimate_tokens("") == 0


def test_estimate_tokens_whitespace():
    assert _estimate_tokens("    ") == 0


def test_estimate_tokens_four_chars():
    assert _estimate_tokens("abcd") == 1


def test_estimate_tokens_five_chars():
    assert _estimate_tokens("abcde") == 2


def test_estimate_tokens_eight_chars():
    assert _estimate_tokens("abcdefgh") == 2


def test_estimate_tokens_single_char():
    assert _estimate_tokens("a") == 1


def test_resolve_output_paths_markdown(tmp_path):
    md_path, json_path = _resolve_output_paths(
        output_path=None,
        export_dir=tmp_path,
        format="markdown",
        mode="graph",
    )
    assert md_path.parent == tmp_path
    assert md_path is not None
    assert md_path.suffix == ".md"
    assert json_path is None


def test_resolve_output_paths_json(tmp_path):
    md_path, json_path = _resolve_output_paths(
        output_path=None,
        export_dir=tmp_path,
        format="json",
        mode="graph",
    )

    assert md_path is None
    assert json_path is not None
    assert json_path.suffix == ".json"
    assert json_path.parent == tmp_path


def test_resolve_output_paths_both(tmp_path):
    md_path, json_path = _resolve_output_paths(
        output_path=None,
        export_dir=tmp_path,
        format="both",
        mode="graph",
    )

    assert md_path.parent == tmp_path
    assert json_path.parent == tmp_path
    assert md_path.suffix == ".md"
    assert json_path.suffix == ".json"


def test_resolve_output_paths_custom_output_path(tmp_path):
    output_file = tmp_path / "my_export"

    md_path, json_path = _resolve_output_paths(
        output_path=output_file,
        export_dir=tmp_path,
        format="both",
        mode="graph",
    )

    assert md_path == output_file.with_suffix(".md")
    assert json_path == output_file.with_suffix(".json")
