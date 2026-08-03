from __future__ import annotations

import threading
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import anyio
import pytest

from waggle.config import AppConfig
from waggle.errors import ServiceUnavailableError
from waggle.protocol.mcp import http as mcp_http
from waggle.protocol.mcp.http import MCPHttpApp


class FakeMetrics:
    def increment(self, *args: Any, **kwargs: Any) -> None:
        pass

    def observe(self, *args: Any, **kwargs: Any) -> None:
        pass

    def set_gauge(self, *args: Any, **kwargs: Any) -> None:
        pass


class FakeTenantGraph:
    tenant_id = "tenant-a"

    def emit_audit_event(self, **kwargs: Any) -> None:
        pass


class FakeRootGraph:
    def authenticate_api_key(self, raw_api_key: str) -> Any:
        assert raw_api_key == "test-key"
        return SimpleNamespace(
            tenant_id="tenant-a",
            api_key_id="key-a",
            name="test key",
            require_scope=lambda scope: None,
        )

    def for_tenant(self, tenant_id: str) -> FakeTenantGraph:
        assert tenant_id == "tenant-a"
        return FakeTenantGraph()


class SlowRootGraph(FakeRootGraph):
    def __init__(self) -> None:
        self.started = threading.Event()
        self.finished = threading.Event()

    def authenticate_api_key(self, raw_api_key: str) -> Any:
        self.started.set()
        time.sleep(0.15)
        self.finished.set()
        return super().authenticate_api_key(raw_api_key)


class FakeRateLimiter:
    async def check_rate(self, api_key_id: str, *, is_write: bool) -> None:
        pass

    @asynccontextmanager
    async def concurrency_slot(self, api_key_id: str):
        yield


def make_http_service(mcp_app: Any, *, root_graph: Any | None = None) -> MCPHttpApp:
    config = AppConfig.from_env()
    config.request_timeout_seconds = 30
    service = MCPHttpApp.__new__(MCPHttpApp)
    service.config = config
    service.metrics = FakeMetrics()
    service._root_graph = root_graph or FakeRootGraph()
    service.rate_limiter = FakeRateLimiter()
    service.ready = True
    service.draining = False
    service._mcp_app = mcp_app
    return service


async def call_mcp_asgi(service: MCPHttpApp) -> list[dict[str, Any]]:
    messages = [
        {
            "type": "http.request",
            "body": b'{"method":"tools/call","params":{"name":"query_graph"}}',
            "more_body": False,
        }
    ]
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        if messages:
            return messages.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [(b"x-api-key", b"test-key")],
        "client": ("127.0.0.1", 12345),
    }
    await service.mcp_asgi(scope, receive, send)
    return sent


def test_mcp_asgi_does_not_send_error_response_after_stream_started() -> None:
    async def started_then_failed(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise ServiceUnavailableError("stream failed")

    sent = anyio.run(call_mcp_asgi, make_http_service(started_then_failed))

    response_starts = [message for message in sent if message["type"] == "http.response.start"]
    assert response_starts == [{"type": "http.response.start", "status": 200, "headers": []}]


def test_mcp_asgi_sends_error_response_before_stream_started() -> None:
    async def failed_before_start(scope: Any, receive: Any, send: Any) -> None:
        raise ServiceUnavailableError("not started")

    sent = anyio.run(call_mcp_asgi, make_http_service(failed_before_start))

    response_starts = [message for message in sent if message["type"] == "http.response.start"]
    assert len(response_starts) == 1
    assert response_starts[0]["status"] == 503


def test_mcp_asgi_authentication_does_not_block_event_loop() -> None:
    root_graph = SlowRootGraph()

    async def ok_app(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def exercise() -> None:
        progressed_during_auth = False
        service = make_http_service(ok_app, root_graph=root_graph)

        async def observe_loop_progress() -> None:
            nonlocal progressed_during_auth
            await anyio.to_thread.run_sync(root_graph.started.wait)
            await anyio.sleep(0)
            progressed_during_auth = not root_graph.finished.is_set()

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(call_mcp_asgi, service)
            task_group.start_soon(observe_loop_progress)

        assert progressed_during_auth

    anyio.run(exercise)


def test_mcp_http_live_allowlist_excludes_testserver(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class FakeServer:
        def streamable_http_app(self, **kwargs: Any) -> Any:
            captured.update(kwargs)

            async def app(scope: Any, receive: Any, send: Any) -> None:
                pass

            app.router = SimpleNamespace(lifespan_context=lambda app: None)
            return app

    def fake_build_waggle_server(**kwargs: Any) -> tuple[FakeServer, Any]:
        return FakeServer(), object()

    monkeypatch.setattr(mcp_http, "build_waggle_server", fake_build_waggle_server)
    config = AppConfig.from_env()
    config.api_key_environment = "live"

    MCPHttpApp(root_graph=object(), config=config, metrics=FakeMetrics())

    settings = captured["transport_security"]
    assert "testserver" not in settings.allowed_hosts
    assert "http://testserver" not in settings.allowed_origins
