"""Application-level WebMCP support for the Waggle Workspace."""

from .proposals import ProposalRepository
from .workspace import compile_project_brief, propose_memory_change, recall_authoritative_memory

__all__ = ["ProposalRepository", "compile_project_brief", "propose_memory_change", "recall_authoritative_memory"]
