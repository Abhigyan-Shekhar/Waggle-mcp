"""
waggle/recursive_context.py
============================
RLM-inspired Recursive Context Assembly for Waggle.

Inspired by Recursive Language Models (https://github.com/alexzhang13/rlm):
  - Externalise long context into an environment (the Waggle graph)
  - Decompose a task into targeted subqueries
  - Retrieve from graph, hybrid, and verbatim lanes
  - Expand around important nodes via typed edges
  - Detect updates, contradictions, and superseded memories
  - Deduplicate, rank, and compress into a compact context pack

This module adds a NEW orchestration layer on top of existing Waggle
primitives.  It does NOT replace query_graph, hybrid retrieval, or
prime_context.
"""

from __future__ import annotations

import logging
import os
import random
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name, "").strip().lower()
    if val in ("1", "true", "yes"):
        return True
    if val in ("0", "false", "no"):
        return False
    return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default


# Feature flag — set WAGGLE_RECURSIVE_CONTEXT_ENABLED=false to disable
RECURSIVE_CONTEXT_ENABLED: bool = _env_bool("WAGGLE_RECURSIVE_CONTEXT_ENABLED", True)
DEFAULT_TOKEN_BUDGET: int = _env_int("WAGGLE_RECURSIVE_CONTEXT_DEFAULT_BUDGET", 1200)
DEFAULT_MAX_SUBQUERIES: int = _env_int("WAGGLE_RECURSIVE_CONTEXT_MAX_SUBQUERIES", 6)
DEFAULT_DEPTH: int = _env_int("WAGGLE_RECURSIVE_CONTEXT_DEFAULT_DEPTH", 2)
DEFAULT_INCLUDE_EVIDENCE: bool = _env_bool("WAGGLE_RECURSIVE_CONTEXT_INCLUDE_EVIDENCE", True)

# Edge types that are high-value for context assembly
_HIGH_VALUE_EDGE_TYPES = frozenset({"updates", "contradicts", "depends_on", "derived_from", "part_of"})

# Node types that carry high-signal memory
_HIGH_SIGNAL_NODE_TYPES = frozenset({"decision", "preference", "concept"})

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class RecursiveSubquery(BaseModel):
    """A single decomposed subquery with retrieval metadata."""

    query: str
    purpose: str
    priority: float = 1.0
    retrieval_modes: list[str] = Field(default_factory=lambda: ["graph", "hybrid"])


class RecursiveContextResult(BaseModel):
    """The assembled context pack returned by build_context."""

    original_query: str
    context_pack: str = ""
    subqueries: list[RecursiveSubquery] = Field(default_factory=list)
    nodes_used: list[Any] = Field(default_factory=list)
    edges_used: list[Any] = Field(default_factory=list)
    transcript_evidence: list[Any] = Field(default_factory=list)
    conflicts: list[Any] = Field(default_factory=list)
    token_estimate: int = 0
    debug: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Ablation configuration
# ---------------------------------------------------------------------------


@dataclass
class AblationConfig:
    """
    Controls which RMCA steps are active for ablation studies.

    All flags default to True (full RMCA behaviour).  Set a flag to False
    to disable the corresponding step.  ``random_subqueries`` takes
    precedence over ``decompose`` when both are set.
    """

    decompose: bool = True
    graph_expand: bool = True
    conflict_resolve: bool = True
    verbatim_evidence: bool = True
    budget_compress: bool = True
    random_subqueries: bool = False
    random_seed: int = 42


# ---------------------------------------------------------------------------
# Internal hit container (lightweight, not a Pydantic model for speed)
# ---------------------------------------------------------------------------


@dataclass
class _Hit:
    """A single retrieved memory item with provenance and score."""

    node_id: str
    label: str
    content: str
    node_type: str
    score: float
    source: str  # "graph", "hybrid", "verbatim"
    subquery: str = ""
    created_at: datetime | None = None
    valid_to: datetime | None = None
    is_superseded: bool = False
    updates_ids: list[str] = field(default_factory=list)
    contradicts_ids: list[str] = field(default_factory=list)
    raw_node: Any = None  # original Node object


# ---------------------------------------------------------------------------
# Main controller
# ---------------------------------------------------------------------------


class RecursiveContextController:
    """
    Orchestrates recursive context assembly over Waggle's existing primitives.

    Parameters
    ----------
    graph:
        A MemoryGraph (or Neo4jMemoryGraph) instance.
    hybrid_retriever:
        Optional pre-built HybridRetriever.  If None, the controller will
        call graph.hybrid_retriever() lazily.
    config:
        Optional dict of overrides (token_budget, max_subqueries, depth, …).
    """

    def __init__(
        self,
        graph: Any,
        hybrid_retriever: Any | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        self._graph = graph
        self._hybrid_retriever = hybrid_retriever
        self._config: dict[str, Any] = config or {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build_context(
        self,
        query: str,
        tenant_id: str = "default",
        agent_id: str | None = None,
        project: str | None = None,
        session_id: str | None = None,
        context_window_id: str | None = None,
        token_budget: int = DEFAULT_TOKEN_BUDGET,
        depth: int = DEFAULT_DEPTH,
        max_subqueries: int = DEFAULT_MAX_SUBQUERIES,
        include_evidence: bool = DEFAULT_INCLUDE_EVIDENCE,
        mode: str = "balanced",
        ablation: AblationConfig | None = None,
    ) -> RecursiveContextResult:
        """
        Recursively assemble a compact context pack for *query*.

        Steps
        -----
        1. Decompose query into targeted subqueries.
        2. Run retrieval for each subquery (graph + hybrid + verbatim).
        3. Expand graph around top nodes via typed edges.
        4. Resolve updates/conflicts.
        5. Deduplicate hits.
        6. Rank hits.
        7. Compress to token budget.
        8. Format and return context pack.
        """
        t0 = time.perf_counter()
        query = (query or "").strip()
        if not query:
            return RecursiveContextResult(
                original_query=query,
                context_pack="No query provided.",
                debug={"error": "empty_query"},
            )

        agent_id = (agent_id or "").strip()
        project = (project or "").strip()
        session_id = (session_id or "").strip()

        scope = {
            "agent_id": agent_id,
            "project": project,
            "session_id": session_id,
        }

        # 1. Decompose
        if ablation is not None and ablation.random_subqueries:
            # Random substrings of the query (takes precedence over decompose flag)
            rng = random.Random(ablation.random_seed)
            words = query.split()
            subqueries: list[RecursiveSubquery] = []
            seen_substrings: set[str] = set()
            attempts = 0
            while len(subqueries) < max_subqueries and attempts < max_subqueries * 10:
                attempts += 1
                if len(words) < 2:
                    break
                slice_len = rng.randint(2, min(4, len(words)))
                start = rng.randint(0, len(words) - slice_len)
                substring = " ".join(words[start : start + slice_len])
                if substring in seen_substrings:
                    continue
                seen_substrings.add(substring)
                subqueries.append(
                    RecursiveSubquery(
                        query=substring,
                        purpose="random_substring",
                        priority=1.0,
                        retrieval_modes=["graph", "hybrid"],
                    )
                )
        elif ablation is not None and not ablation.decompose:
            subqueries = [
                RecursiveSubquery(
                    query=query,
                    purpose="original_query",
                    priority=1.0,
                    retrieval_modes=["graph", "hybrid"],
                )
            ]
        else:
            subqueries = self._decompose_query(query, max_subqueries=max_subqueries, mode=mode)

        # 2. Retrieve for each subquery
        all_hits: list[_Hit] = []
        all_edges: list[Any] = []
        transcript_hits: list[Any] = []

        for sq in subqueries:
            # Step 2 ablation: remove "verbatim" from retrieval_modes
            if ablation is not None and not ablation.verbatim_evidence:
                modes = [m for m in sq.retrieval_modes if m != "verbatim"]
                if not modes:
                    modes = ["graph", "hybrid"]
                sq = RecursiveSubquery(
                    query=sq.query,
                    purpose=sq.purpose,
                    priority=sq.priority,
                    retrieval_modes=modes,
                )
            hits, edges, transcripts = self._run_subquery(
                sq,
                scope=scope,
                depth=depth,
                include_evidence=include_evidence,
                mode=mode,
            )
            all_hits.extend(hits)
            all_edges.extend(edges)
            transcript_hits.extend(transcripts)

        # 3. Expand graph around top nodes
        if all_hits and depth > 0 and not (ablation is not None and not ablation.graph_expand):
            top_ids = [h.node_id for h in sorted(all_hits, key=lambda h: -h.score)[:5]]
            expanded_hits, expanded_edges = self._expand_graph(top_ids, scope=scope, depth=depth)
            all_hits.extend(expanded_hits)
            all_edges.extend(expanded_edges)

        # 4. Resolve updates and conflicts
        if ablation is not None and not ablation.conflict_resolve:
            conflict_entries: list[Any] = []
            # all_hits left unchanged
        else:
            all_hits, conflict_entries = self._resolve_updates_and_conflicts(all_hits, all_edges)

        # 5. Deduplicate
        all_hits = self._deduplicate_hits(all_hits)

        # 6. Rank
        all_hits = self._rank_hits(all_hits)

        # 7. Compress to budget
        effective_budget = 999_999_999 if (ablation is not None and not ablation.budget_compress) else token_budget
        context_pack, nodes_used = self._compress_to_budget(
            query=query,
            hits=all_hits,
            conflicts=conflict_entries,
            transcript_hits=transcript_hits,
            token_budget=effective_budget,
        )

        elapsed = time.perf_counter() - t0
        token_estimate = self._estimate_tokens(context_pack)

        return RecursiveContextResult(
            original_query=query,
            context_pack=context_pack,
            subqueries=subqueries,
            nodes_used=nodes_used,
            edges_used=list({e.id: e for e in all_edges if hasattr(e, "id")}.values()),
            transcript_evidence=transcript_hits[:5],
            conflicts=conflict_entries,
            token_estimate=token_estimate,
            debug={
                "elapsed_seconds": round(elapsed, 3),
                "total_hits_before_dedup": len(all_hits),
                "subquery_count": len(subqueries),
                "mode": mode,
                "depth": depth,
                "token_budget": token_budget,
            },
        )

    # ------------------------------------------------------------------
    # Step 1: Decompose query
    # ------------------------------------------------------------------

    def _decompose_query(
        self,
        query: str,
        max_subqueries: int = DEFAULT_MAX_SUBQUERIES,
        mode: str = "balanced",
    ) -> list[RecursiveSubquery]:
        """
        Deterministically decompose a query into targeted subqueries.

        No external LLM required — uses keyword heuristics to detect
        whether the query is a coding/project query or a generic memory query.
        """
        q = query.lower()

        # Detect query intent
        is_project_query = bool(
            re.search(
                r"\b(build|implement|continue|finish|code|develop|fix|debug|feature|task|"
                r"waggle|project|architecture|design|api|module|class|function|test|deploy)\b",
                q,
            )
        )
        is_continuation = bool(
            re.search(
                r"\b(continue|pick up|where we left|resume|last time|from before|carry on)\b",
                q,
            )
        )

        # Extract the main topic/entity from the query (first noun-like phrase)
        topic = self._extract_topic(query)

        subqueries: list[RecursiveSubquery] = []

        if is_project_query or is_continuation:
            # Coding / project context decomposition
            if is_continuation and len(query.split()) <= 8:
                # Generic continuation with no useful topic — use broad project-state subqueries
                # that will match decision/constraint/next-step nodes regardless of topic
                templates = [
                    ("recent decisions", "decisions", 1.0, ["graph", "hybrid"]),
                    ("active constraints and requirements", "constraints", 0.95, ["graph", "hybrid"]),
                    ("next steps and unfinished work", "unfinished_work", 0.90, ["graph", "hybrid"]),
                    ("superseded or rejected directions", "superseded", 0.85, ["graph"]),
                    ("recent implementation details", "implementation", 0.80, ["graph", "hybrid"]),
                    (query, "original_query", 0.75, ["hybrid", "verbatim"]),
                ]
            else:
                templates = [
                    (f"recent decisions about {topic}", "decisions", 1.0, ["graph", "hybrid"]),
                    (f"current unfinished tasks for {topic}", "unfinished_work", 0.95, ["graph", "hybrid"]),
                    (f"constraints and rejected directions for {topic}", "constraints", 0.90, ["graph", "hybrid"]),
                    (f"recent implementation details for {topic}", "implementation", 0.85, ["graph", "hybrid"]),
                    (f"conflicts or updates in {topic} direction", "conflicts", 0.80, ["graph"]),
                    (query, "original_query", 0.75, ["hybrid", "verbatim"]),
                ]
        else:
            # Generic memory query decomposition
            templates = [
                (query, "original_query", 1.0, ["hybrid", "verbatim"]),
                (f"recent relevant facts about {topic}", "recent_facts", 0.90, ["graph", "hybrid"]),
                (f"decisions related to {topic}", "decisions", 0.85, ["graph"]),
                (f"contradictions or conflicts about {topic}", "conflicts", 0.75, ["graph"]),
                (f"transcript evidence for {topic}", "evidence", 0.65, ["verbatim"]),
            ]

        # Fast mode: fewer subqueries
        if mode == "fast":
            templates = templates[:3]

        # Deep mode: add extra subqueries
        if mode == "deep":
            templates.append(
                (
                    f"bugs errors or rejected approaches for {topic}",
                    "bugs_rejected",
                    0.70,
                    ["graph", "hybrid"],
                )
            )
            templates.append(
                (
                    f"next steps or planned work for {topic}",
                    "next_steps",
                    0.72,
                    ["graph", "hybrid"],
                )
            )

        for sq_query, purpose, priority, modes in templates[:max_subqueries]:
            subqueries.append(
                RecursiveSubquery(
                    query=sq_query,
                    purpose=purpose,
                    priority=priority,
                    retrieval_modes=modes,
                )
            )

        return subqueries

    def _extract_topic(self, query: str) -> str:
        """Extract a short topic phrase from the query for subquery templating."""
        # Remove common filler prefixes
        cleaned = re.sub(
            r"^(continue|please|can you|help me|let's|let us|i want to|we need to|"
            r"implement|build|finish|fix|debug|add|create|update)\s+",
            "",
            query.strip(),
            flags=re.IGNORECASE,
        ).strip()

        # Take first 6 words as topic
        words = cleaned.split()[:6]
        topic = " ".join(words)
        return topic or query[:40]

    # ------------------------------------------------------------------
    # Step 2: Run subquery retrieval
    # ------------------------------------------------------------------

    def _run_subquery(
        self,
        subquery: RecursiveSubquery,
        scope: dict[str, str],
        depth: int,
        include_evidence: bool,
        mode: str,
    ) -> tuple[list[_Hit], list[Any], list[Any]]:
        """
        Run retrieval for a single subquery using the requested modes.
        Returns (hits, edges, transcript_hits).
        """
        hits: list[_Hit] = []
        edges: list[Any] = []
        transcripts: list[Any] = []

        retrieval_modes = subquery.retrieval_modes

        # Determine effective retrieval mode
        if "hybrid" in retrieval_modes:
            effective_mode = "hybrid"
        elif "graph" in retrieval_modes:
            effective_mode = "graph"
        else:
            effective_mode = "verbatim"

        # Verbatim-only subqueries
        if retrieval_modes == ["verbatim"]:
            effective_mode = "verbatim"

        try:
            result = self._graph.query(
                query=subquery.query,
                max_nodes=8 if mode == "fast" else 12,
                max_depth=depth,
                agent_id=scope.get("agent_id", ""),
                project=scope.get("project", ""),
                session_id=scope.get("session_id", ""),
                retrieval_mode=effective_mode,
            )
            for node in result.nodes:
                hits.append(self._node_to_hit(node, source=effective_mode, subquery=subquery.query))
            edges.extend(result.edges)

            # Collect verbatim transcript hits
            if include_evidence and hasattr(result, "replay_hits"):
                transcripts.extend(result.replay_hits[:3])
            if include_evidence and hasattr(result, "hybrid_hits"):
                transcripts.extend(result.hybrid_hits[:2])

        except Exception as exc:
            LOGGER.debug("recursive_context._run_subquery failed: %s", exc)
            # Fallback: try graph-only if hybrid failed
            if effective_mode != "graph":
                try:
                    result = self._graph.query(
                        query=subquery.query,
                        max_nodes=8,
                        max_depth=depth,
                        agent_id=scope.get("agent_id", ""),
                        project=scope.get("project", ""),
                        session_id=scope.get("session_id", ""),
                        retrieval_mode="graph",
                    )
                    for node in result.nodes:
                        hits.append(self._node_to_hit(node, source="graph", subquery=subquery.query))
                    edges.extend(result.edges)
                except Exception as exc2:
                    LOGGER.debug("recursive_context._run_subquery graph fallback failed: %s", exc2)

        return hits, edges, transcripts

    def _node_to_hit(self, node: Any, source: str, subquery: str) -> _Hit:
        """Convert a Node object to a _Hit."""
        score = getattr(node, "final_score", None)
        if score is None:
            score = getattr(node, "similarity_score", None) or 0.0

        # Boost high-signal node types
        node_type_str = getattr(node.node_type, "value", str(node.node_type))
        if node_type_str in _HIGH_SIGNAL_NODE_TYPES:
            score = min(1.0, score + 0.1)

        return _Hit(
            node_id=node.id,
            label=node.label,
            content=node.content,
            node_type=node_type_str,
            score=score,
            source=source,
            subquery=subquery,
            created_at=getattr(node, "created_at", None),
            valid_to=getattr(node, "valid_to", None),
            raw_node=node,
        )

    # ------------------------------------------------------------------
    # Step 3: Graph expansion
    # ------------------------------------------------------------------

    def _expand_graph(
        self,
        node_ids: list[str],
        scope: dict[str, str],
        depth: int,
    ) -> tuple[list[_Hit], list[Any]]:
        """
        Expand around top nodes via typed edges.
        Prioritises updates, contradicts, depends_on, derived_from, part_of.
        """
        hits: list[_Hit] = []
        edges: list[Any] = []

        for node_id in node_ids[:3]:  # limit expansion seeds
            try:
                result = self._graph.get_related(node_id=node_id, max_depth=min(depth, 2))
                for node in result.nodes:
                    if node.id not in set(node_ids):
                        hits.append(self._node_to_hit(node, source="graph_expansion", subquery=""))
                edges.extend(result.edges)
            except Exception as exc:
                LOGGER.debug("recursive_context._expand_graph failed for %s: %s", node_id, exc)

        return hits, edges

    # ------------------------------------------------------------------
    # Step 4: Resolve updates and conflicts
    # ------------------------------------------------------------------

    def _resolve_updates_and_conflicts(
        self,
        hits: list[_Hit],
        edges: list[Any],
    ) -> tuple[list[_Hit], list[dict[str, Any]]]:
        """
        Detect updates and contradictions from edges.

        - updates edge: prefer newer node, mark older as superseded
        - contradicts edge: keep both, record conflict entry
        - expired valid_to: mark as superseded
        """
        now = datetime.now(UTC)
        hit_by_id = {h.node_id: h for h in hits}
        conflict_entries: list[dict[str, Any]] = []

        for edge in edges:
            rel = getattr(edge, "relationship", "")
            src = getattr(edge, "source_id", "")
            tgt = getattr(edge, "target_id", "")

            if rel == "updates":
                # source updates target → target is superseded
                if tgt in hit_by_id:
                    hit_by_id[tgt].is_superseded = True
                    hit_by_id[tgt].score *= 0.3
                if src in hit_by_id:
                    hit_by_id[src].updates_ids.append(tgt)
                    hit_by_id[src].score = min(1.0, hit_by_id[src].score + 0.15)

            elif rel == "contradicts":
                if src in hit_by_id and tgt in hit_by_id:
                    hit_by_id[src].contradicts_ids.append(tgt)
                    conflict_entries.append(
                        {
                            "source_id": src,
                            "source_label": hit_by_id[src].label,
                            "target_id": tgt,
                            "target_label": hit_by_id[tgt].label,
                            "relationship": "contradicts",
                        }
                    )

        # Mark expired nodes as superseded
        for hit in hits:
            if hit.valid_to is not None:
                vt = hit.valid_to
                if vt.tzinfo is None:
                    vt = vt.replace(tzinfo=UTC)
                if vt < now:
                    hit.is_superseded = True
                    hit.score *= 0.2

        return list(hit_by_id.values()), conflict_entries

    # ------------------------------------------------------------------
    # Step 5: Deduplicate
    # ------------------------------------------------------------------

    def _deduplicate_hits(self, hits: list[_Hit]) -> list[_Hit]:
        """
        Remove duplicate hits by node_id, keeping the highest-scored copy.
        """
        seen: dict[str, _Hit] = {}
        for hit in hits:
            if hit.node_id not in seen or hit.score > seen[hit.node_id].score:
                seen[hit.node_id] = hit
        return list(seen.values())

    # ------------------------------------------------------------------
    # Step 6: Rank
    # ------------------------------------------------------------------

    def _rank_hits(self, hits: list[_Hit]) -> list[_Hit]:
        """
        Rank hits by score, with superseded items pushed to the bottom.
        """
        return sorted(
            hits,
            key=lambda h: (
                0 if not h.is_superseded else 1,  # superseded last
                -h.score,
                h.label.lower(),
            ),
        )

    # ------------------------------------------------------------------
    # Step 7: Compress to budget
    # ------------------------------------------------------------------

    def _compress_to_budget(
        self,
        query: str,
        hits: list[_Hit],
        conflicts: list[dict[str, Any]],
        transcript_hits: list[Any],
        token_budget: int,
    ) -> tuple[str, list[Any]]:
        """
        Build the context pack string within the token budget.

        Priority order:
        1. Current decisions
        2. Constraints / preferences
        3. Implementation context
        4. Next / unfinished work
        5. Conflicts
        6. Evidence (transcript)
        """
        max_tokens = int(token_budget * 1.15)  # allow 15% overage
        nodes_used: list[Any] = []

        # Bucket hits by node type
        decisions: list[_Hit] = []
        constraints: list[_Hit] = []
        implementation: list[_Hit] = []
        unfinished: list[_Hit] = []
        superseded: list[_Hit] = []
        other: list[_Hit] = []

        for hit in hits:
            nt = hit.node_type
            if hit.is_superseded:
                superseded.append(hit)
            elif nt in ("decision",):
                decisions.append(hit)
            elif nt in ("preference", "concept"):
                constraints.append(hit)
            elif nt in ("fact", "note"):
                implementation.append(hit)
            elif nt in ("question",):
                unfinished.append(hit)
            else:
                other.append(hit)

        # Build sections
        sections: list[tuple[str, list[_Hit]]] = [
            ("Current relevant decisions", decisions),
            ("Active constraints", constraints),
            ("Important implementation context", implementation + other),
            ("Recent progress / unfinished work", unfinished),
        ]

        lines: list[str] = [
            "### Waggle Recursive Context Pack",
            f"Task: {query}",
            "",
        ]
        used_tokens = self._estimate_tokens("\n".join(lines))

        for section_title, section_hits in sections:
            if not section_hits:
                continue
            section_lines = [f"{section_title}:"]
            for hit in section_hits:
                bullet = f"- [{hit.node_type}] {hit.label}: {hit.content[:200]}"
                if hit.updates_ids:
                    bullet += f" (supersedes {len(hit.updates_ids)} older item(s))"
                cost = self._estimate_tokens(bullet)
                if used_tokens + cost > max_tokens:
                    break
                section_lines.append(bullet)
                used_tokens += cost
                if hit.raw_node is not None:
                    nodes_used.append(hit.raw_node)
            if len(section_lines) > 1:
                lines.extend(section_lines)
                lines.append("")

        # Conflicts section
        if conflicts:
            conflict_lines = ["Conflicts or superseded context:"]
            for c in conflicts:
                bullet = f"- Possible conflict: '{c['source_label']}' contradicts '{c['target_label']}'"
                cost = self._estimate_tokens(bullet)
                if used_tokens + cost > max_tokens:
                    break
                conflict_lines.append(bullet)
                used_tokens += cost
            if len(conflict_lines) > 1:
                lines.extend(conflict_lines)
                lines.append("")

        # Superseded section (brief)
        if superseded:
            sup_lines = ["Superseded context (for reference):"]
            for hit in superseded[:3]:
                bullet = f"- [superseded] {hit.label}: {hit.content[:100]}"
                cost = self._estimate_tokens(bullet)
                if used_tokens + cost > max_tokens:
                    break
                sup_lines.append(bullet)
                used_tokens += cost
            if len(sup_lines) > 1:
                lines.extend(sup_lines)
                lines.append("")

        # Evidence section
        if transcript_hits:
            ev_lines = ["Evidence:"]
            for hit in transcript_hits[:3]:
                snippet = ""
                if hasattr(hit, "transcript_snippet"):
                    snippet = str(hit.transcript_snippet)[:150]
                elif hasattr(hit, "transcript_text"):
                    snippet = str(hit.transcript_text)[:150]
                elif isinstance(hit, dict):
                    snippet = str(hit.get("transcript_snippet") or hit.get("transcript_text", ""))[:150]
                if snippet:
                    bullet = f"- {snippet}"
                    cost = self._estimate_tokens(bullet)
                    if used_tokens + cost > max_tokens:
                        break
                    ev_lines.append(bullet)
                    used_tokens += cost
            if len(ev_lines) > 1:
                lines.extend(ev_lines)
                lines.append("")

        context_pack = "\n".join(lines).rstrip()
        return context_pack, nodes_used

    # ------------------------------------------------------------------
    # Token estimation
    # ------------------------------------------------------------------

    def _estimate_tokens(self, text: str) -> int:
        """Approximate token count: 1 token ≈ 4 characters."""
        return len(text) // 4

    # ------------------------------------------------------------------
    # Evidence collection (public helper for tests)
    # ------------------------------------------------------------------

    def _collect_evidence(
        self,
        query: str,
        scope: dict[str, str],
        max_items: int = 5,
    ) -> list[Any]:
        """Collect verbatim transcript evidence for a query."""
        try:
            result = self._graph.query(
                query=query,
                max_nodes=max_items,
                max_depth=1,
                agent_id=scope.get("agent_id", ""),
                project=scope.get("project", ""),
                session_id=scope.get("session_id", ""),
                retrieval_mode="verbatim",
            )
            evidence: list[Any] = []
            if hasattr(result, "replay_hits"):
                evidence.extend(result.replay_hits)
            if hasattr(result, "hybrid_hits"):
                evidence.extend(result.hybrid_hits)
            return evidence[:max_items]
        except Exception as exc:
            LOGGER.debug("recursive_context._collect_evidence failed: %s", exc)
            return []
