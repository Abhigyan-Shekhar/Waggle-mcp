import json
import re

from waggle.graph_ui import render_graph_editor_html


def _extract_boot_config(html: str) -> dict:
    match = re.search(
        r"window\.__WAGGLE_GRAPH_CONFIG__\s*=\s*(\{.*?\});",
        html,
        re.DOTALL,
    )
    assert match is not None
    return json.loads(match.group(1))


def test_render_graph_editor_html_emits_versioned_boot_config():
    html = render_graph_editor_html(
        mode="view",
        project="project-a",
        agent_id="agent-a",
        session_id="session-a",
    )

    config = _extract_boot_config(html)

    assert config["schemaVersion"] == 1
    assert config["mode"] == "view"
    assert config["sampleMode"] is False
    assert config["scope"] == {
        "project": "project-a",
        "agent_id": "agent-a",
        "session_id": "session-a",
    }


def test_render_graph_editor_html_keeps_backward_compatible_flat_scope_keys():
    html = render_graph_editor_html(
        mode="edit",
        project="project-b",
        agent_id="agent-b",
        session_id="session-b",
    )

    config = _extract_boot_config(html)

    assert config["project"] == "project-b"
    assert config["agent_id"] == "agent-b"
    assert config["session_id"] == "session-b"


def test_render_graph_editor_html_normalizes_invalid_mode_to_edit():
    html = render_graph_editor_html(mode="invalid")

    config = _extract_boot_config(html)

    assert config["mode"] == "edit"
