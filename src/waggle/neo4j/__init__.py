from __future__ import annotations

from .base import Neo4jMemoryGraphBase
from .mutation import Neo4jMutationMixin
from .transcript import Neo4jTranscriptMixin
from .traversal import Neo4jTraversalMixin


class Neo4jMemoryGraph(
    Neo4jTranscriptMixin,
    Neo4jTraversalMixin,
    Neo4jMutationMixin,
    Neo4jMemoryGraphBase,
):
    """Neo4j-backed graph memory decomposed and composed modularly."""
