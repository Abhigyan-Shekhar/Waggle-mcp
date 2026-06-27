from __future__ import annotations

import base64
import json
import logging
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
from mcp.server.streamable_http import StreamableHTTPServerTransport
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from waggle.abhi import (
    abhi_to_snapshot,
    build_abhi_document,
    execute_abhi_query,
    load_abhi_document,
    validate_abhi_document,
)
from waggle.config import AppConfig
from waggle.errors import (
    AuthenticationError,
    PayloadTooLargeError,
    ServiceUnavailableError,
    ValidationFailure,
    WaggleError,
)
from waggle.graph_ui import render_graph_editor_html
from waggle.models import (
    NodeType,
    RelationType,
)
from waggle.rate_limit import RateLimiter
from waggle.runtime_context import runtime_context

from .mcp import WaggleServer
from .utils import (
    WRITE_HEAVY_TOOLS,
    _serialize_audit_event,
    _serialize_retention_policy,
    _serialize_retention_run,
)

LOGGER = logging.getLogger(__name__)


class MCPHttpApp:
    def __init__(self, app_server: WaggleServer, config: AppConfig) -> None:
        self.app_server = app_server
        self.config = config
        self.metrics = app_server.metrics
        self.rate_limiter = RateLimiter(
            requests_per_minute=config.rate_limit_rpm,
            max_concurrent_requests=config.max_concurrent_requests,
            write_requests_per_minute=config.write_rate_limit_rpm,
        )
        self.transport: StreamableHTTPServerTransport | None = None
        self.ready = False
        self.draining = False

    @asynccontextmanager
    async def lifespan(self, app: Starlette):
        self.transport = StreamableHTTPServerTransport(mcp_session_id=None, is_json_response_enabled=False)
        em = self.app_server._root_graph.embedding_model
        if (
            not self.config.is_fast_mode
            and hasattr(em, "start_background_warmup")
            and not getattr(em, "_warmup_started", False)
        ):
            em.start_background_warmup()
        async with self.transport.connect() as (read_stream, write_stream), anyio.create_task_group() as tg:
            tg.start_soon(
                self.app_server.server.run,
                read_stream,
                write_stream,
                self.app_server.initialization_options(),
                False,
                True,
            )
            self.app_server.validate_startup()
            self.ready = True
            self.metrics.set_gauge("waggle_ready", 1)
            app.state.http_service = self
            try:
                yield
            finally:
                self.draining = True
                self.ready = False
                self.metrics.set_gauge("waggle_ready", 0)
                tg.cancel_scope.cancel()

    async def mcp_asgi(self, scope: Any, receive: Any, send: Any) -> None:
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
                    key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers", [])
                }

            raw_api_key = headers.get("x-api-key", "")
            if not raw_api_key:
                raise AuthenticationError("Missing X-API-Key header.")
            principal = self.app_server._root_graph.authenticate_api_key(raw_api_key)
            scope.setdefault("state", {})
            scope["state"]["tenant_id"] = principal.tenant_id
            scope["state"]["api_key_id"] = principal.api_key_id
            scope["state"]["request_id"] = request_id
            tenant_graph = self.app_server._root_graph.for_tenant(principal.tenant_id)
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
                metadata={"method": method, "tool_name": self._extract_tool_name(body)},
            )

            tool_name = self._extract_tool_name(body)
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
                        assert self.transport is not None
                        await self.transport.handle_request(scope, receive_callable, send_wrapper)
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


class _RequestBodySizeMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        content_length = headers.get("content-length")
        if content_length is not None:
            try:
                length = int(content_length)
            except (ValueError, TypeError):
                length = 0
            if length > self.max_bytes:
                response = Response(
                    f"Request body exceeds maximum allowed size of {self.max_bytes} bytes.",
                    status_code=413,
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_http_application(app_server: WaggleServer, config: AppConfig) -> Starlette:
    service = MCPHttpApp(app_server, config)

    async def waggle_error_handler(request: Request, exc: WaggleError) -> Response:
        if isinstance(exc, AuthenticationError):
            service.metrics.increment("waggle_auth_failures_total")
        if exc.code == "rate_limited":
            service.metrics.increment("waggle_rate_limit_rejections_total")
        return JSONResponse({"error": exc.code, "message": str(exc)}, status_code=exc.status_code)

    async def live(_: Request) -> Response:
        return JSONResponse({"status": "live"})

    async def ready(request: Request) -> Response:
        http_service: MCPHttpApp = request.app.state.http_service
        if not http_service.ready or http_service.draining:
            return JSONResponse({"status": "not-ready"}, status_code=503)
        return JSONResponse({"status": "ready"})

    async def metrics_endpoint(request: Request) -> Response:
        return PlainTextResponse(request.app.state.http_service.metrics.render_prometheus())

    def _scope_from_request(request: Request) -> dict[str, str]:
        return {
            "project": request.query_params.get("project", "").strip(),
            "agent_id": request.query_params.get("agent_id", "").strip(),
            "session_id": request.query_params.get("session_id", "").strip(),
        }

    def _graph_from_request(request: Request, *, tenant_override: str = "") -> tuple[Any, Any | None]:
        raw_api_key = request.headers.get("x-api-key", "").strip()
        if raw_api_key:
            principal = app_server._root_graph.authenticate_api_key(raw_api_key)
            return app_server._root_graph.for_tenant(principal.tenant_id), principal
        tenant_id = (
            tenant_override.strip() or request.query_params.get("tenant_id", "").strip() or config.default_tenant_id
        )
        return app_server.graph.for_tenant(tenant_id), None

    def _emit_http_audit(
        request: Request,
        *,
        event_type: str,
        resource_type: str,
        resource_id: str = "",
        action: str = "",
        metadata: dict[str, Any] | None = None,
        tenant_override: str = "",
    ) -> None:
        try:
            graph, principal = _graph_from_request(request, tenant_override=tenant_override)
        except AuthenticationError:
            graph = app_server.graph.for_tenant(tenant_override.strip() or config.default_tenant_id)
            principal = None
        graph.emit_audit_event(
            event_type=event_type,
            actor_type="api_key" if principal is not None else "admin",
            actor_id=(principal.name or principal.api_key_id) if principal is not None else "local-http",
            api_key_id=principal.api_key_id if principal is not None else "",
            resource_type=resource_type,
            resource_id=resource_id,
            action=action or event_type,
            ip_address=request.client.host if request.client else "",
            user_agent=request.headers.get("user-agent", ""),
            metadata=metadata or {},
        )

    def _require_http_scope(
        request: Request, required_scope: str, *, tenant_override: str = ""
    ) -> tuple[Any, Any | None]:
        graph, principal = _graph_from_request(request, tenant_override=tenant_override)
        if principal is not None:
            principal.require_scope(required_scope)
        return graph, principal

    def _build_scoped_abhi(graph: Any, scope: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
        snapshot = graph.get_graph_snapshot(**scope)
        return snapshot, build_abhi_document(snapshot)

    def _validate_live_snapshot(snapshot: dict[str, Any]) -> None:
        validation = validate_abhi_document(build_abhi_document(snapshot), input_path="live://graph")
        blocking_errors = [
            error
            for error in validation.errors
            if "cannot originate from node type" not in error and "cannot target node type" not in error
        ]
        if blocking_errors:
            raise ValidationFailure("; ".join(blocking_errors))

    def _node_snapshot_payload(
        *,
        snapshot: dict[str, Any],
        payload: dict[str, Any],
        existing: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = existing or {}
        now = datetime.now(UTC).isoformat()
        return {
            "id": str(current.get("id") or payload.get("id") or f"preview-{uuid.uuid4()}").strip(),
            "tenant_id": str(snapshot.get("tenant_id", "")),
            "agent_id": str(payload.get("agent_id", current.get("agent_id", "")) or current.get("agent_id", "")),
            "project": str(payload.get("project", current.get("project", "")) or current.get("project", "")),
            "session_id": str(
                payload.get("session_id", current.get("session_id", "")) or current.get("session_id", "")
            ),
            "context_window_id": current.get("context_window_id"),
            "label": str(payload.get("label", current.get("label", "")) or current.get("label", "")).strip(),
            "content": str(payload.get("content", current.get("content", "")) or current.get("content", "")).strip(),
            "node_type": str(
                payload.get("node_type", current.get("node_type", "note")) or current.get("node_type", "note")
            ).strip(),
            "tags": payload.get("tags", current.get("tags", [])) or [],
            "source_prompt": current.get("source_prompt", ""),
            "metadata": current.get("metadata", {}),
            "evidence_records": current.get("evidence_records", []),
            "valid_from": current.get("valid_from"),
            "valid_to": current.get("valid_to"),
            "created_at": current.get("created_at", now),
            "updated_at": now,
            "access_count": current.get("access_count", 0),
        }

    def _edge_snapshot_payload(
        *,
        snapshot: dict[str, Any],
        payload: dict[str, Any],
        existing: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = existing or {}
        now = datetime.now(UTC).isoformat()
        return {
            "id": str(current.get("id") or payload.get("id") or f"preview-{uuid.uuid4()}").strip(),
            "tenant_id": str(snapshot.get("tenant_id", "")),
            "source_id": str(
                payload.get("source_id", current.get("source_id", "")) or current.get("source_id", "")
            ).strip(),
            "target_id": str(
                payload.get("target_id", current.get("target_id", "")) or current.get("target_id", "")
            ).strip(),
            "relationship": str(
                payload.get("relationship", current.get("relationship", "")) or current.get("relationship", "")
            ).strip(),
            "weight": float(payload.get("weight", current.get("weight", 1.0)) or current.get("weight", 1.0)),
            "metadata": current.get("metadata", {}),
            "created_at": current.get("created_at", now),
        }

    def _json_safe_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
        safe = deepcopy(snapshot)
        for node in safe.get("nodes", []):
            node.pop("embedding", None)
        for transcript in safe.get("transcripts", []):
            transcript.pop("embedding", None)
        return safe

    async def graph_editor(request: Request) -> Response:
        mode = request.query_params.get("mode", "edit").strip().lower()
        if mode not in {"edit", "view"}:
            mode = "edit"
        scope = _scope_from_request(request)
        return HTMLResponse(
            render_graph_editor_html(
                mode=mode,
                project=scope["project"],
                agent_id=scope["agent_id"],
                session_id=scope["session_id"],
            )
        )

    async def graph_snapshot(request: Request) -> Response:
        scope = _scope_from_request(request)
        graph, _ = _require_http_scope(request, "graph:read")
        include_source_prompt = request.query_params.get("include_source_prompt", "").strip().lower() in {
            "1",
            "true",
            "yes",
        }
        try:
            snapshot = graph.get_graph_snapshot(include_source_prompt=include_source_prompt, **scope)
        except TypeError:
            snapshot = graph.get_graph_snapshot(**scope)
        _emit_http_audit(
            request,
            event_type="graph.snapshot.read",
            resource_type="graph_snapshot",
            action="read",
            metadata={"project": scope["project"], "agent_id": scope["agent_id"], "session_id": scope["session_id"]},
        )
        return JSONResponse(
            {
                "tenant_id": snapshot.get("tenant_id", ""),
                "schema_version": snapshot.get("schema_version", 1),
                "nodes": snapshot.get("nodes", []),
                "edges": snapshot.get("edges", []),
                "ui": snapshot.get("ui", {}),
                "node_types": [node_type.value for node_type in NodeType],
                "relation_types": [relation.value for relation in RelationType],
            }
        )

    async def graph_transcripts(request: Request) -> Response:
        scope = _scope_from_request(request)
        try:
            limit = int(request.query_params.get("limit", "200") or "200")
            offset = int(request.query_params.get("offset", "0") or "0")
        except ValueError:
            raise ValidationFailure("limit and offset query parameters must be integers.")
        query_text = request.query_params.get("query", "").strip()
        graph, _ = _require_http_scope(request, "graph:read")
        if query_text and hasattr(graph, "search_transcript_records"):
            hits = graph.search_transcript_records(query=query_text, limit=limit, **scope)
            _emit_http_audit(
                request,
                event_type="record.read",
                resource_type="transcript_records",
                action="read",
                metadata={"mode": "hybrid", "query_length": len(query_text), "limit": limit},
            )
            return JSONResponse(
                {
                    "mode": "hybrid",
                    "query": query_text,
                    "hits": [hit.model_dump(mode="json") for hit in hits],
                }
            )
        if not hasattr(graph, "list_transcript_records"):
            raise ValidationFailure("Transcript listing is not available in this backend.")
        records = graph.list_transcript_records(limit=limit, offset=offset, **scope)
        total_count = graph.count_transcript_records(**scope)
        _emit_http_audit(
            request,
            event_type="record.read",
            resource_type="transcript_records",
            action="read",
            metadata={"mode": "chronological", "limit": limit, "offset": offset},
        )
        return JSONResponse(
            {
                "mode": "chronological",
                "records": [record.model_dump(mode="json") for record in records],
                "pagination": {
                    "offset": offset,
                    "limit": limit,
                    "total_count": total_count,
                },
            }
        )

    async def graph_retrieval_debug(request: Request) -> Response:
        payload = await request.json()
        scope = {
            "project": str(payload.get("project", "")).strip(),
            "agent_id": str(payload.get("agent_id", "")).strip(),
            "session_id": str(payload.get("session_id", "")).strip(),
        }
        query_text = str(payload.get("query", "")).strip()
        if not query_text:
            raise ValidationFailure("query is required.")
        try:
            max_nodes = int(payload.get("max_nodes", 8) or 8)
            max_depth = int(payload.get("max_depth", 1) or 1)
        except ValueError:
            raise ValidationFailure("max_nodes and max_depth must be integers.")
        graph, _ = _require_http_scope(request, "graph:read")
        debug = graph.debug_retrieval(
            query=query_text, max_nodes=max_nodes, max_depth=max_depth, retrieval_mode="hybrid", **scope
        )
        fusion = graph.query(
            query=query_text,
            retrieval_mode="hybrid",
            max_nodes=max_nodes,
            max_depth=max_depth,
            **scope,
        )
        token_estimate = (
            sum(
                max(1, len((hit.content if hasattr(hit, "content") else "") or "").split())
                for hit in fusion.fusion_hits
            )
            * 1.33
        )
        fused_ranking: list[dict[str, Any]] = []
        for hit in fusion.fusion_hits:
            reasoning: list[str] = []
            if hit.source_lane == "graph":
                reasoning.append("semantic graph node")
            if hit.graph_rank is not None:
                reasoning.append(f"graph rank {hit.graph_rank}")
            if hit.replay_rank is not None:
                reasoning.append(f"replay rank {hit.replay_rank}")
            if hit.session_id:
                reasoning.append(f"session {hit.session_id}")
            fused_ranking.append(
                {
                    "content": hit.content,
                    "score": hit.score,
                    "source_lane": hit.source_lane,
                    "graph_rank": hit.graph_rank,
                    "replay_rank": hit.replay_rank,
                    "fused_rank": hit.fused_rank,
                    "node_id": hit.node_id,
                    "session_id": hit.session_id,
                    "turn_index": hit.turn_index,
                    "transcript_snippet": hit.transcript_snippet,
                    "reasoning": ", ".join(reasoning) or "ranked by reciprocal-rank fusion",
                }
            )
        _emit_http_audit(
            request,
            event_type="graph.query.executed",
            resource_type="retrieval_debug",
            action="read",
            metadata={"query_length": len(query_text), "max_nodes": max_nodes, "max_depth": max_depth},
        )
        return JSONResponse(
            {
                "debug": debug,
                "replay_hits": [hit.model_dump(mode="json") for hit in fusion.replay_hits],
                "fusion_hits": fused_ranking,
                "token_estimate": int(token_estimate),
            }
        )

    async def graph_abhi_preview(request: Request) -> Response:
        scope = _scope_from_request(request)
        graph, _ = _require_http_scope(request, "graph:read")
        snapshot, document = _build_scoped_abhi(graph, scope)
        validation = validate_abhi_document(document, input_path="live://graph")
        _emit_http_audit(
            request,
            event_type="export.previewed",
            resource_type="abhi_preview",
            action="read",
            metadata={"project": scope["project"], "agent_id": scope["agent_id"], "session_id": scope["session_id"]},
        )
        return JSONResponse(
            {
                "tenant_id": snapshot.get("tenant_id", ""),
                "scope": scope,
                "schema": document.get("schema", {}),
                "constraints": document.get("constraints", []),
                "queries": document.get("queries", {}),
                "events": document.get("events", {}),
                "versions": document.get("versions", []),
                "integrity": document.get("integrity", {}),
                "validation": validation.model_dump(mode="json"),
            }
        )

    async def graph_query(request: Request) -> Response:
        payload = await request.json()
        graph, _ = _require_http_scope(request, "graph:read")
        scope = {
            "project": str(payload.get("project", "")).strip(),
            "agent_id": str(payload.get("agent_id", "")).strip(),
            "session_id": str(payload.get("session_id", "")).strip(),
        }
        _, document = _build_scoped_abhi(graph, scope)
        result = execute_abhi_query(
            document,
            query_id=str(payload.get("query_id", "")).strip(),
            query_text=str(payload.get("query", "")).strip(),
        )
        _emit_http_audit(
            request,
            event_type="graph.query.executed",
            resource_type="abhi_query",
            action="read",
            metadata={
                "query_id": str(payload.get("query_id", "")).strip(),
                "query_length": len(str(payload.get("query", "")).strip()),
            },
        )
        return JSONResponse(result)

    async def graph_diff_feed(request: Request) -> Response:
        since = request.query_params.get("since", "24h").strip() or "24h"
        graph, _ = _require_http_scope(request, "graph:read")
        diff = graph.graph_diff(since=since)
        _emit_http_audit(
            request,
            event_type="graph.diff.read",
            resource_type="graph_diff",
            action="read",
            metadata={"since": since},
        )
        return JSONResponse(
            {
                "since": diff.since,
                "added_nodes": [node.model_dump(mode="json") for node in diff.added_nodes],
                "updated_nodes": [node.model_dump(mode="json") for node in diff.updated_nodes],
                "created_edges": [edge.model_dump(mode="json") for edge in diff.created_edges],
                "contradiction_edges": [edge.model_dump(mode="json") for edge in diff.contradiction_edges],
            }
        )

    async def graph_save_ui(request: Request) -> Response:
        payload = await request.json()
        graph, _ = _require_http_scope(request, "graph:write")
        zoom_val = None
        if "zoom" in payload and payload.get("zoom") is not None:
            try:
                zoom_val = float(payload["zoom"])
            except ValueError:
                raise ValidationFailure("zoom must be a float number.")
        saved = graph.save_ui_state(
            project=str(payload.get("project", "")).strip(),
            agent_id=str(payload.get("agent_id", "")).strip(),
            session_id=str(payload.get("session_id", "")).strip(),
            positions=payload.get("positions"),
            zoom=zoom_val,
            viewport=payload.get("viewport"),
            groups=payload.get("groups"),
            collapsed_groups=payload.get("collapsed_groups"),
            selected_nodes=payload.get("selected_nodes"),
        )
        return JSONResponse(saved)

    async def graph_restore(request: Request) -> Response:
        payload = await request.json()
        scope = {
            "project": str(payload.get("project", "")).strip(),
            "agent_id": str(payload.get("agent_id", "")).strip(),
            "session_id": str(payload.get("session_id", "")).strip(),
        }
        graph, _ = _require_http_scope(request, "graph:write")
        current = graph.get_graph_snapshot(**scope)
        desired_nodes = {
            str(node.get("id", "")).strip(): node
            for node in payload.get("nodes", [])
            if str(node.get("id", "")).strip()
        }
        desired_edges = {
            str(edge.get("id", "")).strip(): edge
            for edge in payload.get("edges", [])
            if str(edge.get("id", "")).strip()
        }
        current_nodes = {
            str(node.get("id", "")).strip(): node
            for node in current.get("nodes", [])
            if str(node.get("id", "")).strip()
        }
        current_edges = {
            str(edge.get("id", "")).strip(): edge
            for edge in current.get("edges", [])
            if str(edge.get("id", "")).strip()
        }

        # Validation Pass
        ui = payload.get("ui", {}) or {}
        zoom_val = None
        if "zoom" in ui and ui.get("zoom") is not None:
            try:
                zoom_val = float(ui["zoom"])
            except ValueError:
                raise ValidationFailure("zoom must be a float number.")

        for node_id, node_payload in desired_nodes.items():
            if node_id not in current_nodes:
                try:
                    NodeType(str(node_payload.get("node_type", "note")).strip() or "note")
                except ValueError:
                    raise ValidationFailure(f"node_type must be one of {[nt.value for nt in NodeType]}.")

        parsed_weights = {}
        for edge_id, edge_payload in desired_edges.items():
            if edge_id in current_edges:
                try:
                    weight_val = (
                        float(edge_payload["weight"])
                        if "weight" in edge_payload and edge_payload.get("weight") is not None
                        else None
                    )
                    parsed_weights[edge_id] = weight_val
                except ValueError:
                    raise ValidationFailure("weight must be a float number.")
            else:
                try:
                    weight_val = float(edge_payload.get("weight", 1.0))
                    parsed_weights[edge_id] = weight_val
                except ValueError:
                    raise ValidationFailure("weight must be a float number.")

        # Mutation Pass
        for edge_id in list(current_edges):
            if edge_id not in desired_edges:
                graph.delete_edge(edge_id=edge_id)

        for node_id in list(current_nodes):
            if node_id not in desired_nodes:
                graph.delete_node(node_id=node_id)

        for node_id, node_payload in desired_nodes.items():
            tags = [str(tag).strip() for tag in node_payload.get("tags", []) if str(tag).strip()]
            if node_id in current_nodes:
                graph.update_node(
                    node_id=node_id,
                    label=node_payload.get("label"),
                    content=node_payload.get("content"),
                    tags=tags,
                )
                continue
            graph.add_node(
                node_id=node_id,
                label=str(node_payload.get("label", "")).strip(),
                content=str(node_payload.get("content", "")).strip(),
                node_type=NodeType(str(node_payload.get("node_type", "note")).strip() or "note"),
                tags=tags,
                agent_id=str(node_payload.get("agent_id", scope["agent_id"])).strip(),
                project=str(node_payload.get("project", scope["project"])).strip(),
                session_id=str(node_payload.get("session_id", scope["session_id"])).strip(),
            )

        for edge_id, edge_payload in desired_edges.items():
            if edge_id in current_edges:
                graph.update_edge(
                    edge_id=edge_id,
                    source_id=str(edge_payload.get("source_id", "")).strip() or None,
                    target_id=str(edge_payload.get("target_id", "")).strip() or None,
                    relationship=str(edge_payload.get("relationship", "")).strip() or None,
                    weight=parsed_weights[edge_id],
                )
                continue
            edge_id_to_use = str(edge_payload.get("id") or f"preview-{uuid.uuid4()}").strip()
            graph.add_edge(
                edge_id=edge_id_to_use,
                source_id=str(edge_payload.get("source_id", "")).strip(),
                target_id=str(edge_payload.get("target_id", "")).strip(),
                relationship=str(edge_payload.get("relationship", "")).strip(),
                weight=parsed_weights[edge_id],
            )

        saved = graph.save_ui_state(
            project=scope["project"],
            agent_id=scope["agent_id"],
            session_id=scope["session_id"],
            positions=ui.get("positions"),
            zoom=zoom_val,
            viewport=ui.get("viewport"),
            groups=ui.get("groups"),
            collapsed_groups=ui.get("collapsed_groups"),
            selected_nodes=ui.get("selected_nodes"),
        )
        restored = graph.get_graph_snapshot(**scope)
        return JSONResponse(
            {
                "nodes": restored.get("nodes", []),
                "edges": restored.get("edges", []),
                "ui": saved,
            }
        )

    async def graph_create_node(request: Request) -> Response:
        payload = await request.json()
        graph, _ = _require_http_scope(request, "graph:write")
        scope = {
            "project": str(payload.get("project", "")).strip(),
            "agent_id": str(payload.get("agent_id", "")).strip(),
            "session_id": str(payload.get("session_id", "")).strip(),
        }
        snapshot = graph.get_graph_snapshot(**scope)
        snapshot["nodes"] = [*snapshot.get("nodes", []), _node_snapshot_payload(snapshot=snapshot, payload=payload)]
        _validate_live_snapshot(snapshot)
        created = graph.add_node(
            node_id=str(payload.get("id", "")).strip() or None,
            label=str(payload.get("label", "")).strip(),
            content=str(payload.get("content", "")).strip(),
            node_type=NodeType(str(payload.get("node_type", "note")).strip() or "note"),
            tags=[str(tag).strip() for tag in payload.get("tags", []) if str(tag).strip()],
            agent_id=str(payload.get("agent_id", "")).strip(),
            project=str(payload.get("project", "")).strip(),
            session_id=str(payload.get("session_id", "")).strip(),
        )
        return JSONResponse(created.node.model_dump(mode="json"))

    async def graph_update_node(request: Request) -> Response:
        node_id = request.path_params["node_id"]
        payload = await request.json()
        graph, _ = _require_http_scope(request, "graph:write")
        snapshot = graph.get_graph_snapshot()
        existing = next(
            (node for node in snapshot.get("nodes", []) if str(node.get("id", "")).strip() == node_id), None
        )
        if existing is None:
            raise ValidationFailure(f"Node not found: {node_id}")
        snapshot["nodes"] = [
            _node_snapshot_payload(snapshot=snapshot, payload=payload, existing=existing)
            if str(node.get("id", "")).strip() == node_id
            else node
            for node in snapshot.get("nodes", [])
        ]
        _validate_live_snapshot(snapshot)
        updated = graph.update_node(
            node_id=node_id,
            label=payload.get("label"),
            content=payload.get("content"),
            tags=payload.get("tags"),
        )
        return JSONResponse(updated.model_dump(mode="json"))

    async def graph_delete_node(request: Request) -> Response:
        node_id = request.path_params["node_id"]
        graph, _ = _require_http_scope(request, "graph:write")
        deleted = graph.delete_node(node_id=node_id)
        return JSONResponse(deleted.model_dump(mode="json"))

    async def graph_create_edge(request: Request) -> Response:
        payload = await request.json()
        graph, _ = _require_http_scope(request, "graph:write")
        raw_weight = payload.get("weight", 1.0)
        try:
            weight_value = float(raw_weight)
        except (TypeError, ValueError):
            raise ValidationFailure("Edge weight must be a numeric value between 0 and 1.")
        if not (0 <= weight_value <= 1):
            raise ValidationFailure("Edge weight must be a numeric value between 0 and 1.")
        snapshot = graph.get_graph_snapshot()
        snapshot["edges"] = [*snapshot.get("edges", []), _edge_snapshot_payload(snapshot=snapshot, payload=payload)]
        _validate_live_snapshot(snapshot)
        edge = graph.add_edge(
            edge_id=str(payload.get("id", "")).strip() or None,
            source_id=str(payload.get("source_id", "")).strip(),
            target_id=str(payload.get("target_id", "")).strip(),
            relationship=str(payload.get("relationship", "")).strip(),
            weight=float(payload.get("weight", 1.0)),
        )
        return JSONResponse(edge.model_dump(mode="json"))

    async def graph_update_edge(request: Request) -> Response:
        edge_id = request.path_params["edge_id"]
        payload = await request.json()
        graph, _ = _require_http_scope(request, "graph:write")
        if "weight" in payload and payload.get("weight") is not None:
            try:
                weight_value = float(payload["weight"])
            except (TypeError, ValueError):
                raise ValidationFailure("Edge weight must be a numeric value between 0 and 1.")
            if not (0 <= weight_value <= 1):
                raise ValidationFailure("Edge weight must be a numeric value between 0 and 1.")
        snapshot = graph.get_graph_snapshot()
        existing = next(
            (item for item in snapshot.get("edges", []) if str(item.get("id", "")).strip() == edge_id), None
        )
        if existing is None:
            raise ValidationFailure(f"Edge not found: {edge_id}")
        snapshot["edges"] = [
            _edge_snapshot_payload(snapshot=snapshot, payload=payload, existing=existing)
            if str(item.get("id", "")).strip() == edge_id
            else item
            for item in snapshot.get("edges", [])
        ]
        _validate_live_snapshot(snapshot)

        edge = graph.update_edge(
            edge_id=edge_id,
            source_id=str(payload.get("source_id", "")).strip() or None,
            target_id=str(payload.get("target_id", "")).strip() or None,
            relationship=str(payload.get("relationship", "")).strip() or None,
            weight=float(payload["weight"]) if "weight" in payload and payload.get("weight") is not None else None,
        )
        return JSONResponse(edge.model_dump(mode="json"))

    async def graph_delete_edge(request: Request) -> Response:
        edge_id = request.path_params["edge_id"]
        graph, _ = _require_http_scope(request, "graph:write")
        edge = graph.delete_edge(edge_id=edge_id)
        return JSONResponse(edge.model_dump(mode="json"))

    async def graph_export(request: Request) -> Response:
        graph, _ = _require_http_scope(request, "graph:read")
        scope = _scope_from_request(request)
        export_format = request.query_params.get("format", "abhi").strip().lower()
        if export_format == "abhi":
            exported = graph.export_abhi(**scope)
            content = Path(exported.output_path).read_bytes()
            _emit_http_audit(
                request,
                event_type="export.downloaded",
                resource_type="abhi_export",
                resource_id=exported.output_path,
                action="download",
                metadata={"format": "abhi", "project": scope["project"]},
            )
            return Response(
                content,
                media_type="application/octet-stream",
                headers={"Content-Disposition": 'attachment; filename="waggle-memory.abhi"'},
            )
        if export_format == "json":
            snapshot = graph.get_graph_snapshot(**scope)
            _emit_http_audit(
                request,
                event_type="export.downloaded",
                resource_type="backup",
                action="download",
                metadata={"format": "json", "project": scope["project"]},
            )
            return Response(
                json.dumps(snapshot, indent=2),
                media_type="application/json",
                headers={"Content-Disposition": 'attachment; filename="waggle-backup.json"'},
            )
        raise ValidationFailure("format must be one of: abhi, json.")

    async def graph_import(request: Request) -> Response:
        graph, _ = _require_http_scope(request, "graph:write")
        payload = await request.json()
        import_format = str(payload.get("format", "abhi")).strip().lower()
        content = str(payload.get("content", ""))
        content_base64 = str(payload.get("content_base64", ""))
        suffix = ".abhi" if import_format == "abhi" else ".json"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
            temp_path = Path(handle.name)
        try:
            if import_format == "abhi" and content_base64:
                temp_path.write_bytes(base64.b64decode(content_base64))
            else:
                temp_path.write_text(content, encoding="utf-8")
            imported_node_ids: list[str] = []
            if import_format == "abhi":
                document = load_abhi_document(temp_path)
                imported_node_ids = [
                    str(node.get("id", "")).strip()
                    for node in abhi_to_snapshot(document, fallback_tenant_id=graph.tenant_id).get("nodes", [])
                    if str(node.get("id", "")).strip()
                ]
                imported = graph.import_abhi(input_path=temp_path)
            elif import_format == "json":
                snapshot = json.loads(content)
                imported_node_ids = [
                    str(node.get("id", "")).strip()
                    for node in snapshot.get("nodes", [])
                    if str(node.get("id", "")).strip()
                ]
                imported = graph.import_graph_backup(input_path=temp_path)
            else:
                raise ValidationFailure("format must be one of: abhi, json.")
            return JSONResponse({**imported.model_dump(mode="json"), "imported_node_ids": imported_node_ids})
        finally:
            temp_path.unlink(missing_ok=True)

    async def graph_import_preview(request: Request) -> Response:
        graph, _ = _require_http_scope(request, "graph:read")
        payload = await request.json()
        import_format = str(payload.get("format", "abhi")).strip().lower()
        content = str(payload.get("content", ""))
        content_base64 = str(payload.get("content_base64", ""))
        suffix = ".abhi" if import_format == "abhi" else ".json"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
            temp_path = Path(handle.name)
        try:
            if import_format == "abhi" and content_base64:
                temp_path.write_bytes(base64.b64decode(content_base64))
            else:
                temp_path.write_text(content, encoding="utf-8")
            if import_format == "abhi":
                validation = graph.validate_abhi(input_path=temp_path)
                inspect_result = graph.inspect_abhi(input_path=temp_path)
                document = load_abhi_document(temp_path)
                snapshot = abhi_to_snapshot(document, fallback_tenant_id=graph.tenant_id)
                return JSONResponse(
                    {
                        "validation": validation.model_dump(mode="json"),
                        "inspect": inspect_result.model_dump(mode="json"),
                        "snapshot": {
                            "tenant_id": snapshot.get("tenant_id", ""),
                            "nodes": _json_safe_snapshot(snapshot).get("nodes", []),
                            "edges": snapshot.get("edges", []),
                            "ui": snapshot.get("ui", {}),
                        },
                        "imported_node_ids": [
                            str(node.get("id", "")).strip()
                            for node in snapshot.get("nodes", [])
                            if str(node.get("id", "")).strip()
                        ],
                    }
                )
            if import_format == "json":
                snapshot = json.loads(content)
                return JSONResponse(
                    {
                        "validation": {"valid": True, "errors": []},
                        "inspect": {
                            "node_count": len(snapshot.get("nodes", [])),
                            "edge_count": len(snapshot.get("edges", [])),
                        },
                        "snapshot": snapshot,
                        "imported_node_ids": [
                            str(node.get("id", "")).strip()
                            for node in snapshot.get("nodes", [])
                            if str(node.get("id", "")).strip()
                        ],
                    }
                )
            raise ValidationFailure("format must be one of: abhi, json.")
        finally:
            temp_path.unlink(missing_ok=True)

    async def graph_abhi_diff(request: Request) -> Response:
        payload = await request.json()
        content_a = str(payload.get("content_a", ""))
        content_b = str(payload.get("content_b", ""))
        content_a_base64 = str(payload.get("content_a_base64", ""))
        content_b_base64 = str(payload.get("content_b_base64", ""))
        with tempfile.NamedTemporaryFile(suffix=".abhi", delete=False) as handle_a:
            path_a = Path(handle_a.name)
        with tempfile.NamedTemporaryFile(suffix=".abhi", delete=False) as handle_b:
            path_b = Path(handle_b.name)
        try:
            if content_a_base64:
                path_a.write_bytes(base64.b64decode(content_a_base64))
            else:
                path_a.write_text(content_a, encoding="utf-8")
            if content_b_base64:
                path_b.write_bytes(base64.b64decode(content_b_base64))
            else:
                path_b.write_text(content_b, encoding="utf-8")
            graph, _ = _require_http_scope(request, "graph:read")
            diff = graph.diff_abhi(input_path_a=path_a, input_path_b=path_b)
            snapshot_a = abhi_to_snapshot(load_abhi_document(path_a), fallback_tenant_id=graph.tenant_id)
            snapshot_b = abhi_to_snapshot(load_abhi_document(path_b), fallback_tenant_id=graph.tenant_id)
            return JSONResponse(
                {
                    "diff": diff.model_dump(mode="json"),
                    "left": {
                        "nodes": _json_safe_snapshot(snapshot_a).get("nodes", []),
                        "edges": snapshot_a.get("edges", []),
                    },
                    "right": {
                        "nodes": _json_safe_snapshot(snapshot_b).get("nodes", []),
                        "edges": snapshot_b.get("edges", []),
                    },
                }
            )
        finally:
            path_a.unlink(missing_ok=True)
            path_b.unlink(missing_ok=True)

    async def admin_retention_status(request: Request) -> Response:
        graph, _ = _require_http_scope(request, "admin:read")
        policy = graph.get_retention_policy(
            default_enabled=config.retention_enabled,
            default_retention_days=config.retention_days,
            default_prune_interval_hours=config.retention_prune_interval_hours,
        )
        payload = _serialize_retention_policy(policy)
        payload["recent_runs"] = [_serialize_retention_run(run) for run in graph.list_retention_runs(limit=5)]
        return JSONResponse(payload)

    async def admin_retention_update(request: Request) -> Response:
        payload = await request.json()
        graph, principal = _require_http_scope(
            request, "admin:write", tenant_override=str(payload.get("tenant_id", "") or "")
        )
        policy = graph.update_retention_policy(
            enabled=payload.get("enabled"),
            retention_days=payload.get("retention_days"),
            prune_interval_hours=payload.get("prune_interval_hours"),
            default_enabled=config.retention_enabled,
            default_retention_days=config.retention_days,
            default_prune_interval_hours=config.retention_prune_interval_hours,
        )
        graph.emit_audit_event(
            event_type="retention.policy.updated",
            actor_type="api_key" if principal is not None else "admin",
            actor_id=(principal.name or principal.api_key_id) if principal is not None else "local-http",
            api_key_id=principal.api_key_id if principal is not None else "",
            resource_type="retention_policy",
            resource_id=policy.tenant_id,
            action="update",
            metadata={
                "enabled": policy.enabled,
                "retention_days": policy.retention_days,
                "prune_interval_hours": policy.prune_interval_hours,
            },
            ip_address=request.client.host if request.client else "",
            user_agent=request.headers.get("user-agent", ""),
        )
        return JSONResponse(_serialize_retention_policy(policy))

    async def admin_retention_prune(request: Request) -> Response:
        payload = await request.json() if request.method != "GET" else {}
        graph, _ = _require_http_scope(request, "admin:write", tenant_override=str(payload.get("tenant_id", "") or ""))
        try:
            batch_size = int(payload.get("batch_size", 1000) or 1000)
        except ValueError:
            raise ValidationFailure("batch_size must be an integer.")
        run = graph.prune_retention(
            batch_size=batch_size,
            default_enabled=config.retention_enabled,
            default_retention_days=config.retention_days,
            default_prune_interval_hours=config.retention_prune_interval_hours,
        )
        response = _serialize_retention_run(run)
        response["policy"] = _serialize_retention_policy(
            graph.get_retention_policy(
                default_enabled=config.retention_enabled,
                default_retention_days=config.retention_days,
                default_prune_interval_hours=config.retention_prune_interval_hours,
            )
        )
        return JSONResponse(response)

    async def admin_retention_runs(request: Request) -> Response:
        graph, _ = _require_http_scope(request, "admin:read")
        try:
            limit = int(request.query_params.get("limit", "20") or "20")
        except ValueError:
            raise ValidationFailure("limit query parameter must be an integer.")
        runs = graph.list_retention_runs(limit=limit)
        return JSONResponse([_serialize_retention_run(run) for run in runs])

    async def admin_audit_events(request: Request) -> Response:
        graph, _ = _require_http_scope(request, "admin:read")
        try:
            limit = int(request.query_params.get("limit", "100") or "100")
        except ValueError:
            raise ValidationFailure("limit query parameter must be an integer.")
        events = graph.list_audit_events(
            limit=limit,
            event_type=request.query_params.get("type", "").strip(),
            actor_id=request.query_params.get("actor_id", "").strip(),
            resource_id=request.query_params.get("resource_id", "").strip(),
            resource_type=request.query_params.get("resource_type", "").strip(),
            status=request.query_params.get("status", "").strip(),
        )
        return JSONResponse([_serialize_audit_event(event) for event in events])

    raw_app = Starlette(
        routes=[
            Route("/health/live", live),
            Route("/health/ready", ready),
            Route("/metrics", metrics_endpoint),
            Route("/graph", graph_editor),
            Route("/api/graph", graph_snapshot, methods=["GET"]),
            Route("/api/graph/transcripts", graph_transcripts, methods=["GET"]),
            Route("/api/graph/retrieval-debug", graph_retrieval_debug, methods=["POST"]),
            Route("/api/graph/abhi", graph_abhi_preview, methods=["GET"]),
            Route("/api/graph/abhi/preview-import", graph_import_preview, methods=["POST"]),
            Route("/api/graph/abhi/diff", graph_abhi_diff, methods=["POST"]),
            Route("/api/graph/query", graph_query, methods=["POST"]),
            Route("/api/graph/diff", graph_diff_feed, methods=["GET"]),
            Route("/api/graph/ui", graph_save_ui, methods=["PATCH"]),
            Route("/api/graph/restore", graph_restore, methods=["POST"]),
            Route("/api/graph/nodes", graph_create_node, methods=["POST"]),
            Route("/api/graph/nodes/{node_id:str}", graph_update_node, methods=["PATCH"]),
            Route("/api/graph/nodes/{node_id:str}", graph_delete_node, methods=["DELETE"]),
            Route("/api/graph/edges", graph_create_edge, methods=["POST"]),
            Route("/api/graph/edges/{edge_id:str}", graph_update_edge, methods=["PATCH"]),
            Route("/api/graph/edges/{edge_id:str}", graph_delete_edge, methods=["DELETE"]),
            Route("/api/graph/export", graph_export, methods=["GET"]),
            Route("/api/graph/import", graph_import, methods=["POST"]),
            Route("/api/admin/retention", admin_retention_status, methods=["GET"]),
            Route("/api/admin/retention", admin_retention_update, methods=["PUT", "PATCH"]),
            Route("/api/admin/retention/prune", admin_retention_prune, methods=["POST"]),
            Route("/api/admin/retention/runs", admin_retention_runs, methods=["GET"]),
            Route("/api/admin/audit-events", admin_audit_events, methods=["GET"]),
            Mount("/graph-assets", app=StaticFiles(packages=[("waggle", "static/graph")], html=False, check_dir=False)),
            Mount("/mcp", app=service.mcp_asgi),
        ],
        lifespan=service.lifespan,
        exception_handlers={WaggleError: waggle_error_handler},
    )
    return _RequestBodySizeMiddleware(raw_app, max_bytes=config.max_payload_bytes)
