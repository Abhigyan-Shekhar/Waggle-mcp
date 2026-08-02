from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from waggle.retrieval.hybrid import HybridRetrievalConfig, HybridRetriever
    from waggle.retrieval.temporal_slots import TemporalSlotRetriever


__all__ = ["HybridRetrievalConfig", "HybridRetriever", "TemporalSlotRetriever"]


def __getattr__(name: str) -> object:
    if name in {"HybridRetrievalConfig", "HybridRetriever"}:
        from waggle.retrieval.hybrid import HybridRetrievalConfig, HybridRetriever

        exports = {
            "HybridRetrievalConfig": HybridRetrievalConfig,
            "HybridRetriever": HybridRetriever,
        }
        return exports[name]
    if name == "TemporalSlotRetriever":
        from waggle.retrieval.temporal_slots import TemporalSlotRetriever

        return TemporalSlotRetriever
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
