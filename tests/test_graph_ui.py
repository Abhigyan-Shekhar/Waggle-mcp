import re

import pytest

from waggle.graph_ui import render_graph_editor_html


def test_default_mode_is_edit():
    html = render_graph_editor_html()

    assert '"mode": "edit"' in html


def test_view_mode_is_honored():
    html = render_graph_editor_html(mode="view")

    assert '"mode": "view"' in html


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("VIEW", "view"),
        ("  view  ", "view"),
        ("anything else", "edit"),
    ],
)
def test_mode_normalization(mode, expected):
    html = render_graph_editor_html(mode=mode)

    assert f'"mode": "{expected}"' in html


def test_scope_parameters_are_injected():
    html = render_graph_editor_html(
        project="p",
        agent_id="a",
        session_id="s",
    )

    assert '"project": "p"' in html
    assert '"agent_id": "a"' in html
    assert '"session_id": "s"' in html


def test_special_characters_are_json_safe():
    payload = '"><script>alert(1)</script>'

    html = render_graph_editor_html(project=payload)

    assert "<script>alert(1)</script>" not in html


def test_asset_version_is_numeric():
    html = render_graph_editor_html()

    matches = re.findall(r"\?v=(\d+)", html)

    assert matches
    assert all(int(version) >= 0 for version in matches)


def test_missing_assets_fall_back_to_version_zero(monkeypatch):
    from pathlib import Path

    import waggle.graph_ui as graph_ui

    class FakeResolvedPath:
        @property
        def parent(self):
            return Path("/definitely/nonexistent/path")

    monkeypatch.setattr(
        graph_ui.Path,
        "resolve",
        lambda self: FakeResolvedPath(),
    )

    html = render_graph_editor_html()

    assert "?v=0" in html
