"""Application-level WebMCP support for the Waggle Workspace."""

from .demo import (
    DEMO_COOKIE_MAX_AGE,
    DEMO_COOKIE_NAME,
    DEMO_PUBLIC_PROJECT_ID,
    DemoScope,
    ensure_demo_seed,
    publicize_demo_payload,
    reset_demo,
    resolve_demo_scope,
    resolve_public_project,
    valid_demo_session_id,
)
from .proposals import ProposalRepository
from .workspace import (
    apply_approved_memory_change,
    compile_project_brief,
    propose_memory_change,
    recall_authoritative_memory,
    review_memory_change,
)

__all__ = [
    "DEMO_COOKIE_MAX_AGE",
    "DEMO_COOKIE_NAME",
    "DEMO_PUBLIC_PROJECT_ID",
    "DemoScope",
    "ProposalRepository",
    "apply_approved_memory_change",
    "compile_project_brief",
    "ensure_demo_seed",
    "propose_memory_change",
    "publicize_demo_payload",
    "recall_authoritative_memory",
    "reset_demo",
    "resolve_demo_scope",
    "resolve_public_project",
    "review_memory_change",
    "valid_demo_session_id",
]
