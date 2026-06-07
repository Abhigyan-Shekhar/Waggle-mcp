"""Persistent graph memory MCP server."""

from importlib import import_module
from importlib.metadata import PackageNotFoundError, version

_LAZY_EXPORTS = {
    "MemoryGraph": ("waggle.graph", "MemoryGraph"),
    "Neo4jMemoryGraph": ("waggle.neo4j_graph", "Neo4jMemoryGraph"),
    "Edge": ("waggle.models", "Edge"),
    "ApiKeyCreateResult": ("waggle.models", "ApiKeyCreateResult"),
    "ApiKeyRecord": ("waggle.models", "ApiKeyRecord"),
    "BackupResult": ("waggle.models", "BackupResult"),
    "ConflictEntry": ("waggle.models", "ConflictEntry"),
    "ConflictListResult": ("waggle.models", "ConflictListResult"),
    "ConflictRecord": ("waggle.models", "ConflictRecord"),
    "ContextBundle": ("waggle.models", "ContextBundle"),
    "ContextBundleExportResult": ("waggle.models", "ContextBundleExportResult"),
    "ContextRenderHints": ("waggle.models", "ContextRenderHints"),
    "ContextScopeResult": ("waggle.models", "ContextScopeResult"),
    "ContextTimelineItem": ("waggle.models", "ContextTimelineItem"),
    "GraphStats": ("waggle.models", "GraphStats"),
    "GraphDiffResult": ("waggle.models", "GraphDiffResult"),
    "EvidenceRecord": ("waggle.models", "EvidenceRecord"),
    "ImportResult": ("waggle.models", "ImportResult"),
    "Node": ("waggle.models", "Node"),
    "NodeHistoryResult": ("waggle.models", "NodeHistoryResult"),
    "NodeStoreResult": ("waggle.models", "NodeStoreResult"),
    "NodeType": ("waggle.models", "NodeType"),
    "ObservationResult": ("waggle.models", "ObservationResult"),
    "PrimeContextResult": ("waggle.models", "PrimeContextResult"),
    "RelationType": ("waggle.models", "RelationType"),
    "SubgraphResult": ("waggle.models", "SubgraphResult"),
    "TenantRecord": ("waggle.models", "TenantRecord"),
    "TimelineResult": ("waggle.models", "TimelineResult"),
    "TopicCluster": ("waggle.models", "TopicCluster"),
    "TopicResult": ("waggle.models", "TopicResult"),
    "AsyncMemoryOrchestrator": ("waggle.orchestrator", "AsyncMemoryOrchestrator"),
    "ConversationTurn": ("waggle.orchestrator", "ConversationTurn"),
    "IngestPlan": ("waggle.orchestrator", "IngestPlan"),
    "MemoryPolicy": ("waggle.orchestrator", "MemoryPolicy"),
    "MemoryScope": ("waggle.orchestrator", "MemoryScope"),
    "RetrievePlan": ("waggle.orchestrator", "RetrievePlan"),
    "RetrieveRequest": ("waggle.orchestrator", "RetrieveRequest"),
    "ModelAdapter": ("waggle.chat_runtime", "ModelAdapter"),
    "OrchestratedChatRuntime": ("waggle.chat_runtime", "OrchestratedChatRuntime"),
    "RuntimeTurnResult": ("waggle.chat_runtime", "RuntimeTurnResult"),
}

__all__ = sorted(_LAZY_EXPORTS)


def __getattr__(name: str):
    export = _LAZY_EXPORTS.get(name)
    if export is None:
        raise AttributeError(name)
    module_name, attr_name = export
    module = import_module(module_name)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value


try:  # pragma: no cover
    __version__ = version("waggle-mcp")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0.0.1"

version = __version__
