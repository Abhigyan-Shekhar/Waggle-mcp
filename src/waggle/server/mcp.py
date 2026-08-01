from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import anyio
import mcp.types as types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions

try:
    from mcp.server.lowlevel.server import request_ctx
except ImportError:

    class _MissingRequestContext:
        def get(self) -> Any:
            raise LookupError

    request_ctx = _MissingRequestContext()

from waggle import __version__
from waggle.config import AppConfig
from waggle.embeddings import STATUS_READY
from waggle.metrics import MetricsRegistry
from waggle.protocol.mcp.surface import (
    MEMORY_AUTOMATION_POLICY,
    build_prompts,
    build_resources,
    get_prompt_result,
    read_resource_text,
)
from waggle.recursive_context import RECURSIVE_CONTEXT_ENABLED
from waggle.runtime_info import SERVER_NAME, WAGGLE_SERVER_INFO
from waggle.tools.context import WaggleRequestContext
from waggle.tools.dispatcher import _TOOL_ALIASES, WaggleToolDispatcher

from .utils import _build_backend

LOGGER = logging.getLogger(__name__)

__all__ = [
    "AUTOMATIC_MEMORY_RULE_TEXT",
    "MEMORY_AUTOMATION_POLICY",
    "RECURSIVE_CONTEXT_ENABLED",
    "_AGENTS_MEMORY_BLOCK",
    "_AGENTS_MEMORY_BLOCK_FOOTER",
    "_AGENTS_MEMORY_BLOCK_HEADER",
    "_TOOL_ALIASES",
    "WaggleServer",
    "_LegacyCallToolResult",
    "_LegacyTool",
]

AUTOMATIC_MEMORY_RULE_TEXT = """Use Waggle automatically for conversational memory.

At the start of a new session, if project, agent, or session scope is known, call prime_context.

Before answering questions that may depend on prior decisions, preferences, constraints, project state, or earlier conversation context, call query_graph with the narrowest relevant scope.

After completed turns that contain durable information such as decisions, preferences, constraints, requirements, user corrections, project facts, or meaningful task outcomes, call observe_conversation automatically.

Waggle should remember relevant context automatically. If memory appears empty, the session is likely missing the automatic memory policy or the runtime hooks that call build_context before answers and on_assistant_turn after answers.

Do not ask the user to trigger Waggle manually. Use it in the background when relevant.
"""

_AGENTS_MEMORY_BLOCK_HEADER = "<!-- waggle:auto-memory:start -->"
_AGENTS_MEMORY_BLOCK_FOOTER = "<!-- waggle:auto-memory:end -->"
_AGENTS_MEMORY_BLOCK = (
    f"{_AGENTS_MEMORY_BLOCK_HEADER}\n"
    "## Waggle Automatic Memory\n\n"
    f"{AUTOMATIC_MEMORY_RULE_TEXT.rstrip()}\n"
    f"{_AGENTS_MEMORY_BLOCK_FOOTER}\n"
)


@dataclass(slots=True)
class _LegacyTool:
    name: str
    description: str
    inputSchema: dict[str, Any]
    title: str | None = None
    annotations: dict[str, Any] | None = None

    @property
    def input_schema(self) -> dict[str, Any]:
        return self.inputSchema


@dataclass(slots=True)
class _LegacyCallToolResult:
    content: list[types.TextContent]
    structuredContent: dict[str, Any] | list[Any]
    isError: bool = False

    @property
    def structured_content(self) -> dict[str, Any] | list[Any]:
        return self.structuredContent

    @property
    def is_error(self) -> bool:
        return self.isError


class WaggleServer:
    """Compatibility shell for older imports around the SDK v2 implementation."""

    def __init__(
        self,
        graph: Any | None = None,
        *,
        config: AppConfig | None = None,
        metrics: MetricsRegistry | None = None,
    ) -> None:
        self.config = config or AppConfig.from_env()
        self.metrics = metrics or MetricsRegistry()
        self._static_graph = graph
        self._root_graph = graph or _build_backend(self.config)
        self._dispatcher = WaggleToolDispatcher(
            graph=self._root_graph,
            config=self.config,
            metrics=self.metrics,
        )
        self.server = Server("waggle")
        self._register_handlers()

    @property
    def graph(self) -> Any:
        return self.current_graph()

    def _register_handlers(self) -> None:
        if not hasattr(self.server, "list_tools"):
            self.server = Server(
                "waggle",
                version=__version__,
                on_list_tools=self._on_list_tools_v2,
                on_call_tool=self._on_call_tool_v2,
                on_list_resources=self._on_list_resources_v2,
                on_read_resource=self._on_read_resource_v2,
                on_list_prompts=self._on_list_prompts_v2,
                on_get_prompt=self._on_get_prompt_v2,
            )
            return

        @self.server.list_tools()
        async def list_tools() -> list[_LegacyTool]:
            return self.build_tools()

        @self.server.call_tool()
        async def call_tool(name: str, arguments: dict[str, Any]) -> _LegacyCallToolResult:
            return await anyio.to_thread.run_sync(self.handle_tool_call, name, arguments or {})

        @self.server.list_resources()
        async def list_resources(
            request: types.ListResourcesRequest | None = None,
        ) -> types.ListResourcesResult:
            del request
            return self.build_resources()

        @self.server.read_resource()
        async def read_resource(uri: Any) -> str:
            return self.read_resource_text(str(uri))

        @self.server.list_prompts()
        async def list_prompts() -> list[types.Prompt]:
            return self.build_prompts()

        @self.server.get_prompt()
        async def get_prompt(name: str, arguments: dict[str, str] | None) -> types.GetPromptResult:
            return self.get_prompt_result(name, arguments or {})

    async def _on_list_tools_v2(
        self,
        ctx: object,
        params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        del ctx, params
        tools = [
            types.Tool(
                name=tool.name,
                title=tool.title,
                description=tool.description,
                input_schema=tool.inputSchema,
                annotations=types.ToolAnnotations(**(tool.annotations or {})),
            )
            for tool in self.build_tools()
        ]
        return types.ListToolsResult(tools=tools)

    async def _on_call_tool_v2(
        self,
        ctx: object,
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        del ctx
        result = await anyio.to_thread.run_sync(self.handle_tool_call, params.name, params.arguments or {})
        return types.CallToolResult(
            content=result.content,
            structured_content=result.structuredContent,
            is_error=result.isError,
        )

    async def _on_list_resources_v2(
        self,
        ctx: object,
        params: types.PaginatedRequestParams | None,
    ) -> types.ListResourcesResult:
        del ctx, params
        return self.build_resources()

    async def _on_read_resource_v2(
        self,
        ctx: object,
        params: types.ReadResourceRequestParams,
    ) -> types.ReadResourceResult:
        del ctx
        text = self.read_resource_text(str(params.uri))
        return types.ReadResourceResult(
            contents=[types.TextResourceContents(uri=params.uri, text=text, mime_type="text/plain")]
        )

    async def _on_list_prompts_v2(
        self,
        ctx: object,
        params: types.PaginatedRequestParams | None,
    ) -> types.ListPromptsResult:
        del ctx, params
        return types.ListPromptsResult(prompts=self.build_prompts())

    async def _on_get_prompt_v2(
        self,
        ctx: object,
        params: types.GetPromptRequestParams,
    ) -> types.GetPromptResult:
        del ctx
        return self.get_prompt_result(params.name, dict(params.arguments or {}))

    def build_tools(self) -> list[_LegacyTool]:
        return [
            _LegacyTool(
                name=definition.name,
                title=definition.title,
                description=definition.description,
                inputSchema=definition.input_schema,
                annotations=definition.annotations or {},
            )
            for definition in self._dispatcher.list_tools()
        ]

    def build_prompts(self) -> list[types.Prompt]:
        return build_prompts()

    def get_prompt_result(self, name: str, arguments: dict[str, str]) -> types.GetPromptResult:
        return get_prompt_result(name, arguments)

    def _get_request(self) -> Any:
        from starlette.requests import Request

        try:
            current = request_ctx.get()
        except LookupError:
            return None
        return current.request if isinstance(current.request, Request) else None

    def current_graph(self) -> Any:
        request = self._get_request()
        if request is not None and getattr(request.state, "tenant_id", ""):
            return self._root_graph.for_tenant(request.state.tenant_id)
        return self._root_graph.for_tenant(self.config.default_tenant_id)

    def validate_startup(self) -> None:
        graph = self.current_graph()
        started = time.perf_counter()
        graph.ensure_tenant(graph.tenant_id)
        if self.config.api_key_environment == "live" and self.config.default_tenant_id == "local-default":
            LOGGER.warning(
                "WAGGLE_API_KEY_ENVIRONMENT is set to 'live' but "
                "WAGGLE_DEFAULT_TENANT_ID is still 'local-default'. "
                "Production deployments should use a unique tenant ID."
            )
        em = graph.embedding_model
        if self.config.is_fast_mode:
            LOGGER.info(
                "startup_fast_mode",
                extra={"startup_mode": self.config.startup_mode},
            )
        elif self.config.is_strict_mode:
            LOGGER.info(
                "startup_strict_mode_waiting_for_embedding",
                extra={"model": em.model_name},
            )
            try:
                em.embed("startup validation", wait_timeout=120.0)
                if em.warmup_status != STATUS_READY:
                    LOGGER.warning(
                        "startup_strict_mode_embedding_not_ready",
                        extra={"status": em.warmup_status, "error": em.warmup_error},
                    )
            except Exception:
                LOGGER.exception("startup_strict_mode_embedding_failed")
        else:
            try:
                em.embed("startup validation", wait_timeout=0.5)
            except Exception:
                LOGGER.debug("startup_embedding_probe_skipped")
        self.metrics.observe(
            "waggle_startup_validation_seconds",
            time.perf_counter() - started,
            backend=self.config.backend,
        )

    def build_resources(self) -> types.ListResourcesResult:
        return build_resources()

    def read_resource_text(self, uri: str) -> str:
        return read_resource_text(self.current_graph(), uri)

    def initialization_options(self) -> InitializationOptions:
        return InitializationOptions(
            server_name=SERVER_NAME,
            server_version=__version__,
            capabilities=self.server.get_capabilities(
                notification_options=NotificationOptions(),
                experimental_capabilities={"waggle_server_info": WAGGLE_SERVER_INFO},
            ),
        )

    def handle_tool_call(self, name: str, arguments: dict[str, Any]) -> _LegacyCallToolResult:
        request = self._get_request()
        request_id = ""
        api_key_id = ""
        if request is not None:
            request_id = getattr(request.state, "request_id", "")
            api_key_id = getattr(request.state, "api_key_id", "")
            transport = "http"
        else:
            try:
                request_id = str(request_ctx.get().request_id)
            except LookupError:
                request_id = ""
            transport = "stdio"

        graph = self.current_graph()
        ctx = WaggleRequestContext(
            request_id=request_id,
            tenant_id=getattr(graph, "tenant_id", self.config.default_tenant_id),
            transport=transport,
            api_key_id=api_key_id or None,
        )

        result = self._dispatcher.call_tool(name, arguments, ctx, graph)
        self._record_graph_size(name, graph)

        return _LegacyCallToolResult(
            content=[types.TextContent(type="text", text=result.text)],
            structuredContent=result.structured,
            isError=result.is_error,
        )

    def _record_graph_size(self, tool_name: str, graph: Any) -> None:
        if tool_name not in {"store_node", "store_edge", "import_graph_backup", "pull"}:
            return
        try:
            stats = graph.get_stats()
            self.metrics.set_gauge(
                "waggle_graph_nodes",
                stats.total_nodes,
                tenant_id=getattr(graph, "tenant_id", self.config.default_tenant_id),
            )
            self.metrics.set_gauge(
                "waggle_graph_edges",
                stats.total_edges,
                tenant_id=getattr(graph, "tenant_id", self.config.default_tenant_id),
            )
        except Exception:
            LOGGER.debug("graph_size_metrics_failed", exc_info=True)
