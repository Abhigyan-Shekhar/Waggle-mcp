"""HTTP transport service for Waggle's MCP SDK v2 server."""
from __future__ import annotations

import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

import anyio
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from waggle.config import AppConfig
from waggle.errors import (
    AuthenticationError,
    PayloadTooLargeError,
    ServiceUnavailableError,
    WaggleError,
)
from waggle.rate_limit import RateLimiter
from waggle.runtime_context import runtime_context
from waggle.server.utils import WRITE_HEAVY_TOOLS

from .server import build_waggle_server

LOGGER = logging.getLogger(__name__)


class MCPHttpApp:
    """Tenant/auth/rate-limit wrapper around the SDK v2 streamable HTTP app."""

    def __init__(self, root_graph: Any, config: AppConfig, metrics: Any) -> None:
        self.config = config
        self.metrics = metrics
        self._root_graph = root_graph
        self.rate_limiter = RateLimiter(
            requests_per_minute=config.rate_limit_rpm,
            max_concurrent_requests=config.max_concurrent_requests,
            write_requests_per_minute=config.write_rate_limit_rpm,
        )
        self.ready = False
        self.draining = False
        self.server, self.adapter = build_waggle_server(graph=root_graph, config=config, metrics=metrics)
        allowed_hosts = [config.http_host, f"{config.http_host}:*", "localhost", "localhost:*", "testserver"]
        if config.http_host == "0.0.0.0":
            allowed_hosts.extend(["127.0.0.1", "127.0.0.1:*"])
        self._mcp_app: ASGIApp = self.server.streamable_http_app(
            streamable_http_path="/",
            json_response=False,
            stateless_http=True,
            max_request_body_size=config.max_payload_bytes,
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=allowed_hosts,
                allowed_origins=[
                    f"http://{config.http_host}:*",
                    "http://localhost:*",
                    "http://127.0.0.1:*",
                    "http://testserver",
                ],
            ),
            host=config.http_host,
        )
        self._mcp_lifespan_context = self._mcp_app.router.lifespan_context  # type: ignore[attr-defined]

    @asynccontextmanager
    async def lifespan(self, app: Any):
        em = self._root_graph.embedding_model
        if (
            not self.config.is_fast_mode
            and hasattr(em, "start_background_warmup")
            and not getattr(em, "_warmup_started", False)
        ):
            em.start_background_warmup()
        async with self._mcp_lifespan_context(self._mcp_app):
            self._validate_startup()
            self.ready = True
            self.metrics.set_gauge("waggle_ready", 1)
            app.state.http_service = self
            try:
                yield
            finally:
                self.draining = True
                self.ready = False
                self.metrics.set_gauge("waggle_ready", 0)

    async def mcp_asgi(self, scope: Scope, receive: Receive, send: Send) -> None:
        started = time.perf_counter()
        method = scope["method"]
        headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers", [])}
        request_id = headers.get("x-request-id", str(uuid.uuid4()))
        status_holder = {"status": 500}

        async def send_wrapper(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = int(message["status"])
            await send(message)

        try:
            if self.draining:
                raise ServiceUnavailableError("Server is draining.")

            body = b""
            receive_callable = receive
            if method == "POST":
                request = Request(scope, receive)
                body = await request.body()
                if len(body) > self.config.max_payload_bytes:
                    raise PayloadTooLargeError()
                receive_callable = self._replay_receive(body)
                headers = {
                    key.decode("latin-1").lower(): value.decode("latin-1")
                    for key, value in scope.get("headers", [])
                }

            raw_api_key = headers.get("x-api-key", "")
            if not raw_api_key:
                raise AuthenticationError("Missing X-API-Key header.")
            principal = self._root_graph.authenticate_api_key(raw_api_key)
            scope.setdefault("state", {})
            scope["state"]["tenant_id"] = principal.tenant_id
            scope["state"]["api_key_id"] = principal.api_key_id
            scope["state"]["request_id"] = request_id

            tool_name = self._extract_tool_name(body)
            tenant_graph = self._root_graph.for_tenant(principal.tenant_id)
            tenant_graph.emit_audit_event(
                event_type="api_key.used",
                actor_type="api_key",
                actor_id=principal.name or principal.api_key_id,
                api_key_id=principal.api_key_id,
                resource_type="mcp_request",
                resource_id=request_id,
                action="use",
                ip_address=scope.get("client", ("", 0))[0] or "",
                user_agent=headers.get("user-agent", ""),
                metadata={"method": method, "tool_name": tool_name},
            )

            principal.require_scope("graph:write" if tool_name in WRITE_HEAVY_TOOLS else "graph:read")
            await self.rate_limiter.check_rate(principal.api_key_id, is_write=tool_name in WRITE_HEAVY_TOOLS)
            async with self.rate_limiter.concurrency_slot(principal.api_key_id):
                with runtime_context(
                    request_id=request_id,
                    tenant_id=principal.tenant_id,
                    transport="http",
                    backend=self.config.backend,
                    api_key_id=principal.api_key_id,
                    tool_name=tool_name,
                ):
                    with anyio.fail_after(self.config.request_timeout_seconds):
                        await self._mcp_app(scope, receive_callable, send_wrapper)
        except TimeoutError:
            LOGGER.warning("http_request_timeout", extra={"timeout": self.config.request_timeout_seconds})
            self.metrics.increment("waggle_http_timeouts_total")
            await JSONResponse({"error": "gateway_timeout", "message": "Request timed out."}, status_code=504)(
                scope, receive, send
            )
            status_holder["status"] = 504
        except WaggleError as exc:
            LOGGER.warning("http_request_failed", extra={"error_code": exc.code, "status_code": exc.status_code})
            if isinstance(exc, AuthenticationError):
                self.metrics.increment("waggle_auth_failures_total")
            if exc.code == "rate_limited":
                self.metrics.increment("waggle_rate_limit_rejections_total")
            await JSONResponse({"error": exc.code, "message": str(exc)}, status_code=exc.status_code)(
                scope, receive, send
            )
            status_holder["status"] = exc.status_code
        finally:
            elapsed = time.perf_counter() - started
            self.metrics.increment(
                "waggle_http_requests_total",
                path="/mcp",
                method=method,
                status=str(status_holder["status"]),
            )
            self.metrics.observe("waggle_http_request_latency_seconds", elapsed, path="/mcp", method=method)

    def _validate_startup(self) -> None:
        graph = self._root_graph.for_tenant(self.config.default_tenant_id)
        started = time.perf_counter()
        graph.ensure_tenant(graph.tenant_id)
        if self.config.api_key_environment == "live" and self.config.default_tenant_id == "local-default":
            LOGGER.warning(
                "WAGGLE_API_KEY_ENVIRONMENT is set to 'live' but "
                "WAGGLE_DEFAULT_TENANT_ID is still 'local-default'. "
                "Production deployments should use a unique tenant ID."
            )
        self.metrics.observe(
            "waggle_startup_validation_seconds",
            time.perf_counter() - started,
            backend=self.config.backend,
        )

    @staticmethod
    def _extract_tool_name(body: bytes) -> str:
        if not body:
            return ""
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return ""
        params = payload.get("params", {})
        if isinstance(params, dict):
            return str(params.get("name", ""))
        return ""

    @staticmethod
    def _replay_receive(body: bytes):
        delivered = False

        async def receive() -> dict[str, Any]:
            nonlocal delivered
            if delivered:
                return {"type": "http.disconnect"}
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}

        return receive
