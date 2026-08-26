"""Application-level WebMCP support for the Waggle Workspace."""

from .proposals import ProposalRepository
from .workspace import (
    apply_approved_memory_change,
    compile_project_brief,
    propose_memory_change,
    recall_authoritative_memory,
    review_memory_change,
)

__all__ = [
    "ProposalRepository",
    "apply_approved_memory_change",
    "compile_project_brief",
    "propose_memory_change",
    "recall_authoritative_memory",
    "review_memory_change",
]
