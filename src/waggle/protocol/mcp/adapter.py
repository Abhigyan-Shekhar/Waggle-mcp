"""MCP 2026-07-28 adapter — translate between SDK v2 wire types and Waggle internals.

This module has exactly one responsibility: given an inbound ``ServerRequestContext``
and ``params`` from the MCP SDK v2, build the Waggle-internal types (``WaggleRequestContext``,
``WaggleToolResult``), pass them to ``WaggleToolDispatcher``, and translate results
back to MCP v2 wire types.

No graph access.  No config reading.  Just translation.
"""

from __future__ import annotations

import anyio
import mcp.types as types

from waggle.tools.context import WaggleRequestContext
from waggle.tools.dispatcher import WaggleToolDispatcher
from waggle.tools.results import WaggleToolResult

from .surface import build_prompts, build_resources, get_prompt_result, read_resource_text


class WagglemcpAdapter:
    """Translates MCP SDK v2 handler calls into ``WaggleToolDispatcher`` calls.

    One instance is created per ``build_waggle_server()`` call and shared
    across all handler closures captured by the ``Server`` constructor.

    The adapter is transport-agnostic: it derives the transport name from
    ctx.headers being present (HTTP) or absent (stdio/other).
    """

    def __init__(self, dispatcher: WaggleToolDispatcher) -> None:
        self._dispatcher = dispatcher

    @property
    def graph(self) -> object:
        """Return the adapter's current root graph."""
        return self._dispatcher._graph

    # ── MCP v2 handler implementations ───────────────────────────────────

    async def on_list_tools(
        self,
        ctx: object,
        params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        """Return the complete Waggle tool catalogue as MCP v2 ``Tool`` objects."""
        del ctx, params  # unused — catalogue is static per config
        tools = [
            types.Tool(
                name=d.name,
                title=d.title,
                description=d.description,
                input_schema=d.input_schema,  # snake_case in SDK v2
                annotations=types.ToolAnnotations(**(d.annotations or {})),
            )
            for d in self._dispatcher.list_tools()
        ]
        return types.ListToolsResult(tools=tools)

    async def on_call_tool(
        self,
        ctx: object,
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        """Dispatch a tool call via ``WaggleToolDispatcher`` and return a v2 result."""
        waggle_ctx = self._build_waggle_context(ctx)
        graph = self._graph_for_context(ctx)
        self._dispatcher._graph = graph

        # Run the synchronous dispatcher in a worker thread (same pattern as WaggleServer).
        waggle_result: WaggleToolResult = await anyio.to_thread.run_sync(
            self._dispatcher.call_tool,
            params.name,
            params.arguments or {},
            waggle_ctx,
        )

        return self._to_mcp_result(waggle_result)

    async def on_list_resources(
        self,
        ctx: object,
        params: types.PaginatedRequestParams | None,
    ) -> types.ListResourcesResult:
        del ctx, params
        return build_resources()

    async def on_read_resource(
        self,
        ctx: object,
        params: types.ReadResourceRequestParams,
    ) -> types.ReadResourceResult:
        graph = self._graph_for_context(ctx)
        text = read_resource_text(graph, str(params.uri))
        return types.ReadResourceResult(
            contents=[types.TextResourceContents(uri=params.uri, text=text, mime_type="text/plain")]
        )

    async def on_list_prompts(
        self,
        ctx: object,
        params: types.PaginatedRequestParams | None,
    ) -> types.ListPromptsResult:
        del ctx, params
        return types.ListPromptsResult(prompts=build_prompts())

    async def on_get_prompt(
        self,
        ctx: object,
        params: types.GetPromptRequestParams,
    ) -> types.GetPromptResult:
        del ctx
        return get_prompt_result(params.name, dict(params.arguments or {}))

    # ── Context construction ──────────────────────────────────────────────

    def _build_waggle_context(self, ctx: object) -> WaggleRequestContext:
        """Extract Waggle request context from an MCP v2 ``ServerRequestContext``.

        In SDK v2, per-request HTTP metadata lives in ``ctx.request`` (a
        Starlette ``Request``) when the transport is HTTP.  On stdio,
        ``ctx.request`` is ``None`` and ``ctx.headers`` is ``None``.

        Waggle's own HTTP middleware stamps ``request.state.request_id`` and
        ``request.state.api_key_id`` for tenant resolution; the adapter reads
        those here.
        """
        request_id = ""
        api_key_id = ""
        transport = "stdio"

        # ServerRequestContext.request holds the raw Starlette Request on HTTP.
        raw_request = getattr(ctx, "request", None)
        if raw_request is not None:
            transport = "http"
            state = getattr(raw_request, "state", None)
            request_id = str(getattr(state, "request_id", "") or "")
            api_key_id = str(getattr(state, "api_key_id", "") or "")
        else:
            # Fall back to the request_id on the context itself (available for stdio).
            req_id_raw = getattr(ctx, "request_id", None)
            if req_id_raw is not None:
                request_id = str(req_id_raw)

        tenant_id = getattr(self._graph_for_context(ctx), "tenant_id", "")
        return WaggleRequestContext(
            request_id=request_id,
            tenant_id=tenant_id,
            transport=transport,
            api_key_id=api_key_id or None,
        )

    def _graph_for_context(self, ctx: object) -> object:
        """Resolve the tenant graph for this request when HTTP state provides one."""
        graph = self._dispatcher._graph
        raw_request = getattr(ctx, "request", None)
        tenant_id = ""
        if raw_request is not None:
            state = getattr(raw_request, "state", None)
            tenant_id = str(getattr(state, "tenant_id", "") or "")
        if tenant_id and hasattr(graph, "for_tenant"):
            return graph.for_tenant(tenant_id)
        return graph

    # ── Result translation ────────────────────────────────────────────────

    @staticmethod
    def _to_mcp_result(result: WaggleToolResult) -> types.CallToolResult:
        """Translate ``WaggleToolResult`` → MCP SDK v2 ``CallToolResult``."""
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=result.text)],
            structured_content=result.structured,  # v2 snake_case field
            is_error=result.is_error,  # v2 snake_case field
        )
