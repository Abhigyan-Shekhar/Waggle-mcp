from __future__ import annotations

from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from typing import Any

import anyio
import mcp.types as types
import numpy as np

from waggle.config import AppConfig
from waggle.graph import MemoryGraph
from waggle.metrics import MetricsRegistry
from waggle.protocol.mcp.adapter import WagglemcpAdapter
from waggle.protocol.mcp.server import build_waggle_server
from waggle.tools.dispatcher import WaggleToolDispatcher
from waggle.tools.results import WaggleToolResult


class FakeEmbeddingModel:
    model_name = "fake-model"
    model_id = "fake-model:deterministic-v1"
    warmup_status = "disabled"
    warmup_error = ""

    def embed(self, text: str) -> np.ndarray:
        vector = np.zeros(8, dtype=np.float32)
        for token in text.lower().split():
            vector[sum(ord(character) for character in token) % len(vector)] += 1.0
        norm = np.linalg.norm(vector)
        if norm == 0.0:
            return vector
        return vector / norm

    def to_bytes(self, embedding: np.ndarray) -> bytes:
        return embedding.astype(np.float32).tobytes()

    def from_bytes(self, data: bytes) -> np.ndarray:
        return np.frombuffer(data, dtype=np.float32)

    def cosine_similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        a_norm = np.linalg.norm(a)
        b_norm = np.linalg.norm(b)
        if a_norm == 0.0 or b_norm == 0.0:
            return 0.0
        return float(np.dot(a, b) / (a_norm * b_norm))


def make_adapter(tmp_path: Path) -> WagglemcpAdapter:
    config = AppConfig.from_env()
    config.startup_mode = "normal"
    graph = MemoryGraph(tmp_path / "mcp-v2.db", FakeEmbeddingModel(), tenant_id="test-tenant")
    dispatcher = WaggleToolDispatcher(graph=graph, config=config, metrics=MetricsRegistry())
    return WagglemcpAdapter(dispatcher=dispatcher)


def test_list_tools_uses_v2_snake_case_schema(tmp_path: Path) -> None:
    adapter = make_adapter(tmp_path)

    result = anyio.run(adapter.on_list_tools, SimpleNamespace(request_id="r1", request=None), None)

    assert result.tools
    query_graph = next(tool for tool in result.tools if tool.name == "query_graph")
    assert query_graph.input_schema["type"] == "object"
    dumped = query_graph.model_dump(mode="json", by_alias=False)
    assert "input_schema" in dumped
    assert "inputSchema" not in dumped


def test_list_tools_preserves_mcp_annotations(tmp_path: Path) -> None:
    adapter = make_adapter(tmp_path)

    result = anyio.run(adapter.on_list_tools, SimpleNamespace(request_id="r1", request=None), None)

    tools = {tool.name: tool for tool in result.tools}
    assert tools["get_stats"].annotations.read_only_hint is True
    assert tools["query_graph"].annotations.read_only_hint is False
    assert tools["query_graph"].annotations.idempotent_hint is False
    assert tools["query_graph"].annotations.destructive_hint is False
    assert tools["store_node"].annotations.read_only_hint is False
    assert tools["delete_node"].annotations.destructive_hint is True


def test_call_tool_validation_failure_returns_is_error(tmp_path: Path) -> None:
    adapter = make_adapter(tmp_path)

    result = anyio.run(
        adapter.on_call_tool,
        SimpleNamespace(request_id="r1", request=None),
        types.CallToolRequestParams(name="query_graph", arguments={}),
    )

    assert result.is_error is True
    assert result.structured_content["error_code"] == "validation_failed"
    assert "query" in result.content[0].text


def test_alias_defaults_are_applied_before_schema_validation(tmp_path: Path) -> None:
    adapter = make_adapter(tmp_path)

    result = anyio.run(
        adapter.on_call_tool,
        SimpleNamespace(request_id="r1", request=None),
        types.CallToolRequestParams(name="query_abhi", arguments={}),
    )

    assert result.is_error is True
    assert result.structured_content["error_code"] == "validation_failed"
    assert "input_path" in result.content[0].text


def test_concurrent_http_calls_keep_tenant_graph_request_local(tmp_path: Path) -> None:
    config = AppConfig.from_env()
    config.startup_mode = "normal"
    root_graph = MemoryGraph(tmp_path / "mcp-v2-tenants.db", FakeEmbeddingModel(), tenant_id="root")
    dispatcher = WaggleToolDispatcher(graph=root_graph, config=config, metrics=MetricsRegistry())
    adapter = WagglemcpAdapter(dispatcher=dispatcher)
    barrier = Barrier(2)

    def fake_call_tool(
        name: str,
        arguments: dict[str, Any],
        context: Any,
        graph_override: Any | None = None,
    ) -> WaggleToolResult:
        del name, arguments, context
        barrier.wait(timeout=5)
        graph = graph_override or dispatcher._graph
        tenant_id = getattr(graph, "tenant_id", "")
        return WaggleToolResult(text=tenant_id, structured={"tenant_id": tenant_id}, is_error=False)

    dispatcher.call_tool = fake_call_tool  # type: ignore[method-assign]

    def make_ctx(tenant_id: str) -> SimpleNamespace:
        state = SimpleNamespace(tenant_id=tenant_id, request_id=f"req-{tenant_id}", api_key_id=f"key-{tenant_id}")
        return SimpleNamespace(request=SimpleNamespace(state=state))

    async def call_for_tenant(tenant_id: str) -> str:
        result = await adapter.on_call_tool(
            make_ctx(tenant_id),
            types.CallToolRequestParams(name="query_graph", arguments={"query": "hello"}),
        )
        return str(result.structured_content["tenant_id"])

    async def run_concurrently() -> tuple[str, str]:
        results: dict[str, str] = {}

        async with anyio.create_task_group() as task_group:

            async def worker(tenant_id: str) -> None:
                results[tenant_id] = await call_for_tenant(tenant_id)

            task_group.start_soon(worker, "tenant-a")
            task_group.start_soon(worker, "tenant-b")

        return results["tenant-a"], results["tenant-b"]

    assert anyio.run(run_concurrently) == ("tenant-a", "tenant-b")
    assert dispatcher._graph is root_graph


def test_build_waggle_server_does_not_import_legacy_server_compat(tmp_path: Path) -> None:
    config = AppConfig.from_env()
    config.startup_mode = "normal"
    graph = MemoryGraph(tmp_path / "mcp-v2-server.db", FakeEmbeddingModel(), tenant_id="test-tenant")

    server, adapter = build_waggle_server(graph=graph, config=config)

    assert type(server).__name__ == "Server"
    assert isinstance(adapter, WagglemcpAdapter)
    assert hasattr(server, "streamable_http_app")
