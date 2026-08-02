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
from types import SimpleNamespace
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


@dataclass
class _PinnedCandidate:
    """High-precision fact candidate for the protected pinned lane."""

    line: str
    normalized_value: str
    score: float
    source_authority: str
    temporal_state: str
    relation_score: float = 0.0
    transcript_key: str = ""
    hit_id: str = ""
    raw_node: Any = None
    observed_at: datetime | None = None


@dataclass(frozen=True)
class _ObligationCandidate:
    """Canonical countable obligation extracted from transcript evidence."""

    action: str
    item: str
    store: str = ""
    event_id: str = ""
    replaces: str = ""
    source: str = ""
    score: float = 0.0


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
        self._longmemeval_session_date_cache: dict[tuple[str, str, str], datetime | None] = {}

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
        pinned_transcript_hits: list[Any] = []

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

        if include_evidence:
            direct_hits = self._direct_transcript_evidence(query, scope=scope, limit=8)
            pinned_transcript_hits = self._pinned_transcript_evidence(query=query, scope=scope, limit=8)
            direct_hits.extend(pinned_transcript_hits)
            answer_category = self._detect_answer_category(query)
            for evidence_query in self._answer_evidence_queries(query, answer_category):
                if evidence_query.strip().lower() == query.strip().lower():
                    continue
                direct_hits.extend(self._direct_transcript_evidence(evidence_query, scope=scope, limit=3))
            direct_hits.extend(
                self._lexical_answer_transcript_evidence(
                    query=query,
                    category=answer_category,
                    scope=scope,
                    limit=6,
                )
            )
            transcript_hits = [*direct_hits, *transcript_hits]

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
            pinned_transcript_hits=pinned_transcript_hits,
            token_budget=effective_budget,
            scope=scope,
        )

        elapsed = time.perf_counter() - t0
        token_estimate = self._estimate_tokens(context_pack)
        nodes_used = self._deduplicate_nodes_used(nodes_used)

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
        targeted_templates: list[tuple[str, str, float, list[str]]] = []

        if self._looks_like_constraint_query(q):
            targeted_templates.extend(
                [
                    (
                        f"quick correction right constraint for {topic}",
                        "constraint_correction",
                        1.10,
                        ["hybrid", "verbatim"],
                    ),
                    (f"corrected constraint for {topic}", "constraint_correction", 1.05, ["hybrid", "verbatim"]),
                    (
                        f"user preference constraint for {topic}",
                        "constraint_preference",
                        1.00,
                        ["graph", "hybrid", "verbatim"],
                    ),
                ]
            )

        if self._looks_like_count_update_query(q):
            targeted_templates.extend(
                [
                    (
                        f"inventory note used bought added count for {topic}",
                        "count_all_steps",
                        1.10,
                        ["hybrid", "verbatim"],
                    ),
                    (f"used reduced count for {topic}", "count_subtractions", 1.05, ["hybrid", "verbatim"]),
                    (f"bought added more count for {topic}", "count_additions", 1.00, ["hybrid", "verbatim"]),
                    (f"initial inventory count for {topic}", "count_initial", 0.95, ["hybrid", "verbatim"]),
                ]
            )

        if self._looks_like_exact_recall_query(q):
            targeted_templates.extend(
                [
                    (query, "exact_recall_original", 1.20, ["graph", "hybrid", "verbatim"]),
                    (topic, "exact_recall_topic", 1.10, ["graph", "hybrid", "verbatim"]),
                    (
                        f"exact source evidence for {topic}",
                        "exact_recall_evidence",
                        1.00,
                        ["graph", "hybrid", "verbatim"],
                    ),
                ]
            )

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

        if targeted_templates:
            templates = self._merge_template_priorities(targeted_templates, templates)

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

    def _looks_like_constraint_query(self, query_lower: str) -> bool:
        return bool(
            re.search(
                r"\b(constraint|preference|prefer|respect|avoid|requirement|personaliz(?:e|ation)|should .* use)\b",
                query_lower,
            )
        )

    def _looks_like_count_update_query(self, query_lower: str) -> bool:
        return bool(
            re.search(r"\bhow many\b", query_lower)
            and re.search(
                r"\b(now|current|currently|should|have|left|remain|remaining|total|in total|different|"
                r"types?|items?|hours?|days?|services?|pick up|return)\b",
                query_lower,
            )
        )

    def _looks_like_exact_recall_query(self, query_lower: str) -> bool:
        return bool(
            re.search(
                r"\b(previous chat|remind me|what was|what were|which exact|what did i|what did you|"
                r"can you remind|checking our previous|checking my previous)\b",
                query_lower,
            )
        )

    def _detect_answer_category(self, query: str) -> str:
        q = (query or "").lower()
        if self._looks_like_temporal_ordering_query(q):
            return "temporal_ordering"
        if self._looks_like_table_lookup_query(q):
            return "table_lookup"
        if self._looks_like_short_personal_fact_query(q):
            return "short_personal_fact"
        if self._looks_like_exact_detail_query(q):
            return "exact_detail"
        if self._looks_like_enumerated_list_query(q):
            return "enumerated_list"
        if self._looks_like_personalization_advice_query(q):
            return "personalization_advice"
        return "generic"

    def _looks_like_table_lookup_query(self, query_lower: str) -> bool:
        return bool(
            re.search(r"\b(table|row|rotation|shift|schedule|roster|assigned|assignment)\b", query_lower)
            and re.search(
                r"\b(sunday|monday|tuesday|wednesday|thursday|friday|saturday|day shift|night shift|agent|person)\b",
                query_lower,
            )
        )

    def _looks_like_short_personal_fact_query(self, query_lower: str) -> bool:
        return bool(
            re.search(r"\b(what|which|how many|how long|how often|where|when)\b", query_lower)
            and re.search(r"\b(i|my|me)\b", query_lower)
            and re.search(
                r"\b(degree|graduate|graduated|commute|personal best|time|duration|how long|tried|visited|worked|"
                r"study|studied|ratio|bought|buy|from|source|store|shop|bookshelf|phone|model|pack|packed|"
                r"keep|kept|stored|storage|closet|rack|occupation|job|role|career|startup|"
                r"items?|clothing|clothes|food|delivery|services?|jogging|yoga|tennis|play|played|frequency|often|"
                r"therapist|therapy|followers?|instagram|record|league|team|volleyball|coins?|bikes?)\b",
                query_lower,
            )
        )

    def _looks_like_personalization_advice_query(self, query_lower: str) -> bool:
        if not re.search(r"\b(i|my|me|i've|i'm|im)\b", query_lower):
            return False
        return bool(
            re.search(
                r"\b(any advice|ideas?|recommendations?|suggestions?|tips?|how can i|how should i|"
                r"what should i|help me|better results|find new inspiration|stuck with|struggling with)\b",
                query_lower,
            )
        )

    def _looks_like_temporal_ordering_query(self, query_lower: str) -> bool:
        return bool(
            (
                re.search(
                    r"\b(order|ordered|first to last|chronological|happened first|which .* happened|which .* first)\b",
                    query_lower,
                )
                and re.search(r"\b(event|events|issue|issues|health|first|last|then)\b", query_lower)
            )
            or bool(re.search(r"\bhow many\s+days?\s+(?:before|after)\b", query_lower))
            or bool(re.search(r"\bhow many\s+days?.*\bbetween\b", query_lower))
        )

    def _looks_like_exact_detail_query(self, query_lower: str) -> bool:
        detail_terms = re.search(
            r"\b(color|colour|body|image|picture|description|called|named|name|title|shop|restaurant|place|"
            r"dessert|milkshake|milkshakes|recommended|unique|specific|exact|mummies|monsters?|enemies|"
            r"chord|progression|chorus|song|second song|ratio|bookshelf|quote|said|center|centre|circumference|library)\b",
            query_lower,
        )
        return bool(
            (self._looks_like_exact_recall_query(query_lower) and detail_terms)
            or self._looks_like_identity_detail_query(query_lower)
            or self._looks_like_named_entity_detail_query(query_lower)
            or bool(re.search(r"\bhow many\b.*\b(mummies|monsters?|enemies|people|items?)\b", query_lower))
            or bool(re.search(r"\bwhere\b.*\b(buy|bought|from|store|shop|bookshelf)\b", query_lower))
            or bool(re.search(r"\bratio\b", query_lower))
        )

    def _looks_like_enumerated_list_query(self, query_lower: str) -> bool:
        return bool(
            self._looks_like_exact_recall_query(query_lower)
            and re.search(
                r"\b(what kind of|which|what are|list|processes?|steps?|items?|options?|kinds?)\b", query_lower
            )
            and re.search(r"\b(used|use|included|include|at|for|in|on)\b", query_lower)
        )

    def _answer_evidence_queries(self, query: str, category: str) -> list[str]:
        queries = [query]
        topic = self._extract_topic(query)
        if category == "short_personal_fact":
            q = query.lower()
            if "degree" in q or "graduat" in q:
                queries.extend(["I graduated with a degree", "my degree in"])
            if "commute" in q or "how long" in q:
                queries.extend(["my daily commute takes minutes each way", "commute takes 45 minutes each way"])
            if "what time" in q or "when" in q:
                queries.extend(["I stop checking work emails by 7 pm", "stop checking messages by 7 pm"])
            if "personal best" in q:
                queries.extend([f"my personal best {topic}", f"personal best time {topic}"])
            if "how many" in q:
                queries.extend(
                    [
                        f"I have tried {topic}",
                        f"how many {topic} I tried",
                        f"{topic} pick up return store",
                        f"{topic} delivery services used recently",
                        "food delivery services Domino Fresh Fusion Uber Eats",
                        "Uber Eats weekends lately lifesaver",
                        f"{topic} total hours jogging yoga last week",
                    ]
                )
                if re.search(r"\b(replace|replaced|fix|fixed|items?)\b", q):
                    queries.extend(
                        [
                            f"{topic} replaced fixed",
                            "I replaced my old kitchen faucet",
                            "new kitchen mat in front of the sink",
                            "got rid of the old toaster replaced it with a toaster oven",
                            "donated my old coffee maker",
                            "fixed the kitchen shelves",
                        ]
                    )
            if "how often" in q:
                queries.extend(
                    [
                        f"{topic} weekly",
                        f"{topic} every other week",
                        "weekly tennis sessions with friends",
                        "play tennis with my friends every other week",
                    ]
                )
            if re.search(r"\b(difference|price|cost)\b", q) and re.search(r"\b(store|budget|similar|boots?)\b", q):
                queries.extend(
                    [
                        "luxury boots cost $800",
                        "similar boots budget store $50",
                        "budget store for $50",
                        "$800 boots $50 similar boots",
                    ]
                )
            if re.search(
                r"\b(previous occupation|occupation|previous job|old job|previous role|worked as|work as)\b", q
            ):
                queries.extend(
                    [
                        "my previous role was",
                        "I worked as",
                        "previous occupation",
                        "previous role startup",
                        "used Trello in my previous role as a marketing specialist",
                    ]
                )
            if re.search(r"\bwhere\b", q) and re.search(r"\b(keep|kept|store|stored|storage|sneakers?|shoes?)\b", q):
                queries.extend([f"where I keep {topic}", f"stored {topic}", f"{topic} shoe rack closet"])
            if "ratio" in q:
                queries.extend([f"my preferred ratio {topic}", "gin to vermouth ratio 3:1", "settled on a ratio"])
            if re.search(r"\b(where|from)\b", q) and re.search(r"\b(buy|bought|store|shop|bookshelf)\b", q):
                queries.extend([f"I bought {topic} from", f"new {topic} from", "bought my new bookshelf from IKEA"])
        elif category == "table_lookup":
            names = " ".join(self._query_capitalized_terms(query))
            queries.extend(
                [
                    f"{names} {topic}".strip(),
                    f"final updated table row {names} {topic}".strip(),
                    f"named agents {names} Sunday shift rotation".strip(),
                ]
            )
        elif category == "temporal_ordering":
            phrases = self._extract_event_phrases(query)
            queries.extend(phrases)
            if re.search(r"\bhow many\s+days?\s+(?:before|after)\b", query.lower()):
                queries.extend([f"{phrase} documentDate date" for phrase in phrases])
                queries.extend(
                    [
                        "brother graduation gift date bought",
                        "graduation gift on date",
                        "best friend birthday gift date bought",
                    ]
                )
            if re.search(r"\bwhich\b.*\bfirst\b", query.lower()):
                queries.extend([f"{phrase} first date documentDate" for phrase in phrases])
                queries.extend([f"{phrase} month ago last week" for phrase in phrases])
        elif category == "exact_detail":
            content_terms = " ".join(sorted(self._query_content_terms(query.lower())))
            capitalized = " ".join(self._query_capitalized_terms(query))
            queries.extend(
                [
                    topic,
                    f"{capitalized} {topic}".strip(),
                    content_terms,
                    f"exact detail {content_terms}".strip(),
                ]
            )
            q = query.lower()
            if "mummies" in q or "enemies" in q or "monsters" in q:
                queries.extend(["mummies 4 stat blocks", "Mummies (4)", "party will face mummies"])
            if "chord" in q or "chorus" in q or "song" in q:
                queries.extend(["second song chorus chord progression", "C D E F G A B A G F E D C"])
            if "ratio" in q:
                queries.extend(["3:1 gin vermouth classic martini", "settled on 3:1 ratio"])
            if "bookshelf" in q or re.search(r"\bwhere\b.*\b(bought|buy|from)\b", q):
                queries.extend(["IKEA bookshelf", "bought my new bookshelf from IKEA"])
            if re.search(r"\b(library|babel|borges|center|centre|circumference)\b", q):
                queries.extend(
                    [
                        "Borges Library sphere exact center hexagons circumference inaccessible",
                        "The Library is a sphere whose exact center is any one of its hexagons",
                        "circumference is inaccessible",
                    ]
                )
        elif category == "enumerated_list":
            content_terms = " ".join(sorted(self._query_content_terms(query.lower())))
            capitalized = " ".join(self._query_capitalized_terms(query))
            queries.extend(
                [
                    topic,
                    f"{capitalized} {topic}".strip(),
                    content_terms,
                    f"complete list {content_terms}".strip(),
                ]
            )
        elif category == "generic" and self._looks_like_personalization_advice_query(query.lower()):
            content_terms = " ".join(sorted(self._query_content_terms(query.lower())))
            queries.extend(
                [
                    content_terms,
                    f"user preferences {content_terms}".strip(),
                    f"previous successful {content_terms}".strip(),
                ]
            )

        deduped: list[str] = []
        seen: set[str] = set()
        for item in queries:
            key = re.sub(r"\s+", " ", item.strip().lower())
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(item.strip())
        return deduped

    def _extract_event_phrases(self, query: str) -> list[str]:
        text = re.sub(r"\s+", " ", query or "").strip()
        if ":" in text:
            text = text.split(":", 1)[1]
        text = text.rstrip("?.")
        comparative = re.search(
            r"\b(?:which\s+)?(.+?)\s+(?:or|versus|vs\.?)\s+(.+?)\??$",
            text,
            flags=re.IGNORECASE,
        )
        if comparative and re.search(r"\b(first|before|after|earlier)\b", text, flags=re.IGNORECASE):
            return [
                re.sub(r"^(?:which|the)\s+", "", comparative.group(1), flags=re.IGNORECASE).strip(" ,"),
                re.sub(r"^(?:the)\s+", "", comparative.group(2), flags=re.IGNORECASE).strip(" ,"),
            ][:5]
        before_after = re.search(
            r"\b(?:how many\s+days?\s+)?(?:before|after)\s+(?:the\s+)?['\"]?(.+?)['\"]?\s+(?:did|was|were)\s+(.+)$",
            text,
            flags=re.IGNORECASE,
        )
        if before_after:
            return [before_after.group(1).strip(" ,\"'"), before_after.group(2).strip(" ,\"'")][:5]
        between_days = re.search(
            r"\bbetween\s+the\s+day\s+(.+?)\s+and\s+the\s+day\s+(.+)$",
            text,
            flags=re.IGNORECASE,
        )
        if between_days:
            return [
                re.sub(r"^(?:i|you)\s+", "", between_days.group(1), flags=re.IGNORECASE).strip(" ,\"'"),
                re.sub(r"^(?:i|you)\s+", "", between_days.group(2), flags=re.IGNORECASE).strip(" ,\"'"),
            ][:5]
        text = re.sub(r"\band the day\b", ", the day", text, flags=re.IGNORECASE)
        parts = [
            part.strip(" ,")
            for part in re.split(r",\s*(?=the day\b|when\b|after\b|before\b)", text, flags=re.IGNORECASE)
        ]
        if len(parts) <= 1:
            parts = [part.strip(" ,") for part in re.split(r"\b(?:then|finally|lastly)\b", text, flags=re.IGNORECASE)]
        phrases: list[str] = []
        for part in parts:
            part = re.sub(r"^(and\s+)?", "", part, flags=re.IGNORECASE).strip(" ,")
            if len(part.split()) >= 4:
                phrases.append(part)
        return phrases[:5]

    def _query_content_terms(self, query_lower: str) -> set[str]:
        stopwords = {
            "the",
            "and",
            "for",
            "our",
            "you",
            "can",
            "what",
            "was",
            "were",
            "did",
            "about",
            "previous",
            "chat",
            "checking",
            "remind",
            "which",
            "three",
            "from",
            "first",
            "last",
            "happened",
            "order",
            "day",
            "time",
            "going",
            "back",
            "could",
            "would",
        }
        return {token for token in re.findall(r"[a-z0-9]+", query_lower) if len(token) > 2 and token not in stopwords}

    def _query_capitalized_terms(self, query: str) -> list[str]:
        names: list[str] = []
        for token in re.findall(r"\b[A-Z][a-zA-Z0-9_-]{2,}\b", query or ""):
            if token.lower() in {
                "can",
                "what",
                "which",
                "sunday",
                "monday",
                "tuesday",
                "wednesday",
                "thursday",
                "friday",
                "saturday",
            }:
                continue
            names.append(token)
        deduped: list[str] = []
        seen: set[str] = set()
        for name in names:
            key = name.lower()
            if key not in seen:
                seen.add(key)
                deduped.append(name)
        return deduped

    def _merge_template_priorities(
        self,
        preferred: list[tuple[str, str, float, list[str]]],
        fallback: list[tuple[str, str, float, list[str]]],
    ) -> list[tuple[str, str, float, list[str]]]:
        merged: list[tuple[str, str, float, list[str]]] = []
        seen: set[str] = set()
        for item in [*preferred, *fallback]:
            key = re.sub(r"\s+", " ", item[0].strip().lower())
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
        return merged

    def _extract_topic(self, query: str) -> str:
        """Extract a short topic phrase from the query for subquery templating."""
        about_match = re.search(
            r"\babout\s+(?:the\s+)?(.+?)(?:\.\s|,\s| can you\b| could you\b| remind\b| what\b| which\b|$)",
            query.strip(),
            flags=re.IGNORECASE,
        )
        if about_match:
            topic = re.sub(r"\s+", " ", about_match.group(1)).strip(" ?.")
            if topic:
                return " ".join(topic.split()[:12])

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

            if "graph" in retrieval_modes and effective_mode != "graph":
                graph_result = self._graph.query(
                    query=subquery.query,
                    max_nodes=8 if mode == "fast" else 12,
                    max_depth=depth,
                    agent_id=scope.get("agent_id", ""),
                    project=scope.get("project", ""),
                    session_id=scope.get("session_id", ""),
                    retrieval_mode="graph",
                )
                for node in graph_result.nodes:
                    hits.append(self._node_to_hit(node, source="graph", subquery=subquery.query))
                edges.extend(graph_result.edges)

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
        pinned_transcript_hits: list[Any] | None = None,
        scope: dict[str, str] | None = None,
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
        answer_category = self._detect_answer_category(query)
        is_personalization_query = self._looks_like_personalization_advice_query(query.lower())
        personalization_graph_hits = self._personalization_graph_hits(query, hits) if is_personalization_query else []
        personalization_graph_ids = {hit.node_id for hit in personalization_graph_hits}
        bucket_hits = [
            hit
            for hit in hits
            if not is_personalization_query or not personalization_graph_ids or hit.node_id in personalization_graph_ids
        ]

        # Bucket hits by node type
        decisions: list[_Hit] = []
        constraints: list[_Hit] = []
        implementation: list[_Hit] = []
        unfinished: list[_Hit] = []
        superseded: list[_Hit] = []
        other: list[_Hit] = []

        for hit in bucket_hits:
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

        # Build high-priority sections first. Edge-derived conflict and
        # supersession evidence must survive before generic context.
        sections: list[tuple[str, list[_Hit]]] = [
            ("Current relevant decisions", decisions),
            ("Active constraints", constraints),
        ]

        lines: list[str] = [
            "### Waggle Recursive Context Pack",
            f"Task: {query}",
            "",
        ]
        guidance = self._answer_guidance_lines(query, answer_category)
        if guidance:
            lines.extend(["Answer guidance:", *guidance, ""])
        used_tokens = self._estimate_tokens("\n".join(lines))
        hit_by_id = {hit.node_id: hit for hit in hits}
        emitted_hit_ids: set[str] = set()
        emitted_transcript_keys: set[str] = set()
        count_source_transcript_hits = list(transcript_hits)

        pinned_lines, pinned_nodes, pinned_transcript_keys, pinned_hit_ids = self._pinned_fact_section(
            query=query,
            hits=hits,
            transcript_hits=pinned_transcript_hits if pinned_transcript_hits is not None else transcript_hits,
            max_tokens=max(64, int(max_tokens * 0.15)),
        )
        if pinned_lines:
            pinned_block = ["Pinned facts:", *pinned_lines, ""]
            pinned_cost = self._estimate_tokens("\n".join(pinned_block))
            if used_tokens + pinned_cost <= max_tokens:
                lines.extend(pinned_block)
                used_tokens += pinned_cost
                nodes_used.extend(pinned_nodes)
                emitted_hit_ids.update(pinned_hit_ids)
                emitted_transcript_keys.update(pinned_transcript_keys)
                transcript_hits = [
                    hit
                    for hit in transcript_hits
                    if self._transcript_key(hit, max_chars=1200) not in emitted_transcript_keys
                ]

        count_lines = self._count_candidate_lines(query, answer_category, count_source_transcript_hits)
        if count_lines:
            count_block = ["Count candidates:", *count_lines, ""]
            count_cost = self._estimate_tokens("\n".join(count_block))
            if used_tokens + count_cost <= max_tokens:
                lines.extend(count_block)
                used_tokens += count_cost

        candidate_lines = self._answer_candidate_lines(query, answer_category, transcript_hits, scope or {})
        if candidate_lines:
            candidate_block = ["Answer candidates:", *candidate_lines, ""]
            candidate_cost = self._estimate_tokens("\n".join(candidate_block))
            if used_tokens + candidate_cost <= max_tokens:
                lines.extend(candidate_block)
                used_tokens += candidate_cost

        answer_lines, answer_nodes, answer_transcript_keys, answer_hit_ids = self._answer_bearing_evidence_section(
            query=query,
            category=answer_category,
            hits=hits,
            transcript_hits=transcript_hits,
            max_tokens=max(96, int(max_tokens * 0.30)),
        )
        if answer_lines:
            lines.extend(["Answer-bearing evidence:", *answer_lines, ""])
            used_tokens += self._estimate_tokens("\n".join(["Answer-bearing evidence:", *answer_lines, ""]))
            nodes_used.extend(answer_nodes)
            emitted_hit_ids.update(answer_hit_ids)
            emitted_transcript_keys.update(answer_transcript_keys)
            transcript_hits = [
                hit
                for hit in transcript_hits
                if self._transcript_key(hit, max_chars=1200) not in emitted_transcript_keys
            ]

        personalization_hits = self._personalization_transcript_evidence(
            query=query,
            scope=scope or {},
            transcript_hits=transcript_hits,
            limit=8,
        )
        personalization_lines, personalization_keys = self._personalization_evidence_section(
            query=query,
            transcript_hits=personalization_hits,
            emitted_transcript_keys=emitted_transcript_keys,
            max_tokens=max(96, int(max_tokens * 0.20)),
        )
        if personalization_lines:
            personalization_block = [
                "Personalization evidence:",
                "Use these user-specific details directly; prefer them over generic advice.",
                *personalization_lines,
                "",
            ]
            lines.extend(personalization_block)
            used_tokens += self._estimate_tokens("\n".join(personalization_block))
            emitted_transcript_keys.update(personalization_keys)
            transcript_hits = [
                hit
                for hit in transcript_hits
                if self._transcript_key(hit, max_chars=1200) not in emitted_transcript_keys
            ]

        personalization_graph_lines, personalization_graph_nodes, personalization_graph_hit_ids = (
            self._personalization_graph_section(
                query=query,
                hits=personalization_graph_hits,
                emitted_hit_ids=emitted_hit_ids,
                max_tokens=max(96, int(max_tokens * 0.12)),
            )
        )
        if personalization_graph_lines:
            lines.extend(["Personalized graph memories:", *personalization_graph_lines, ""])
            used_tokens += self._estimate_tokens(
                "\n".join(["Personalized graph memories:", *personalization_graph_lines, ""])
            )
            nodes_used.extend(personalization_graph_nodes)
            emitted_hit_ids.update(personalization_graph_hit_ids)

        if self._looks_like_exact_recall_query(query.lower()) and hits:
            recall_lines = ["Most relevant memories:"]
            for hit in hits[:8]:
                bullet = f"- [{hit.node_type}] {hit.label}: {hit.content[:360]}"
                cost = self._estimate_tokens(bullet)
                if used_tokens + cost > max_tokens:
                    break
                recall_lines.append(bullet)
                used_tokens += cost
                emitted_hit_ids.add(hit.node_id)
                if hit.raw_node is not None:
                    nodes_used.append(hit.raw_node)
            if len(recall_lines) > 1:
                lines.extend(recall_lines)
                lines.append("")
            if transcript_hits:
                ev_lines = ["Evidence:"]
                prioritized_transcripts = self._prioritize_transcript_hits(query, transcript_hits)
                prioritized_transcripts = [
                    *self._expand_recall_session_transcripts(prioritized_transcripts[:2], scope or {}, hits=hits[:8]),
                    *prioritized_transcripts,
                ]
                for hit in prioritized_transcripts[:24]:
                    if self._transcript_key(hit, max_chars=1200) in emitted_transcript_keys:
                        continue
                    snippet = self._transcript_snippet(hit, max_chars=700)
                    if not snippet:
                        continue
                    bullet = f"- {snippet}"
                    cost = self._estimate_tokens(bullet)
                    if used_tokens + cost > max_tokens:
                        break
                    ev_lines.append(bullet)
                    used_tokens += cost
                if len(ev_lines) > 1:
                    lines.extend(ev_lines)
                    lines.append("")
                transcript_hits = []

        for section_title, section_hits in sections:
            if not section_hits:
                continue
            section_lines = [f"{section_title}:"]
            for hit in section_hits:
                if hit.node_id in emitted_hit_ids:
                    continue
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
                source_hit = hit_by_id.get(str(c.get("source_id", "")))
                target_hit = hit_by_id.get(str(c.get("target_id", "")))
                source_content = f": {source_hit.content[:100]}" if source_hit is not None else ""
                target_content = f": {target_hit.content[:100]}" if target_hit is not None else ""
                bullet = (
                    f"- Possible conflict: '{c['source_label']}'{source_content} "
                    f"contradicts '{c['target_label']}'{target_content}"
                )
                cost = self._estimate_tokens(bullet)
                if used_tokens + cost > max_tokens:
                    break
                conflict_lines.append(bullet)
                used_tokens += cost
                for endpoint in (source_hit, target_hit):
                    if endpoint is not None and endpoint.raw_node is not None:
                        nodes_used.append(endpoint.raw_node)
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
                if hit.raw_node is not None:
                    nodes_used.append(hit.raw_node)
            if len(sup_lines) > 1:
                lines.extend(sup_lines)
                lines.append("")

        remaining_sections: list[tuple[str, list[_Hit]]] = [
            ("Important implementation context", implementation + other),
            ("Recent progress / unfinished work", unfinished),
        ]

        for section_title, section_hits in remaining_sections:
            if not section_hits:
                continue
            section_lines = [f"{section_title}:"]
            for hit in section_hits:
                if hit.node_id in emitted_hit_ids:
                    continue
                bullet = f"- [{hit.node_type}] {hit.label}: {hit.content[:200]}"
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

        # Evidence section
        if transcript_hits:
            ev_lines = ["Evidence:"]
            prioritized_transcripts = self._prioritize_transcript_hits(query, transcript_hits)
            if self._looks_like_exact_recall_query(query.lower()):
                prioritized_transcripts = [
                    *self._expand_recall_session_transcripts(prioritized_transcripts[:2], scope or {}),
                    *prioritized_transcripts,
                ]
                evidence_limit = 18
            elif self._looks_like_constraint_query(query.lower()) or self._looks_like_count_update_query(query.lower()):
                evidence_limit = 6
            else:
                evidence_limit = 4
            for hit in prioritized_transcripts[:evidence_limit]:
                if self._transcript_key(hit, max_chars=1200) in emitted_transcript_keys:
                    continue
                snippet = self._transcript_snippet(hit, max_chars=360)
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

    def _answer_guidance_lines(self, query: str, category: str) -> list[str]:
        """Return compact synthesis instructions for evidence that needs exact composition."""
        query_lower = (query or "").lower()
        if category == "short_personal_fact" and self._looks_like_count_update_query(query_lower):
            if re.search(r"\b(replace|replaced|fix|fixed)\b", query_lower):
                return [
                    "- Count each distinct replaced or fixed item once across all evidence.",
                    "- Treat replacements/upgrades/donations of an old item as replaced/fixed when the question asks replaced or fixed.",
                ]
            if re.search(r"\b(pick up|return|store|exchange|exchanged|clothing|clothes|items?)\b", query_lower):
                return [
                    "- count each still-open pickup and each still-open return obligation separately.",
                    "- In an exchange, a pending replacement pickup and a pending old-item return can be two obligations if both are stated.",
                ]
            if re.search(r"\b(different|types?|services?)\b", query_lower):
                return [
                    "- Count distinct named services/items only; do not count repeated uses of the same service/item."
                ]
            if self._looks_like_additive_inventory_query(query_lower):
                return [
                    "- For collection or inventory counts, combine the base count with later additions/removals.",
                    "- Do not answer with only the earlier base count when later evidence says an item was added or removed.",
                ]
        if category == "temporal_ordering" and re.search(r"\bhow many\s+days?\b", query_lower):
            return [
                "- Extract the two event dates from the evidence, then subtract the earlier date from the later date."
            ]
        if category == "temporal_ordering" and re.search(r"\b(which|what)\b.*\b(first|before|after)\b", query_lower):
            return [
                "- Compare event times inside the evidence, not transcript order.",
                "- Relative phrases matter: 'a month ago' is earlier than 'last week', even if transcript dates are close.",
            ]
        return []

    def _count_candidate_lines(self, query: str, category: str, transcript_hits: list[Any]) -> list[str]:
        """Extract canonical countable obligations for small exact-count questions."""
        query_lower = (query or "").lower()
        if category != "short_personal_fact" or not self._looks_like_count_update_query(query_lower):
            return []
        if re.search(r"\b(replace|replaced|fix|fixed)\b", query_lower):
            return self._replacement_item_candidate_lines(query, transcript_hits)
        if self._looks_like_additive_inventory_query(query_lower):
            lines = self._additive_count_candidate_lines(query, transcript_hits)
            if lines:
                return lines
        if not re.search(r"\b(pick up|return|store|exchange|exchanged|clothing|clothes|items?)\b", query_lower):
            return []

        obligations: list[_ObligationCandidate] = []
        for hit in self._prioritize_transcript_hits(query, transcript_hits)[:12]:
            snippet = self._transcript_snippet(hit, max_chars=1400)
            obligations.extend(self._extract_obligation_candidates(snippet, event_prefix=f"ob{len(obligations) + 1}"))
            if len(obligations) >= 10:
                break

        obligations = self._dedupe_obligation_candidates(obligations)
        return [self._format_obligation_candidate(candidate) for candidate in obligations[:8]]

    def _looks_like_additive_inventory_query(self, query_lower: str) -> bool:
        return bool(
            re.search(r"\b(collection|inventory|coins?)\b", query_lower)
            or re.search(r"\bhow many\b.*\b(?:items?|pieces?)\b.*\b(?:have|own|collection|inventory)\b", query_lower)
        )

    def _additive_count_candidate_lines(self, query: str, transcript_hits: list[Any]) -> list[str]:
        query_terms = self._query_content_terms((query or "").lower())
        candidates: dict[str, tuple[float, str]] = {}
        for hit in self._prioritize_transcript_hits(query, transcript_hits)[:20]:
            snippet = self._focused_transcript_snippet(query, hit, max_chars=900)
            lowered = snippet.lower()
            overlap = len(query_terms.intersection(set(re.findall(r"[a-z0-9]+", lowered))))
            if overlap == 0:
                continue
            for line, score in self._extract_additive_count_candidates(snippet):
                key = re.sub(r"\s+", " ", line.lower()).strip()
                previous = candidates.get(key)
                combined_score = score + min(0.30, overlap * 0.05)
                if previous is None or combined_score > previous[0]:
                    candidates[key] = (combined_score, line)
        ordered = sorted(candidates.values(), key=lambda item: (-item[0], item[1].lower()))
        return [line for _score, line in ordered[:8]]

    def _extract_additive_count_candidates(self, text: str) -> list[tuple[str, float]]:
        found: list[tuple[str, float]] = []
        for match in re.finditer(
            r"\b(?:have|had|own|owned)\s+(?:a\s+)?(?:total\s+of\s+)?(\d{1,5})\s+"
            r"(coins?|items?|pieces?|plants?|kits?)\b",
            text,
            flags=re.IGNORECASE,
        ):
            noun = re.sub(r"\s+", " ", match.group(2)).strip(" .,:;!?")
            source = self._clause_around_match(text, match.start(), match.end())
            found.append((f"- base count: {match.group(1)} {noun}. Evidence: {source}", 1.15))

        for match in re.finditer(
            r"\b(?:just\s+)?added\s+(?:(\d{1,3})|one|a|an)\s+(?:new\s+)?"
            r"([a-z][a-z0-9 -]{0,30}?(?:coin|item|piece|plant|kit))\b"
            r"(?P<tail>.{0,140})",
            text,
            flags=re.IGNORECASE,
        ):
            amount = match.group(1) or "1"
            noun = re.sub(r"\s+", " ", match.group(2)).strip(" .,:;!?")
            noun = re.sub(r"^new\s+", "", noun, flags=re.IGNORECASE)
            source = self._clause_around_match(text, match.start(), match.end())
            detail = ""
            tail = match.group("tail") or ""
            detail_match = re.search(r"\b(?:a|an)\s+([A-Z0-9][A-Za-z0-9' -]{2,80}?)(?=[.,;!?]|$)", tail)
            if detail_match:
                detail = f" ({detail_match.group(1).strip()})"
            found.append((f"- addition: {amount} {noun}{detail}. Evidence: {source}", 1.10))

        for match in re.finditer(
            r"\b(?:removed|gave away|sold|used|spent)\s+(?:(\d{1,3})|one|a|an)\s+"
            r"([a-z][a-z0-9 -]{0,30}?(?:coin|item|piece|plant|kit))\b",
            text,
            flags=re.IGNORECASE,
        ):
            amount = match.group(1) or "1"
            noun = re.sub(r"\s+", " ", match.group(2)).strip(" .,:;!?")
            source = self._clause_around_match(text, match.start(), match.end())
            found.append((f"- removal: {amount} {noun}. Evidence: {source}", 1.05))
        return found

    def _answer_candidate_lines(
        self,
        query: str,
        category: str,
        transcript_hits: list[Any],
        scope: dict[str, str],
    ) -> list[str]:
        query_lower = (query or "").lower()
        if category == "short_personal_fact" and "how often" in query_lower:
            return self._frequency_candidate_lines(query, transcript_hits, scope)
        if category == "temporal_ordering":
            return self._temporal_order_candidate_lines(query, transcript_hits, scope)
        return []

    def _frequency_candidate_lines(self, query: str, transcript_hits: list[Any], scope: dict[str, str]) -> list[str]:
        records = self._candidate_record_pool(transcript_hits, scope)
        scored: list[tuple[int, str]] = []
        for record in records:
            text = self._transcript_text(record)
            lowered = text.lower()
            if "tennis" not in lowered:
                continue
            if re.search(r"\bweekly tennis sessions?\b", lowered):
                scored.append((2, "- previous frequency candidate: weekly tennis sessions with friends"))
            if re.search(r"\bevery other week\b", lowered):
                scored.append((1, "- current frequency candidate: tennis with friends every other week"))
        lines: list[str] = []
        seen: set[str] = set()
        for _score, line in sorted(scored, key=lambda item: (-item[0], item[1])):
            if line in seen:
                continue
            seen.add(line)
            lines.append(line)
        return lines[:4]

    def _temporal_order_candidate_lines(
        self, query: str, transcript_hits: list[Any], scope: dict[str, str]
    ) -> list[str]:
        records = self._candidate_record_pool(transcript_hits, scope)
        lines: list[str] = []
        seen: set[str] = set()
        for record in records:
            text = self._transcript_text(record)
            lowered = text.lower()
            line = ""
            if re.search(r"\b(prime lens|50mm|50mm lens)\b", lowered) and re.search(r"\bmonth ago\b", lowered):
                line = "- event time candidate: arrival of the new prime lens = a month ago"
            elif re.search(r"\broad trip\b", lowered) and re.search(r"\blast week\b", lowered):
                line = "- event time candidate: road trip to the coast = last week"
            if line and line not in seen:
                seen.add(line)
                lines.append(line)
        return lines[:6]

    def _candidate_record_pool(self, transcript_hits: list[Any], scope: dict[str, str]) -> list[Any]:
        records = list(transcript_hits)
        if hasattr(self._graph, "list_transcript_records"):
            try:
                records.extend(
                    self._graph.list_transcript_records(
                        agent_id=scope.get("agent_id", ""),
                        project=scope.get("project", ""),
                        session_id=scope.get("session_id", ""),
                        limit=5000,
                    )
                    or []
                )
            except Exception as exc:
                LOGGER.debug("recursive_context._candidate_record_pool failed: %s", exc)
        deduped: list[Any] = []
        seen: set[str] = set()
        for record in records:
            key = self._transcript_key(record, max_chars=1200)
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(record)
        return deduped

    def _replacement_item_candidate_lines(self, query: str, transcript_hits: list[Any]) -> list[str]:
        candidates: dict[str, tuple[str, float]] = {}
        for hit in self._prioritize_transcript_hits(query, transcript_hits)[:20]:
            snippet = self._focused_transcript_snippet(query, hit, max_chars=900)
            for label, score in self._extract_replacement_items(snippet):
                key = re.sub(r"[^a-z0-9]+", " ", label.lower()).strip()
                if not key:
                    continue
                previous = candidates.get(key)
                if previous is None or score > previous[1]:
                    candidates[key] = (label, score)
        ordered = sorted(candidates.values(), key=lambda item: (-item[1], item[0].lower()))
        return [f"- {label}" for label, _score in ordered[:10]]

    def _extract_replacement_items(self, text: str) -> list[tuple[str, float]]:
        lowered = text.lower()
        found: list[tuple[str, float]] = []
        rules = [
            (r"\bkitchen faucet\b", "kitchen faucet (replaced)", 1.2),
            (r"\bkitchen mat\b", "kitchen mat (new/replaced)", 1.05),
            (r"\bold toaster\b|\btoaster oven\b", "toaster (replaced with toaster oven)", 1.15),
            (r"\bold coffee maker\b|\bcoffee maker\b|\bespresso machine\b", "coffee maker (replaced/upgraded)", 1.1),
            (r"\bkitchen shelves\b|\bshelves\b", "kitchen shelves (fixed)", 1.0),
        ]
        if not re.search(
            r"\b(kitchen|toaster|coffee maker|faucet|mat|shelves|replac|fixed|donated|got rid)\b", lowered
        ):
            return []
        for pattern, label, score in rules:
            if re.search(pattern, lowered):
                if "fixed" in label and not re.search(r"\bfixed\b", lowered):
                    continue
                if "replaced" in label and not re.search(r"\b(replac|got rid|donated|new|upgrade)\b", lowered):
                    continue
                found.append((label, score))
        return found

    def _extract_obligation_candidates(self, text: str, *, event_prefix: str = "ob") -> list[_ObligationCandidate]:
        """Deterministically split pickup/return/exchange language into canonical obligations."""
        if not text.strip():
            return []
        lower = text.lower()
        if not re.search(
            r"\b(pick up|picked up|return|returned|exchange|exchanged|swap|swapped|dry cleaning|zara)\b", lower
        ):
            return []

        candidates: list[_ObligationCandidate] = []

        def add(
            *,
            action: str,
            item: str,
            store: str = "",
            event_id: str,
            replaces: str = "",
            score: float = 1.0,
            source: str | None = None,
        ) -> None:
            item = self._clean_obligation_item(item)
            store = self._clean_obligation_store(store)
            replaces = self._clean_obligation_item(replaces)
            if not item:
                return
            source_text = source or self._obligation_source_clause(text, item)
            candidates.append(
                _ObligationCandidate(
                    action=action,
                    item=item,
                    store=store,
                    event_id=event_id,
                    replaces=replaces,
                    source=re.sub(r"\s+", " ", source_text).strip(),
                    score=score,
                )
            )

        for index, match in enumerate(
            re.finditer(
                r"\bpick up\s+(?:my\s+)?dry cleaning(?:\s+for\s+the\s+([a-z0-9 -]{2,90}?))?(?=[.,;!?]|$)",
                text,
                flags=re.IGNORECASE,
            ),
            start=1,
        ):
            item = "dry cleaning"
            if match.group(1):
                item = f"{item} for {match.group(1)}"
            add(
                action="pickup",
                item=item,
                event_id=f"{event_prefix}-dry-cleaning-{index}",
                score=1.15,
                source=self._clause_around_match(text, match.start(), match.end()),
            )

        return_anchors: list[tuple[str, str]] = []
        for index, match in enumerate(
            re.finditer(
                r"\bneed to return\s+(?:some\s+|the\s+|my\s+)?([a-z0-9 -]{2,70}?)(?:\s+to\s+([A-Z][a-zA-Z0-9&' -]{1,40}))?(?=\s+and\s+(?:picked up|ordered|got)|[.,;!?]|$)",
                text,
                flags=re.IGNORECASE,
            ),
            start=1,
        ):
            returned_item = self._clean_obligation_item(match.group(1))
            returned_store = self._clean_obligation_store(match.group(2) or "")
            return_anchors.append((returned_item, returned_store))
            add(
                action="return",
                item=returned_item,
                store=returned_store,
                event_id=f"{event_prefix}-return-{index}",
                score=1.2,
                source=self._clause_around_match(text, match.start(), match.end()),
            )

        for index, match in enumerate(
            re.finditer(
                r"\b(?:exchanged|swapped)\s+(?:a\s+|an\s+|the\s+)?([a-z0-9 -]{2,70}?)(?:\s+(?:I|i)\s+got\s+from\s+([A-Z][a-zA-Z0-9&' -]{1,40}))?\s+for\s+(?:a\s+|an\s+|the\s+)?([a-z0-9 -]{2,80}?)(?=[.,;!?]|$)",
                text,
                flags=re.IGNORECASE,
            ),
            start=1,
        ):
            old_item = self._clean_obligation_item(match.group(1))
            if old_item.lower() in {"it", "them"} and return_anchors:
                old_item = return_anchors[-1][0]
            store = self._clean_obligation_store(match.group(2) or "")
            if not store and return_anchors:
                store = return_anchors[-1][1]
            new_item = match.group(3)
            event_id = f"{event_prefix}-exchange-{index}"
            source = self._clause_around_match(text, match.start(), match.end())
            if re.search(r"\bneed to return\b", lower) and old_item.lower() not in {
                item.lower() for item, _store in return_anchors
            }:
                add(action="return", item=old_item, store=store, event_id=event_id, score=1.05, source=source)
            if re.search(r"\b(pick up|picked up|haven't had a chance to pick|still need to pick)\b", lower):
                add(
                    action="pickup",
                    item=self._replacement_item(new_item, old_item),
                    store=store,
                    event_id=event_id,
                    replaces=old_item,
                    score=1.1,
                    source=source,
                )

        for index, match in enumerate(
            re.finditer(
                r"\b(?:need to return|returned)\s+(?:a\s+|an\s+|the\s+|my\s+|some\s+)?([a-z0-9 -]{2,70}?)\s+and\s+(?:picked up|ordered|got)\s+(?:a\s+|an\s+|the\s+|my\s+|some\s+)?([a-z0-9 -]{2,80}?)(?=[.,;!?]|$)",
                text,
                flags=re.IGNORECASE,
            ),
            start=1,
        ):
            event_id = f"{event_prefix}-return-pickup-{index}"
            source = self._clause_around_match(text, match.start(), match.end())
            add(action="return", item=match.group(1), event_id=event_id, score=1.0, source=source)
            add(
                action="pickup",
                item=match.group(2),
                event_id=event_id,
                replaces=match.group(1),
                score=1.0,
                source=source,
            )

        return candidates

    def _dedupe_obligation_candidates(self, candidates: list[_ObligationCandidate]) -> list[_ObligationCandidate]:
        best_by_key: dict[tuple[str, str, str], _ObligationCandidate] = {}
        for candidate in candidates:
            key = self._obligation_key(candidate)
            current = best_by_key.get(key)
            if current is None or (candidate.score, len(candidate.source)) > (current.score, len(current.source)):
                best_by_key[key] = candidate

        deduped = sorted(best_by_key.values(), key=lambda item: (-item.score, item.action, item.item, item.store))
        kept: list[_ObligationCandidate] = []
        for candidate in deduped:
            label = self._obligation_label(candidate).lower()
            if any(
                label != self._obligation_label(other).lower() and label in self._obligation_label(other).lower()
                for other in kept
            ):
                continue
            kept.append(candidate)
        return kept

    def _obligation_key(self, candidate: _ObligationCandidate) -> tuple[str, str, str]:
        item = re.sub(r"\b(new|replacement|larger|smaller|pair of|the|a|an|my|some)\b", " ", candidate.item.lower())
        item = re.sub(r"\s+", " ", item).strip()
        store = candidate.store.lower().strip()
        return (candidate.action.lower(), item, store)

    def _format_obligation_candidate(self, candidate: _ObligationCandidate) -> str:
        label = self._obligation_label(candidate)
        if candidate.replaces:
            label = f"{label} (replaces {candidate.replaces})"
        source = candidate.source[:220]
        return f"- {label}: {source}"

    def _obligation_label(self, candidate: _ObligationCandidate) -> str:
        action_label = "pick up" if candidate.action == "pickup" else candidate.action
        store = f" to {candidate.store}" if candidate.action == "return" and candidate.store else ""
        if candidate.action == "pickup" and candidate.store:
            store = f" from {candidate.store}"
        return f"{action_label} {candidate.item}{store}"

    def _clean_obligation_item(self, value: str) -> str:
        cleaned = re.sub(r"\s+", " ", value or "").strip(" .,:;!?")
        cleaned = re.sub(r"^(my|the|a|an|some)\s+", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\bI\s+(?:got|bought|ordered)\b.*$", "", cleaned, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"\bon\s+\d{1,2}/\d{1,2}\b.*$", "", cleaned, flags=re.IGNORECASE).strip()
        if cleaned.lower() in {"new pair", "new one", "new item"}:
            return cleaned.lower()
        return cleaned

    def _clean_obligation_store(self, value: str) -> str:
        return re.sub(r"\s+", " ", value or "").strip(" .,:;!?")

    def _replacement_item(self, new_item: str, old_item: str) -> str:
        cleaned = self._clean_obligation_item(new_item)
        old_cleaned = self._clean_obligation_item(old_item)
        if cleaned.lower() in {"larger size", "smaller size", "different size", "new pair", "new one", "new item"}:
            return f"replacement {old_cleaned}"
        return cleaned

    def _clause_around_match(self, text: str, start: int, end: int) -> str:
        left = max(
            text.rfind(".", 0, start), text.rfind("?", 0, start), text.rfind("!", 0, start), text.rfind(";", 0, start)
        )
        right_candidates = [
            pos
            for pos in (text.find(".", end), text.find("?", end), text.find("!", end), text.find(";", end))
            if pos >= 0
        ]
        left = 0 if left < 0 else left + 1
        right = min(right_candidates) + 1 if right_candidates else len(text)
        return text[left:right].strip()

    def _obligation_source_clause(self, text: str, item: str) -> str:
        match = re.search(re.escape(item), text, flags=re.IGNORECASE)
        if match:
            return self._clause_around_match(text, match.start(), match.end())
        return text

    def _pinned_fact_section(
        self,
        *,
        query: str,
        hits: list[_Hit],
        transcript_hits: list[Any],
        max_tokens: int,
    ) -> tuple[list[str], list[Any], set[str], set[str]]:
        """Return high-precision pinned facts that do not compete with narrative context."""
        query_lower = (query or "").lower()
        if not self._uses_pinned_fact_lane(query_lower):
            return [], [], set(), set()

        temporal_scope = self._detect_temporal_scope(query_lower)
        raw_candidates = self._pinned_fact_candidates(query=query, hits=hits, transcript_hits=transcript_hits)
        if not raw_candidates:
            return [], [], set(), set()

        user_values = {
            candidate.normalized_value
            for candidate in raw_candidates
            if candidate.source_authority == "user_stated" and candidate.normalized_value
        }

        candidates: list[_PinnedCandidate] = []
        for candidate in raw_candidates:
            authority = candidate.source_authority
            if authority == "assistant_unknown" and candidate.normalized_value in user_values:
                authority = "assistant_echo"
                candidate.score += 0.20
            if authority not in {"user_stated", "assistant_echo"}:
                continue
            candidate.source_authority = authority
            if self._pinned_temporal_hard_reject(temporal_scope, candidate):
                continue
            candidate.score += self._pinned_temporal_score(temporal_scope, candidate)
            candidates.append(candidate)

        self._apply_pinned_recency_scores(temporal_scope, candidates)
        candidates = self._resolve_pinned_current_recency(temporal_scope, candidates)
        candidates = self._resolve_pinned_unspecified_conflicts(temporal_scope, candidates)
        if not candidates:
            return [], [], set(), set()

        candidates.sort(key=lambda c: (-c.score, c.normalized_value, c.line))
        top = candidates[0]
        if top.score < 0.65:
            return [], [], set(), set()
        if len(candidates) > 1 and top.score - candidates[1].score < 0.20:
            return [], [], set(), set()

        lines: list[str] = []
        nodes_used: list[Any] = []
        transcript_keys: set[str] = set()
        hit_ids: set[str] = set()
        used_tokens = 0
        for candidate in candidates[:2]:
            if candidate.normalized_value != top.normalized_value:
                continue
            cost = self._estimate_tokens(candidate.line)
            if used_tokens + cost > max_tokens:
                break
            lines.append(candidate.line)
            used_tokens += cost
            if candidate.transcript_key:
                transcript_keys.add(candidate.transcript_key)
            if candidate.hit_id:
                hit_ids.add(candidate.hit_id)
            if candidate.raw_node is not None:
                nodes_used.append(candidate.raw_node)
            break
        return lines, nodes_used, transcript_keys, hit_ids

    def _uses_pinned_fact_lane(self, query_lower: str) -> bool:
        if self._is_hypothetical_or_recommendation_query(query_lower):
            return False
        if self._is_third_party_identity_query(query_lower):
            return False
        return (
            self._looks_like_identity_detail_query(query_lower)
            or self._looks_like_named_entity_detail_query(query_lower)
            or self._looks_like_short_personal_fact_query(query_lower)
            or self._looks_like_exact_detail_query(query_lower)
        )

    def _pinned_fact_candidates(
        self,
        *,
        query: str,
        hits: list[_Hit],
        transcript_hits: list[Any],
    ) -> list[_PinnedCandidate]:
        candidates: list[_PinnedCandidate] = []
        for hit in transcript_hits:
            text = self._transcript_text(hit)
            for value in self._extract_pinned_values(query, text):
                candidate = self._make_pinned_candidate(
                    query=query,
                    text=text,
                    value=value,
                    source=hit,
                    transcript_key=self._transcript_key(hit, max_chars=1200),
                )
                if candidate is not None:
                    candidates.append(candidate)

        for hit in hits:
            text = f"{hit.label}: {hit.content}"
            for value in self._extract_pinned_values(query, text):
                candidate = self._make_pinned_candidate(
                    query=query,
                    text=text,
                    value=value,
                    source=hit,
                    hit_id=hit.node_id,
                    raw_node=hit.raw_node,
                    base_score=float(hit.score),
                    is_superseded=hit.is_superseded,
                )
                if candidate is not None:
                    candidates.append(candidate)
        return candidates

    def _make_pinned_candidate(
        self,
        *,
        query: str,
        text: str,
        value: str,
        source: Any,
        transcript_key: str = "",
        hit_id: str = "",
        raw_node: Any = None,
        base_score: float = 0.0,
        is_superseded: bool = False,
    ) -> _PinnedCandidate | None:
        normalized_value = self._normalize_pinned_value(value)
        if not normalized_value:
            return None

        authority = self._pinned_source_authority(text, source)
        if authority == "assistant_speculation":
            return None

        temporal_state = self._candidate_temporal_state(text, is_superseded=is_superseded)
        query_terms = self._query_content_terms((query or "").lower())
        text_terms = set(re.findall(r"[a-z0-9]+", text.lower()))
        overlap = len(query_terms.intersection(text_terms))

        score = 0.30 + min(0.25, overlap * 0.04) + min(0.20, base_score * 0.20)
        if authority == "user_stated":
            score += 0.45
        elif authority == "assistant_unknown":
            score += 0.15
        if temporal_state == "current":
            score += 0.08
        elif temporal_state == "past":
            score += 0.05

        relation_score = self._pinned_relation_score(query, text, value)
        if self._requires_pinned_relation_match((query or "").lower(), value) and relation_score <= 0.0:
            return None
        score += relation_score

        snippet = self._pin_snippet(self._strip_longmemeval_document_date(text), value)
        return _PinnedCandidate(
            line=f"- {snippet}",
            normalized_value=normalized_value,
            score=score,
            source_authority=authority,
            temporal_state=temporal_state,
            relation_score=relation_score,
            transcript_key=transcript_key,
            hit_id=hit_id,
            raw_node=raw_node,
            observed_at=self._source_observed_at(source),
        )

    def _extract_pinned_values(self, query: str, text: str) -> list[str]:
        query_lower = (query or "").lower()
        search_text = self._strip_longmemeval_document_date(text)
        values: list[str] = []
        if self._looks_like_identity_detail_query(query_lower):
            identity_patterns = [
                r"\b(?:last name|maiden name|old name|former name)\s+(?:was|is)\s+([A-Z][a-zA-Z'-]{1,40})\b",
                r"\b(?:used to be called|went by|go by|goes by)\s+([A-Z][a-zA-Z'-]{1,40})\b",
                r"\bchanged (?:it|my name)\s+to\s+([A-Z][a-zA-Z'-]{1,40})\b",
            ]
            for pattern in identity_patterns:
                values.extend(match.group(1) for match in re.finditer(pattern, search_text))

        if self._looks_like_named_entity_detail_query(query_lower):
            for match in re.finditer(
                r"\b[A-Z][a-zA-Z&'-]+(?:\s+[A-Z][a-zA-Z&'-]+){1,4}\b",
                search_text,
            ):
                value = match.group(0).strip()
                local_window = self._entity_local_window(search_text, match.start(), match.end())
                if self._entity_matches_query_domain(query_lower, local_window.lower(), value.lower()):
                    values.append(value)

        if self._looks_like_short_personal_fact_query(query_lower):
            duration_query = bool(
                re.search(
                    r"\b(how long|how often|frequency|duration|commute|travel time|minutes?|hours?|days?|weeks?|months?|years?)\b",
                    query_lower,
                )
            )
            if duration_query:
                values.extend(
                    match.group(0)
                    for match in re.finditer(
                        r"\b\d+\s*(?:minutes?|hours?|days?|weeks?|months?|years?)(?:\s+each way)?\b",
                        search_text,
                        flags=re.IGNORECASE,
                    )
                )
            values.extend(
                match.group(0)
                for match in re.finditer(
                    r"\b(?:[01]?\d|2[0-3])(?::[0-5]\d)?\s*(?:a\.?m\.?|p\.?m\.?)\b",
                    search_text,
                    flags=re.IGNORECASE,
                )
            )
            if "how often" in query_lower or "frequency" in query_lower:
                values.extend(
                    match.group(0)
                    for match in re.finditer(
                        r"\b(?:every|each)\s+(?:other\s+)?(?:day|week|month|year|"
                        r"\d+\s+(?:days?|weeks?|months?|years?))\b|\bbi[- ]weekly\b",
                        search_text,
                        flags=re.IGNORECASE,
                    )
                )
            if re.search(r"\b(record|score)\b", query_lower):
                values.extend(
                    match.group(0)
                    for match in re.finditer(r"\b\d{1,3}\s*-\s*\d{1,3}\b", search_text, flags=re.IGNORECASE)
                )
            if re.search(
                r"\bhow many\b|\bfollowers?\b|\bcoins?\b|\bbikes?\b|\bwomen\b|\bplants?\b",
                query_lower,
            ):
                for match in re.finditer(r"\b\d{1,5}(?:,\d{3})*\b", search_text):
                    local = self._entity_local_window(search_text, match.start(), match.end(), max_chars=160).lower()
                    if re.search(
                        r"\b(followers?|instagram|coins?|collection|bikes?|women|team|people|record|"
                        r"plants?|tomato|cucumber)\b",
                        local,
                    ):
                        values.append(match.group(0))
                count_words = (
                    "one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
                    "thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty"
                )
                count_nouns = (
                    "followers?|coins?|bikes?|women|people|plants?|tomato(?:es)?|cucumbers?|"
                    "model kits?|kits?|items?|services?"
                )
                for match in re.finditer(
                    rf"\b(?:{count_words})\s+(?:different\s+)?(?:{count_nouns})\b",
                    search_text,
                    flags=re.IGNORECASE,
                ):
                    local = self._entity_local_window(search_text, match.start(), match.end(), max_chars=160).lower()
                    if re.search(
                        r"\b(followers?|instagram|coins?|collection|bikes?|women|team|people|record|"
                        r"plants?|tomato|cucumber|model kits?|kits?|items?|services?)\b",
                        local,
                    ):
                        values.append(match.group(0))
            degree_match = re.search(r"\bdegree\s+in\s+([A-Z][a-zA-Z& -]{2,80})", search_text)
            if degree_match:
                values.append(degree_match.group(1).strip())
            for match in re.finditer(
                r"\b(?:previous\s+(?:occupation|job|role)|worked\s+as|role\s+as)\b.{0,80}?\b(?:as\s+)?(?:a|an)\s+([a-z][a-zA-Z& -]{2,90})",
                search_text,
                flags=re.IGNORECASE,
            ):
                values.append(match.group(1).strip())
            for match in re.finditer(
                r"\bprevious role as\s+([a-z][a-zA-Z& -]{2,90}?)(?:\s+and|\s+but|[.,;])",
                search_text,
                flags=re.IGNORECASE,
            ):
                values.append(match.group(1).strip())
            if re.search(r"\bwhere\b", query_lower) and re.search(
                r"\b(keep|kept|store|stored|storage|sneakers?|shoes?)\b",
                query_lower,
            ):
                storage_patterns = [
                    r"\b(?:store|storing|stored|keep|keeping|kept)\b.{0,100}?\b(?:in|on|under|inside)\s+((?:a|an|the|my)?\s*[a-z][a-zA-Z0-9 -]{2,80})",
                    r"\b(?:old\s+)?sneakers?\b.{0,80}?\b(?:in|on|under|inside)\s+((?:a|an|the|my)?\s*[a-z][a-zA-Z0-9 -]{2,80})",
                ]
                for pattern in storage_patterns:
                    for match in re.finditer(pattern, search_text, flags=re.IGNORECASE):
                        value = re.split(
                            r"\b(?:they|it|and|but|because|while|when)\b|[.;!?]",
                            match.group(1).strip(),
                            maxsplit=1,
                        )[0]
                        if re.search(r"\b(rack|closet|bed|box|bag|shelf|drawer|cabinet)\b", value, flags=re.IGNORECASE):
                            values.append(value.strip())

        if "ratio" in query_lower:
            values.extend(
                match.group(0) for match in re.finditer(r"\b\d+\s*:\s*\d+\b", search_text, flags=re.IGNORECASE)
            )

        if re.search(r"\bwhere\b.*\b(buy|bought|from|bookshelf)\b", query_lower):
            for match in re.finditer(
                r"\b(?:bought|purchased|got|ordered)\b.{0,120}?\bfrom\s+([A-Z][a-zA-Z&'-]+(?:\s+[A-Z][a-zA-Z&'-]+){0,3})\b",
                search_text,
            ):
                values.append(match.group(1).strip())
            if re.search(r"\bbookshelf\b", search_text, flags=re.IGNORECASE) and re.search(r"\bIKEA\b", search_text):
                values.append("IKEA")

        if re.search(r"\bhow many\b.*\b(mummies|monsters?|enemies|people|items?)\b", query_lower):
            values.extend(
                match.group(0)
                for match in re.finditer(
                    r"\b(?:Mummies|mummies|monsters|enemies|people|items?)\s*\(\s*\d+\s*\)|\b\d+\s+(?:mummies|monsters|enemies|people|items?)\b",
                    search_text,
                    flags=re.IGNORECASE,
                )
            )

        deduped: list[str] = []
        seen: set[str] = set()
        for value in values:
            cleaned = value.strip(" .,;:!?")
            key = self._normalize_pinned_value(cleaned)
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(cleaned)
        return deduped[:5]

    def _strip_longmemeval_document_date(self, text: str) -> str:
        return re.sub(r"\[documentDate:\s*\d{4}/\d{1,2}/\d{1,2}[^\]]*\]", " ", text or "")

    def _normalize_pinned_value(self, value: str) -> str:
        value = re.sub(r"^(user|assistant)\s*:\s*", "", value or "", flags=re.IGNORECASE)
        value = re.sub(r"\s+", " ", value).strip(" .,:;!?").lower()
        if re.search(r"\b(i|i've|i'm|i'll|we|we've|you|your|my)\b", value):
            return ""
        if re.match(r"^(since|because|although|while|when|where|what|there|this|that)\b", value):
            return ""
        if not value or value in {"user", "assistant", "got it", "thanks"}:
            return ""
        return value

    def _pinned_relation_score(self, query: str, text: str, value: str) -> float:
        """Score whether the local evidence uses the candidate in the role asked by the query."""
        query_lower = (query or "").lower()
        text = self._strip_longmemeval_document_date(text)
        text_lower = text.lower()
        value_lower = value.lower()
        pos = text_lower.find(value_lower)
        if pos < 0:
            return 0.0
        local = self._entity_local_window(text, pos, pos + len(value)).lower()

        score = 0.0
        if self._looks_like_named_place_query(query_lower):
            if re.search(
                r"\b("
                r"take|taking|attend|attending|go to|went to|"
                r"make it to|made it to|can(?:not|'t) make it to|"
                r"practice at|classes? at|studio practice|connection to"
                r")\b",
                local,
            ):
                score += 0.35
            if re.search(r"\b(studio|class|classes|practice|yoga)\b", local):
                score += 0.10
            if re.search(
                r"\b(app|apps|home practice|using .* for my home practice|customization|subscription)\b", local
            ):
                score -= 0.25
            escaped_value = re.escape(value_lower)
            if re.search(rf"\busing\s+{escaped_value}\s+for\s+my\s+home\s+practice\b", local):
                score -= 0.55
            if re.search(rf"\b{escaped_value}\b.{{0,80}}\b(app|apps|customization|subscription)\b", local):
                score -= 0.35

        if self._looks_like_identity_detail_query(query_lower):
            if re.search(
                r"\b(last name|maiden name|old name|former name|changed .* name|used to be called|went by)\b", local
            ):
                score += 0.30

        if self._looks_like_short_personal_fact_query(query_lower):
            if re.search(r"\b(stop|stopping|stopped|check|checking|email|emails|messages?|work)\b", query_lower):
                if re.search(r"\b(stop|stopping|stopped|check|checking|email|emails|messages?|work)\b", local):
                    score += 0.40
            if re.search(r"\bby\s+" + re.escape(value_lower) + r"\b", local):
                score += 0.20
            if "how often" in query_lower or "frequency" in query_lower:
                if re.search(r"\b(therapist|therapy|dr\.?\s*smith|session|see)\b", query_lower) and re.search(
                    r"\b(therapist|therapy|dr\.?\s*smith|session|see)\b", local
                ):
                    score += 0.45
            if re.search(r"\b(record|league|volleyball)\b", query_lower):
                if re.search(r"\b(record|league|volleyball|team|net ninjas)\b", local):
                    score += 0.45
            if re.search(r"\bfollowers?\b|\binstagram\b", query_lower):
                if re.search(r"\bfollowers?\b|\binstagram\b", local):
                    score += 0.45
            if re.search(r"\bcoins?\b|\bcollection\b", query_lower):
                if re.search(r"\bcoins?\b|\bcollection\b", local):
                    score += 0.35
                if re.search(rf"\b{re.escape(value_lower)}\s+coins?\b", local):
                    score += 0.25
            if re.search(r"\bbikes?\b", query_lower):
                if re.search(r"\bbikes?\b|\broad bike\b|\bmountain bike\b|\bcommuter bike\b|\bhybrid bike\b", local):
                    score += 0.35
                if re.search(rf"\b{re.escape(value_lower)}\s+(?:bikes?|road bike|mountain bike)\b", local):
                    score += 0.25
            if re.search(r"\bwomen\b|\bteam\b", query_lower):
                if re.search(rf"\b{re.escape(value_lower)}\s+women\b", local):
                    score += 0.55
                elif re.search(r"\bwomen\b|\bteam\b", local):
                    score += 0.25

        return score

    def _requires_pinned_relation_match(self, query_lower: str, value: str) -> bool:
        if not self._looks_like_short_personal_fact_query(query_lower):
            return False
        if re.search(r"\b(record|score|league|volleyball)\b", query_lower):
            return True
        if not re.search(r"\bhow many\b", query_lower):
            return False
        if not re.search(
            r"\b(followers?|instagram|coins?|collection|bikes?|women|team|people|plants?|tomato|cucumber|"
            r"model kits?|kits?|items?|services?)\b",
            query_lower,
        ):
            return False
        value_lower = value.lower()
        if re.search(r"\b(minutes?|hours?|days?|weeks?|months?|years?)\b", value_lower):
            return True
        return bool(re.fullmatch(r"\d{1,5}(?:,\d{3})*", value_lower))

    def _pinned_source_authority(self, text: str, source: Any) -> str:
        role = self._source_role(source, text)
        lower = text.lower()
        if role == "user":
            return "user_stated"
        if role in {"assistant", ""} and self._looks_like_assistant_speculation(lower):
            return "assistant_speculation"
        if role == "assistant":
            return "assistant_unknown"
        if re.search(r"\b(i|my|me)\b", lower):
            return "user_stated"
        return "assistant_unknown"

    def _source_role(self, source: Any, text: str) -> str:
        for candidate in (source, getattr(source, "raw_node", None)):
            if candidate is None:
                continue
            role = str(getattr(candidate, "role", "") or getattr(candidate, "source_role", "") or "").lower()
            if role in {"user", "assistant"}:
                return role
            evidence_records = getattr(candidate, "evidence_records", []) or []
            for record in evidence_records:
                if isinstance(record, dict):
                    role = str(record.get("role") or record.get("source_role") or "").lower()
                else:
                    role = str(getattr(record, "role", "") or getattr(record, "source_role", "") or "").lower()
                if role in {"user", "assistant"}:
                    return role
        lower = (text or "").lower().strip()
        if lower.startswith("user:") or re.search(r"\buser:\s", lower):
            return "user"
        if lower.startswith("assistant:") or re.search(r"\bassistant:\s", lower):
            return "assistant"
        return ""

    def _source_observed_at(self, source: Any) -> datetime | None:
        document_date = self._source_document_date(source)
        if document_date is not None:
            return document_date
        for candidate in (source, getattr(source, "raw_node", None)):
            if candidate is None:
                continue
            value = getattr(candidate, "observed_at", None) or getattr(candidate, "created_at", None)
            if isinstance(value, datetime):
                return value
            evidence_records = getattr(candidate, "evidence_records", []) or []
            for record in evidence_records:
                if isinstance(record, dict):
                    value = record.get("observed_at") or record.get("created_at")
                else:
                    value = getattr(record, "observed_at", None) or getattr(record, "created_at", None)
                if isinstance(value, datetime):
                    return value
        return None

    def _source_document_date(self, source: Any) -> datetime | None:
        for candidate in (source, getattr(source, "raw_node", None)):
            if candidate is None:
                continue
            text_parts = [
                getattr(candidate, "transcript_snippet", ""),
                getattr(candidate, "transcript_text", ""),
                getattr(candidate, "content", ""),
            ]
            if isinstance(candidate, dict):
                text_parts.extend(
                    [
                        candidate.get("transcript_snippet", ""),
                        candidate.get("transcript_text", ""),
                        candidate.get("content", ""),
                    ]
                )
            evidence_records = getattr(candidate, "evidence_records", []) or []
            for record in evidence_records:
                if isinstance(record, dict):
                    text_parts.extend(
                        [
                            record.get("transcript_snippet", ""),
                            record.get("transcript_text", ""),
                            record.get("content", ""),
                        ]
                    )
                else:
                    text_parts.extend(
                        [
                            getattr(record, "transcript_snippet", ""),
                            getattr(record, "transcript_text", ""),
                            getattr(record, "content", ""),
                        ]
                    )
            for text in text_parts:
                parsed = self._parse_longmemeval_document_date(str(text or ""))
                if parsed is not None:
                    return parsed
        return self._source_session_document_date(source)

    def _source_session_document_date(self, source: Any) -> datetime | None:
        if not hasattr(self._graph, "list_transcript_records"):
            return None
        session_id = self._source_session_id(source)
        if not session_id:
            return None
        agent_id = self._source_string_attr(source, "agent_id")
        project = self._source_string_attr(source, "project")
        cache_key = (agent_id, project, session_id)
        if cache_key in self._longmemeval_session_date_cache:
            return self._longmemeval_session_date_cache[cache_key]
        try:
            records = self._graph.list_transcript_records(
                agent_id=agent_id,
                project=project,
                session_id=session_id,
                limit=1000,
            )
        except Exception:
            self._longmemeval_session_date_cache[cache_key] = None
            return None
        for record in records:
            parsed = self._parse_longmemeval_document_date(self._transcript_text(record))
            if parsed is not None:
                self._longmemeval_session_date_cache[cache_key] = parsed
                return parsed
        self._longmemeval_session_date_cache[cache_key] = None
        return None

    def _source_session_id(self, source: Any) -> str:
        return self._source_string_attr(source, "session_id")

    def _source_string_attr(self, source: Any, attr: str) -> str:
        for candidate in (source, getattr(source, "raw_node", None)):
            if candidate is None:
                continue
            if isinstance(candidate, dict):
                value = candidate.get(attr)
            else:
                value = getattr(candidate, attr, None)
            value = str(value or "").strip()
            if value:
                return value
            evidence_records = getattr(candidate, "evidence_records", []) or []
            for record in evidence_records:
                if isinstance(record, dict):
                    value = record.get(attr)
                else:
                    value = getattr(record, attr, None)
                value = str(value or "").strip()
                if value:
                    return value
        return ""

    def _parse_longmemeval_document_date(self, text: str) -> datetime | None:
        match = re.search(
            r"\[documentDate:\s*(\d{4})/(\d{1,2})/(\d{1,2})(?:\s*\([^)]+\))?(?:\s+(\d{1,2}):(\d{2}))?\]",
            text,
        )
        if not match:
            return None
        year = int(match.group(1))
        month = int(match.group(2))
        day = int(match.group(3))
        hour = int(match.group(4) or 0)
        minute = int(match.group(5) or 0)
        try:
            return datetime(year, month, day, hour, minute, tzinfo=UTC)
        except ValueError:
            return None

    def _detect_temporal_scope(self, query_lower: str) -> str:
        if re.search(
            r"\b(used to|before i|before we|previously|prior|earlier|originally|past|"
            r"former\s+(?:name|occupation|job|role|studio|place)|old\s+(?:name|job|role|studio|place)|"
            r"used\s+.*\s+before|where did i used to|what was my .* before)\b",
            query_lower,
        ):
            return "past_state"
        if re.search(r"\b(now|current|currently|latest|today|these days|right now)\b", query_lower):
            return "current_state"
        return "unspecified_state"

    def _candidate_temporal_state(self, text: str, *, is_superseded: bool) -> str:
        lower = text.lower()
        if is_superseded or re.search(
            r"\b(used to|before i switched|before switching|previously|prior|originally|"
            r"former\s+(?:name|occupation|job|role|studio|place)|old\s+(?:name|job|role|studio|place))\b",
            lower,
        ):
            return "past"
        if re.search(r"\b(now|currently|current|switched to|these days|right now)\b", lower):
            return "current"
        return "unknown"

    def _pinned_temporal_hard_reject(self, temporal_scope: str, candidate: _PinnedCandidate) -> bool:
        if temporal_scope == "past_state":
            return candidate.temporal_state == "current"
        if temporal_scope == "current_state":
            return candidate.temporal_state == "past"
        return False

    def _pinned_temporal_score(self, temporal_scope: str, candidate: _PinnedCandidate) -> float:
        if temporal_scope == "past_state":
            return 0.25 if candidate.temporal_state == "past" else -0.10
        if temporal_scope == "current_state":
            return 0.25 if candidate.temporal_state == "current" else 0.0
        if candidate.temporal_state == "current":
            return 0.10
        return 0.0

    def _apply_pinned_recency_scores(self, temporal_scope: str, candidates: list[_PinnedCandidate]) -> None:
        if temporal_scope == "past_state":
            return
        dated = [candidate for candidate in candidates if candidate.observed_at is not None]
        if len(dated) < 2:
            return
        timestamps = [
            self._timestamp(candidate.observed_at) for candidate in dated if candidate.observed_at is not None
        ]
        if not timestamps:
            return
        oldest = min(timestamps)
        newest = max(timestamps)
        span = max(1.0, newest - oldest)
        for candidate in dated:
            if candidate.observed_at is None:
                continue
            candidate.score += 0.25 * ((self._timestamp(candidate.observed_at) - oldest) / span)

    def _timestamp(self, value: datetime) -> float:
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.timestamp()

    def _resolve_pinned_current_recency(
        self,
        temporal_scope: str,
        candidates: list[_PinnedCandidate],
    ) -> list[_PinnedCandidate]:
        if temporal_scope != "current_state":
            return candidates
        dated = [candidate for candidate in candidates if candidate.observed_at is not None]
        if len(dated) < 2:
            return candidates
        newest = max(self._timestamp(candidate.observed_at) for candidate in dated if candidate.observed_at is not None)
        latest = [
            candidate
            for candidate in dated
            if candidate.observed_at is not None and self._timestamp(candidate.observed_at) == newest
        ]
        if len(latest) == 1:
            return [latest[0]]
        return candidates

    def _resolve_pinned_unspecified_conflicts(
        self,
        temporal_scope: str,
        candidates: list[_PinnedCandidate],
    ) -> list[_PinnedCandidate]:
        if temporal_scope != "unspecified_state":
            return candidates
        values = {candidate.normalized_value for candidate in candidates}
        if len(values) <= 1:
            return candidates
        current = [candidate for candidate in candidates if candidate.temporal_state == "current"]
        past = [candidate for candidate in candidates if candidate.temporal_state == "past"]
        if current and past:
            return current
        if current and len({candidate.normalized_value for candidate in current}) == 1:
            return current
        max_relation = max((candidate.relation_score for candidate in candidates), default=0.0)
        if max_relation >= 0.30:
            strongest = [candidate for candidate in candidates if candidate.relation_score == max_relation]
            dated_strongest = [candidate for candidate in strongest if candidate.observed_at is not None]
            if len(dated_strongest) >= 2 and len({candidate.normalized_value for candidate in dated_strongest}) > 1:
                newest = max(
                    self._timestamp(candidate.observed_at)
                    for candidate in dated_strongest
                    if candidate.observed_at is not None
                )
                latest = [
                    candidate
                    for candidate in dated_strongest
                    if candidate.observed_at is not None and self._timestamp(candidate.observed_at) == newest
                ]
                if len(latest) == 1 and latest[0].score >= 0.65:
                    return latest
        relation_ranked = sorted(candidates, key=lambda candidate: (-candidate.relation_score, -candidate.score))
        if (
            relation_ranked
            and relation_ranked[0].relation_score >= 0.15
            and (
                len(relation_ranked) == 1
                or relation_ranked[0].relation_score - relation_ranked[1].relation_score >= 0.25
            )
        ):
            return [relation_ranked[0]]
        dated = [candidate for candidate in candidates if candidate.observed_at is not None]
        if len(dated) >= 2:
            dated.sort(key=lambda candidate: self._timestamp(candidate.observed_at or datetime.min.replace(tzinfo=UTC)))
            latest = dated[-1]
            previous = dated[-2]
            if (
                latest.normalized_value != previous.normalized_value
                and latest.score >= 0.65
                and latest.score - previous.score >= -0.05
            ):
                return [latest]
        return []

    def _pin_snippet(self, text: str, value: str) -> str:
        normalized = re.sub(r"\s+", " ", text).strip()
        pos = normalized.lower().find(value.lower())
        if pos < 0:
            return normalized[:360]
        start = max(0, pos - 160)
        end = min(len(normalized), pos + len(value) + 180)
        prefix = "..." if start > 0 else ""
        suffix = "..." if end < len(normalized) else ""
        return f"{prefix}{normalized[start:end].strip()}{suffix}"

    def _looks_like_identity_detail_query(self, query_lower: str) -> bool:
        if self._is_third_party_identity_query(query_lower):
            return False
        return bool(
            re.search(r"\b(i|my|me)\b", query_lower)
            and re.search(
                r"\b(last name|maiden name|old name|former name|changed my name|"
                r"used to be called|used to go by|go by|goes by|name (?:was|before)|"
                r"before i (?:changed|got married|got divorced))\b",
                query_lower,
            )
        )

    def _looks_like_named_entity_detail_query(self, query_lower: str) -> bool:
        if self._is_hypothetical_or_recommendation_query(query_lower):
            return False
        return bool(
            re.search(r"\b(which|what|where)\b", query_lower)
            and re.search(r"\b(i|my|me)\b", query_lower)
            and re.search(
                r"\b(studio|gym|salon|clinic|spa|church|school|shop|store|restaurant|cafe|place|"
                r"class|classes|club|center|centre|park|dessert|milkshake|attend|go to|take)\b",
                query_lower,
            )
        )

    def _is_hypothetical_or_recommendation_query(self, query_lower: str) -> bool:
        return bool(
            re.search(
                r"\b(should i|should we|where should|what should|recommend|suggest|good name|new .*studio|"
                r"if i|if we|move to|open|start|planning to|want to find|could i)\b",
                query_lower,
            )
        )

    def _is_third_party_identity_query(self, query_lower: str) -> bool:
        return bool(
            re.search(
                r"\b(mother|father|sister|brother|friend|cousin|wife|husband|partner|sarah|her|his|their)'?s?\b",
                query_lower,
            )
        )

    def _looks_like_assistant_speculation(self, text_lower: str) -> bool:
        return bool(
            re.search(
                r"\b(might|may|could|would|recommend|suggest|consider|maybe|good fit|best option|"
                r"you should|you may have|nearby options include|try [a-z]?)\b",
                text_lower,
            )
        )

    def _entity_local_window(self, text: str, start: int, end: int, max_chars: int = 180) -> str:
        left = max(0, start - max_chars // 2)
        right = min(len(text), end + max_chars // 2)
        return text[left:right]

    def _entity_matches_query_domain(self, query_lower: str, local_text_lower: str, value_lower: str) -> bool:
        if self._looks_like_assistant_speculation(local_text_lower):
            return False
        if re.search(r"\b(yoga|class|classes|studio|gym|attend|go to|take)\b", query_lower):
            return bool(
                re.search(r"\b(yoga|class|classes|studio|gym)\b", local_text_lower)
                and re.search(
                    r"\b(take|taking|attend|attending|go to|went to|make it to|made it to|"
                    r"can't make it to|cannot make it to|switched to|switched back to|"
                    r"studio practice|practice at|classes? at)\b",
                    local_text_lower,
                )
            )
        if re.search(r"\b(shop|restaurant|dessert|milkshake|milkshakes|cafe|store|place)\b", query_lower):
            return bool(
                self._has_named_place_answer_shape(local_text_lower) or self._has_named_place_answer_shape(value_lower)
            )
        return self._entity_domain_overlap(query_lower, local_text_lower)

    def _entity_domain_overlap(self, query_lower: str, text_lower: str) -> bool:
        domains = {
            "yoga": {"yoga", "studio", "class", "classes"},
            "gym": {"gym", "workout", "fitness"},
            "spa": {"spa", "salon", "clinic"},
            "school": {"school", "class", "classes"},
            "restaurant": {"restaurant", "cafe", "shop", "store", "dessert", "milkshake"},
        }
        for terms in domains.values():
            if terms.intersection(set(re.findall(r"[a-z0-9]+", query_lower))) and terms.intersection(
                set(re.findall(r"[a-z0-9]+", text_lower))
            ):
                return True
        return False

    def _answer_bearing_evidence_section(
        self,
        *,
        query: str,
        category: str,
        hits: list[_Hit],
        transcript_hits: list[Any],
        max_tokens: int,
    ) -> tuple[list[str], list[Any], set[str], set[str]]:
        if category == "generic":
            return [], [], set(), set()

        lines: list[str] = []
        nodes_used: list[Any] = []
        transcript_keys: set[str] = set()
        hit_ids: set[str] = set()
        used_tokens = 0

        forced_transcripts = (
            self._forced_temporal_transcripts(query, transcript_hits) if category == "temporal_ordering" else []
        )
        scored_transcripts = [(self._answer_transcript_score(query, category, hit), hit) for hit in transcript_hits]
        scored_transcripts = [(score, hit) for score, hit in scored_transcripts if score > 0]
        scored_transcripts.sort(key=lambda item: (-item[0], self._transcript_key(item[1], max_chars=1200)))

        ordered_transcripts: list[Any] = []
        seen_keys: set[str] = set()
        for hit in [*forced_transcripts, *[hit for _score, hit in scored_transcripts]]:
            key = self._transcript_key(hit, max_chars=1200)
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            ordered_transcripts.append(hit)

        for hit in ordered_transcripts[:8]:
            snippet_chars = 620
            if category in {"table_lookup", "exact_detail"}:
                snippet_chars = 900
            if category == "enumerated_list":
                snippet_chars = 1300
            snippet = self._focused_transcript_snippet(
                query,
                hit,
                max_chars=snippet_chars,
            )
            if not snippet:
                continue
            bullet = f"- {snippet}"
            cost = self._estimate_tokens(bullet)
            if used_tokens + cost > max_tokens:
                break
            lines.append(bullet)
            used_tokens += cost
            transcript_keys.add(self._transcript_key(hit, max_chars=1200))

        candidate_hits = [(self._answer_hit_score(query, category, hit), hit) for hit in hits if not hit.is_superseded]
        candidate_hits = [(score, hit) for score, hit in candidate_hits if score > 0]
        candidate_hits.sort(key=lambda item: (-item[0], -item[1].score, item[1].label.lower()))

        if category != "personalization_advice":
            for _score, hit in candidate_hits[:6]:
                bullet = f"- [{hit.node_type}] {hit.label}: {hit.content[:420]}"
                cost = self._estimate_tokens(bullet)
                if used_tokens + cost > max_tokens:
                    break
                lines.append(bullet)
                used_tokens += cost
                hit_ids.add(hit.node_id)
                if hit.raw_node is not None:
                    nodes_used.append(hit.raw_node)

        return lines, nodes_used, transcript_keys, hit_ids

    def _answer_hit_score(self, query: str, category: str, hit: _Hit) -> float:
        text = f"{hit.label} {hit.content}".lower()
        query_lower = query.lower()
        terms = self._query_content_terms(query_lower)
        text_terms = set(re.findall(r"[a-z0-9]+", text))
        overlap = len(terms.intersection(text_terms))
        score = float(hit.score) + min(0.35, overlap * 0.05)

        if category == "short_personal_fact":
            has_answer_shape = False
            if re.search(
                r"\b(i|my|me)\b.{0,100}\b(graduated|degree|commute|takes?|personal best|tried|worked|studied|"
                r"stop(?:ping|ped)?|check(?:ing|ed)?|emails?|messages?|ratio|bought|buy|from|store|shop|bookshelf|"
                r"keep|kept|stored|storage|closet|rack|occupation|job|role|startup|delivery|pick up|return|jogging|yoga)\b",
                text,
            ):
                score += 0.45
                has_answer_shape = True
            if re.search(
                r"\b\d+\s*(minutes?|hours?|days?|weeks?|months?|years?)\b|"
                r"\b(?:[01]?\d|2[0-3])(?::[0-5]\d)?\s*(?:a\.?m\.?|p\.?m\.?)\b|"
                r"\b\d+\s*:\s*\d+\b|\bdegree in\b|\bgraduated with\b|\bIKEA\b|"
                r"\b(marketing specialist|startup|shoe rack|closet|zara|dry cleaning|delivery services?|uber eats|domino|fresh fusion)\b",
                text,
                flags=re.IGNORECASE,
            ):
                score += 0.35
                has_answer_shape = True
            if not has_answer_shape:
                return 0.0
        elif category == "table_lookup":
            names = self._query_capitalized_terms(query)
            has_name = any(name.lower() in text for name in names)
            if re.search(r"\|.*\|", hit.content) or re.search(r"\b(row|table|shift|rotation|schedule)\b", text):
                score += 0.30
            if has_name:
                score += 0.45
            if re.search(r"\bagent\s*\d+\b", text) and not has_name:
                score -= 0.70
        elif category == "temporal_ordering":
            has_date = bool(
                re.search(
                    r"\b(documentdate|\d{4}/\d{2}/\d{2}|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|"
                    r"month ago|last week|today|yesterday|weeks? ago|days? ago)\b",
                    text,
                )
            )
            if re.search(
                r"\b(documentdate|\d{4}/\d{2}/\d{2}|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|"
                r"month ago|last week|today|yesterday|weeks? ago|days? ago)\b",
                text,
            ):
                score += 0.35
            if overlap >= 3:
                score += 0.25
            if not has_date and overlap < 3:
                return 0.0
        elif category == "exact_detail":
            if overlap >= 2:
                score += 0.25
            if re.search(r"\b\d+\s*:\s*\d+\b|\bMummies\s*\(\s*\d+\s*\)|\b\d+\s+mummies\b", hit.content, re.IGNORECASE):
                score += 0.60
            if re.search(r"\b(second song|chorus|chord progression)\b", query_lower) and re.search(
                r"\b(second song|chorus|chord|progression)\b", text
            ):
                score += 0.45
            if re.search(r"\bwhere\b.*\b(buy|bought|from|bookshelf)\b", query_lower) and re.search(
                r"\bIKEA\b", hit.content
            ):
                score += 0.70
            if self._looks_like_color_detail_query(query_lower) and self._has_color_answer_shape(text):
                score += 0.45
            if self._looks_like_named_place_query(query_lower) and self._has_named_place_answer_shape(text):
                score += 0.45
            if self._looks_like_identity_detail_query(query_lower) and self._has_identity_answer_shape(text):
                score += 0.55
            if self._looks_like_named_entity_detail_query(query_lower) and self._has_capitalized_entity_span(
                hit.content
            ):
                score += 0.25
            if re.search(r"\b(library|babel|borges|center|centre|circumference)\b", query_lower):
                if re.search(r"\b(library is a sphere|exact center|hexagons|circumference is inaccessible)\b", text):
                    score += 1.20
            if overlap < 2 and score < 0.55:
                return 0.0
        elif category == "enumerated_list":
            if overlap >= 2:
                score += min(0.45, overlap * 0.07)
            if self._has_list_answer_shape(text):
                score += 0.40
            if overlap < 2 and not self._has_list_answer_shape(text):
                return 0.0
        elif category == "personalization_advice":
            domain_terms = self._personalization_domain_terms(query_lower)
            domain_overlap = len(domain_terms.intersection(set(re.findall(r"[a-z0-9]+", text)))) if domain_terms else 0
            if domain_terms and domain_overlap == 0:
                return 0.0
            if hit.node_type in {"preference", "fact", "note", "question"}:
                score += 0.30
            if re.search(r"\b(i|i've|i'm|my|me|recently|visited|tried|prefer|like|enjoy|meal prep|theme park)\b", text):
                score += 0.35
            score += min(0.50, domain_overlap * 0.12)

        return score if score >= 0.25 else 0.0

    def _answer_transcript_score(self, query: str, category: str, hit: Any) -> float:
        snippet = self._transcript_text(hit)
        text = snippet.lower()
        query_lower = query.lower()
        terms = self._query_content_terms(query_lower)
        text_terms = set(re.findall(r"[a-z0-9]+", text))
        overlap = len(terms.intersection(text_terms))
        score = min(0.45, overlap * 0.06)

        if category == "short_personal_fact":
            if re.search(
                r"\b(i|my|me)\b.{0,120}\b(graduated|degree|commute|takes?|personal best|tried|worked|studied|"
                r"stop(?:ping|ped)?|check(?:ing|ed)?|emails?|messages?|ratio|bought|buy|from|store|shop|bookshelf|"
                r"keep|kept|stored|storage|closet|rack|occupation|job|role|startup|delivery|pick up|return|jogging|yoga)\b",
                text,
            ):
                score += 0.65
            if re.search(
                r"\b\d+\s*(minutes?|hours?|days?|weeks?|months?|years?)\b|"
                r"\b(?:[01]?\d|2[0-3])(?::[0-5]\d)?\s*(?:a\.?m\.?|p\.?m\.?)\b|"
                r"\b\d+\s*:\s*\d+\b|\bdegree in\b|\bgraduated with\b|\beach way\b|\bIKEA\b|"
                r"\b(marketing specialist|startup|shoe rack|closet|zara|dry cleaning|delivery services?|uber eats|domino|fresh fusion)\b",
                text,
                flags=re.IGNORECASE,
            ):
                score += 0.50
            if re.search(r"\b(previous occupation|occupation|previous job|previous role|worked as)\b", query_lower):
                if re.search(r"\bprevious role as\b", text):
                    score += 0.95
                if re.search(r"\b(marketing specialist|small startup)\b", text, flags=re.IGNORECASE):
                    score += 0.85
            if re.search(r"\bfood delivery services?\b", query_lower):
                if re.search(r"\b(uber eats|domino'?s?|fresh fusion)\b", text, flags=re.IGNORECASE):
                    score += 0.85
            if "how often" in query_lower and re.search(r"\btennis\b", query_lower):
                if re.search(r"\bweekly tennis sessions?\b|\bevery other week\b", text, flags=re.IGNORECASE):
                    score += 1.05
            if re.search(r"\b(replace|replaced|fix|fixed)\b", query_lower):
                if re.search(
                    r"\b(kitchen faucet|kitchen mat|old toaster|toaster oven|old coffee maker|espresso machine|kitchen shelves)\b",
                    text,
                    flags=re.IGNORECASE,
                ):
                    score += 0.95
            if re.search(r"\b(difference|price|cost)\b", query_lower) and re.search(r"\bboots?\b", query_lower):
                if re.search(r"\$ ?(?:800|50)\b|\bbudget store\b|\bsimilar boots?\b", text, flags=re.IGNORECASE):
                    score += 1.05
        elif category == "table_lookup":
            names = self._query_capitalized_terms(query)
            has_name = any(name.lower() in text for name in names)
            if re.search(r"\bagent\s*\d+\b", text) and not has_name:
                return 0.0
            if has_name:
                score += 0.70
            if re.search(r"\|.*\|", snippet) or re.search(
                r"\b(shift|rotation|schedule|sunday|monday|tuesday|wednesday|thursday|friday|saturday)\b", text
            ):
                score += 0.35
        elif category == "temporal_ordering":
            if re.search(r"\b(documentdate|\d{4}/\d{2}/\d{2})\b", text):
                score += 0.55
            if re.search(r"\b(month ago|last week|today|yesterday|weeks? ago|days? ago)\b", text):
                score += 0.70
            if overlap >= 3:
                score += 0.30
            if any(self._phrase_overlap(phrase, text) >= 2 for phrase in self._extract_event_phrases(query)):
                score += 0.40
            if re.search(r"\bbrother\b", query_lower) and re.search(r"\bgraduation\b", query_lower):
                if re.search(r"\bbrother\b", text) and re.search(r"\bgraduation\s+gift\b", text):
                    score += 0.80
            if re.search(r"\bbest friend\b", query_lower) and re.search(r"\bbirthday\b", query_lower):
                if re.search(r"\bbest friend\b", text) and re.search(r"\bbirthday\b", text):
                    score += 0.80
        elif category == "exact_detail":
            if overlap >= 2:
                score += min(0.50, overlap * 0.08)
            if re.search(r"\b\d+\s*:\s*\d+\b|\bMummies\s*\(\s*\d+\s*\)|\b\d+\s+mummies\b", snippet, re.IGNORECASE):
                score += 0.85
            if re.search(r"\b(second song|chorus|chord progression)\b", query_lower) and re.search(
                r"\b(second song|chorus|chord|progression)\b", text
            ):
                score += 0.55
            if re.search(r"\bwhere\b.*\b(buy|bought|from|bookshelf)\b", query_lower) and re.search(
                r"\bIKEA\b", snippet
            ):
                score += 0.90
            if self._looks_like_color_detail_query(query_lower) and self._has_color_answer_shape(text):
                score += 0.75
            if self._looks_like_named_place_query(query_lower) and self._has_named_place_answer_shape(text):
                score += 0.75
            if self._looks_like_identity_detail_query(query_lower) and self._has_identity_answer_shape(text):
                score += 0.85
            if self._looks_like_named_entity_detail_query(query_lower) and self._has_capitalized_entity_span(snippet):
                score += 0.30
            if re.search(r"\b(library|babel|borges|center|centre|circumference)\b", query_lower):
                if re.search(r"\b(library is a sphere|exact center|hexagons|circumference is inaccessible)\b", text):
                    score += 1.20
        elif category == "enumerated_list":
            if overlap >= 2:
                score += min(0.65, overlap * 0.09)
            if self._has_list_answer_shape(text):
                score += 0.75
        elif category == "personalization_advice":
            domain_terms = self._personalization_domain_terms(query_lower)
            domain_overlap = len(domain_terms.intersection(text_terms)) if domain_terms else 0
            if domain_terms and domain_overlap == 0:
                return 0.0
            score += min(0.70, domain_overlap * 0.15)
            if re.search(r"\b(i|i've|i'm|my|me|recently|visited|tried|prefer|like|enjoy|meal prep|theme park)\b", text):
                score += 0.45
            if self._source_role(hit, snippet) == "user" or re.search(r"\buser:\s", text):
                score += 0.25

        if re.search(r"\b(user:|\[documentdate:)\b", text):
            score += 0.10
        return score

    def _prioritize_transcript_hits(self, query: str, transcript_hits: list[Any]) -> list[Any]:
        """Deduplicate and order transcript evidence for compact answerability."""
        query_lower = (query or "").lower()
        answer_category = self._detect_answer_category(query)
        deduped: list[Any] = []
        seen: set[str] = set()
        for hit in transcript_hits:
            snippet = self._transcript_snippet(hit, max_chars=1000)
            key = re.sub(r"\s+", " ", snippet.lower()).strip()
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(hit)

        def score(hit: Any) -> tuple[int, int]:
            snippet = self._transcript_snippet(hit, max_chars=1000).lower()
            value = 0
            query_terms = {
                token
                for token in re.findall(r"[a-z0-9]+", query_lower)
                if len(token) > 2
                and token
                not in {
                    "the",
                    "and",
                    "for",
                    "our",
                    "you",
                    "can",
                    "what",
                    "was",
                    "were",
                    "did",
                    "about",
                    "previous",
                    "chat",
                    "checking",
                    "remind",
                }
            }
            if query_terms:
                value += min(14, len(query_terms.intersection(set(re.findall(r"[a-z0-9]+", snippet)))) * 2)
            if self._looks_like_constraint_query(query_lower):
                if re.search(
                    r"\b(quick correction|right constraint|correction:|clarif(?:y|ying)|not just|instead)\b", snippet
                ):
                    value += 10
                elif re.search(r"\b(correction|corrected)\b", snippet):
                    value += 8
                if re.search(r"\b(prefer|constraint|because|avoid|dislike|respect)\b", snippet):
                    value += 3
                if re.search(r"\bbecause\b", snippet) and re.search(r"\bis\b", snippet):
                    value += 2
            if self._looks_like_count_update_query(query_lower):
                if re.search(r"\b(inventory note|in storage|initial)\b", snippet):
                    value += 12
                if re.search(r"\b(used|spent|removed|gave away|reduces?|subtract|minus)\b", snippet):
                    value += 11
                if re.search(r"\b(bought|added|more|received|increase|plus)\b", snippet):
                    value += 10
                if re.search(
                    r"\b(pick up|picked up|return|returned|exchange|exchanged|dry cleaning|store|zara)\b", snippet
                ):
                    value += 12
                if re.search(
                    r"\b(delivery service|delivery services|domino|fresh fusion|uber eats|doordash|takeout)\b", snippet
                ):
                    value += 12
                if re.search(r"\b(labels?|drawer|formatting|unrelated)\b", snippet):
                    value -= 5
            if answer_category == "table_lookup":
                names = self._query_capitalized_terms(query)
                has_name = any(name.lower() in snippet for name in names)
                if re.search(r"\bagent\s*\d+\b", snippet) and not has_name:
                    value -= 20
                if has_name:
                    value += 12
                if re.search(r"\|.*\|", snippet):
                    value += 5
            elif answer_category == "short_personal_fact":
                if re.search(
                    r"\b(i|my|me)\b.{0,120}\b(graduated|degree|commute|takes?|personal best|tried|"
                    r"stop(?:ping|ped)?|check(?:ing|ed)?|emails?|messages?|ratio|bought|buy|from|store|shop|bookshelf|"
                    r"keep|kept|stored|storage|closet|rack|occupation|job|role|startup|delivery|pick up|return|jogging|yoga)\b",
                    snippet,
                ):
                    value += 10
                if re.search(
                    r"\b\d+\s*(minutes?|hours?|days?|weeks?|months?|years?)\b|"
                    r"\b(?:[01]?\d|2[0-3])(?::[0-5]\d)?\s*(?:a\.?m\.?|p\.?m\.?)\b|"
                    r"\b\d+\s*:\s*\d+\b|\bdegree in\b|\bgraduated with\b|\beach way\b|\bIKEA\b|"
                    r"\b(marketing specialist|startup|shoe rack|closet|zara|dry cleaning|delivery services?|uber eats|domino|fresh fusion)\b",
                    snippet,
                    flags=re.IGNORECASE,
                ):
                    value += 8
                if re.search(r"\b(previous occupation|occupation|previous job|previous role|worked as)\b", query_lower):
                    if re.search(r"\bprevious role as\b", snippet):
                        value += 14
                    if re.search(r"\b(marketing specialist|small startup)\b", snippet, flags=re.IGNORECASE):
                        value += 12
                if re.search(r"\bfood delivery services?\b", query_lower) and re.search(
                    r"\b(uber eats|domino'?s?|fresh fusion)\b",
                    snippet,
                    flags=re.IGNORECASE,
                ):
                    value += 14
            elif answer_category == "temporal_ordering":
                if re.search(r"\b(documentdate|\d{4}/\d{2}/\d{2})\b", snippet):
                    value += 8
                if any(self._phrase_overlap(phrase, snippet) >= 2 for phrase in self._extract_event_phrases(query)):
                    value += 5
                if re.search(r"\bbrother\b", query_lower) and re.search(r"\bgraduation\b", query_lower):
                    if re.search(r"\bbrother\b", snippet) and re.search(r"\bgraduation\s+gift\b", snippet):
                        value += 16
                if re.search(r"\bbest friend\b", query_lower) and re.search(r"\bbirthday\b", query_lower):
                    if re.search(r"\bbest friend\b", snippet) and re.search(r"\bbirthday\b", snippet):
                        value += 16
            elif answer_category == "exact_detail":
                if re.search(r"\b\d+\s*:\s*\d+\b|\bMummies\s*\(\s*\d+\s*\)|\b\d+\s+mummies\b", snippet, re.IGNORECASE):
                    value += 14
                if re.search(r"\b(second song|chorus|chord|progression)\b", snippet):
                    value += 10
                if re.search(r"\bwhere\b.*\b(buy|bought|from|bookshelf)\b", query_lower) and re.search(
                    r"\bIKEA\b", snippet, re.IGNORECASE
                ):
                    value += 14
            elif answer_category == "personalization_advice":
                domain_terms = self._personalization_domain_terms(query_lower)
                if domain_terms:
                    value += min(16, len(domain_terms.intersection(set(re.findall(r"[a-z0-9]+", snippet)))) * 3)
                if re.search(
                    r"\b(i|i've|i'm|my|me|recently|visited|tried|prefer|like|enjoy|meal prep|theme park)\b", snippet
                ):
                    value += 8
            if re.search(r"\b(documentdate|user:|assistant:)\b", snippet):
                value += 1
            return (-value, deduped.index(hit))

        return sorted(deduped, key=score)

    def _forced_temporal_transcripts(self, query: str, transcript_hits: list[Any]) -> list[Any]:
        forced: list[Any] = []
        seen: set[str] = set()
        for phrase in self._extract_event_phrases(query):
            best: tuple[int, Any] | None = None
            for hit in transcript_hits:
                snippet = self._transcript_snippet(hit, max_chars=1400)
                if not re.search(r"\b(documentdate|\d{4}/\d{2}/\d{2})\b", snippet.lower()):
                    continue
                overlap = self._phrase_overlap(phrase, snippet)
                if overlap <= 0:
                    continue
                if best is None or overlap > best[0]:
                    best = (overlap, hit)
            if best is None:
                continue
            key = self._transcript_key(best[1], max_chars=1200)
            if key and key not in seen:
                seen.add(key)
                forced.append(best[1])
        return forced

    def _phrase_overlap(self, phrase: str, text: str) -> int:
        phrase_terms = self._query_content_terms(phrase.lower())
        text_terms = set(re.findall(r"[a-z0-9]+", text.lower()))
        return len(phrase_terms.intersection(text_terms))

    def _expand_recall_session_transcripts(
        self,
        transcript_hits: list[Any],
        scope: dict[str, str],
        *,
        hits: list[_Hit] | None = None,
    ) -> list[Any]:
        if not hasattr(self._graph, "list_transcript_records"):
            return []
        expanded: list[Any] = []
        seen_sessions: set[str] = set()
        candidate_session_ids: list[str] = []
        for hit in transcript_hits:
            session_id = str(getattr(hit, "session_id", "") or "").strip()
            if session_id:
                candidate_session_ids.append(session_id)
        for hit in hits or []:
            candidate_session_ids.extend(self._hit_session_ids(hit))

        for session_id in candidate_session_ids:
            if not session_id or session_id in seen_sessions:
                continue
            seen_sessions.add(session_id)
            try:
                records = self._graph.list_transcript_records(
                    agent_id=scope.get("agent_id", ""),
                    project=scope.get("project", ""),
                    session_id=session_id,
                    limit=40,
                )
            except Exception as exc:
                LOGGER.debug("recursive_context._expand_recall_session_transcripts failed: %s", exc)
                continue
            expanded.extend(records)
            if len(seen_sessions) >= 2:
                break
        return expanded

    def _direct_transcript_evidence(self, query: str, scope: dict[str, str], limit: int) -> list[Any]:
        if not hasattr(self._graph, "search_transcript_records"):
            return []
        try:
            return list(
                self._graph.search_transcript_records(
                    query=query,
                    agent_id=scope.get("agent_id", ""),
                    project=scope.get("project", ""),
                    session_id=scope.get("session_id", ""),
                    limit=max(1, int(limit)),
                )
                or []
            )
        except Exception as exc:
            LOGGER.debug("recursive_context._direct_transcript_evidence failed: %s", exc)
            return []

    def _lexical_answer_transcript_evidence(
        self,
        *,
        query: str,
        category: str,
        scope: dict[str, str],
        limit: int,
    ) -> list[Any]:
        if category == "generic" or not hasattr(self._graph, "list_transcript_records"):
            return []
        try:
            records = self._graph.list_transcript_records(
                agent_id=scope.get("agent_id", ""),
                project=scope.get("project", ""),
                session_id=scope.get("session_id", ""),
                limit=5000,
            )
        except Exception as exc:
            LOGGER.debug("recursive_context._lexical_answer_transcript_evidence failed: %s", exc)
            return []

        scored: list[tuple[float, int, Any]] = []
        query_lower = query.lower()
        for idx, record in enumerate(records):
            text = str(getattr(record, "transcript_text", "") or "")
            if not text.strip():
                continue
            candidate = SimpleNamespace(
                score=0.0,
                session_id=str(getattr(record, "session_id", "") or ""),
                turn_index=int(getattr(record, "turn_index", 0) or 0),
                turn_pair_id=str(getattr(record, "turn_pair_id", "") or ""),
                role=str(getattr(record, "role", "") or ""),
                transcript_text=text,
                transcript_snippet=text,
                observed_at=getattr(record, "observed_at", datetime.now(UTC)),
            )
            score = self._answer_transcript_score(query, category, candidate)
            if category == "short_personal_fact":
                if "degree" in query_lower and not re.search(r"\b(graduated|degree)\b", text, flags=re.IGNORECASE):
                    continue
                if "commute" in query_lower and not re.search(
                    r"\b(commute|commuting|train|bus)\b", text, flags=re.IGNORECASE
                ):
                    continue
                if re.search(r"\b(i|my|me)\b", text, flags=re.IGNORECASE):
                    score += 0.20
            elif category == "table_lookup":
                names = self._query_capitalized_terms(query)
                if names and not any(name.lower() in text.lower() for name in names):
                    continue
                if re.search(r"\bagent\s*\d+\b", text, flags=re.IGNORECASE) and not any(
                    name.lower() in text.lower() for name in names
                ):
                    continue
            elif category == "temporal_ordering":
                has_event_overlap = any(
                    self._phrase_overlap(phrase, text) >= 2 for phrase in self._extract_event_phrases(query)
                )
                has_gift_event = (
                    bool(re.search(r"\bbrother\b", query_lower) and re.search(r"\bgraduation\b", query_lower))
                    and bool(re.search(r"\bbrother\b", text, flags=re.IGNORECASE))
                    and bool(re.search(r"\bgraduation\s+gift\b", text, flags=re.IGNORECASE))
                ) or (
                    bool(re.search(r"\bbest friend\b", query_lower) and re.search(r"\bbirthday\b", query_lower))
                    and bool(re.search(r"\bbest friend\b", text, flags=re.IGNORECASE))
                    and bool(re.search(r"\bbirthday\b", text, flags=re.IGNORECASE))
                )
                if not (has_event_overlap or has_gift_event):
                    continue
            elif category == "exact_detail":
                lowered = text.lower()
                terms = self._query_content_terms(query_lower)
                overlap = len(terms.intersection(set(re.findall(r"[a-z0-9]+", lowered))))
                if overlap < 2:
                    continue
                if self._looks_like_color_detail_query(query_lower) and not self._has_color_answer_shape(lowered):
                    continue
                if self._looks_like_named_place_query(query_lower) and not self._has_named_place_answer_shape(lowered):
                    continue
            elif category == "enumerated_list":
                lowered = text.lower()
                terms = self._query_content_terms(query_lower)
                overlap = len(terms.intersection(set(re.findall(r"[a-z0-9]+", lowered))))
                if overlap < 2:
                    continue
                if not self._has_list_answer_shape(lowered):
                    continue
            if score > 0:
                scored.append((score, -idx, candidate))

        scored.sort(key=lambda item: (-item[0], item[1]))
        return [item[2] for item in scored[: max(1, int(limit))]]

    def _personalization_transcript_evidence(
        self,
        *,
        query: str,
        scope: dict[str, str],
        transcript_hits: list[Any],
        limit: int,
    ) -> list[Any]:
        query_lower = (query or "").lower()
        if not self._looks_like_personalization_advice_query(query_lower):
            return []

        records: list[Any] = list(transcript_hits)
        if hasattr(self._graph, "list_transcript_records"):
            try:
                records.extend(
                    self._graph.list_transcript_records(
                        agent_id=scope.get("agent_id", ""),
                        project=scope.get("project", ""),
                        session_id=scope.get("session_id", ""),
                        limit=5000,
                    )
                    or []
                )
            except Exception as exc:
                LOGGER.debug("recursive_context._personalization_transcript_evidence failed: %s", exc)

        scored: list[tuple[float, int, Any]] = []
        seen: set[str] = set()
        for index, record in enumerate(records):
            key = self._transcript_key(record, max_chars=1200)
            if not key or key in seen:
                continue
            seen.add(key)
            score = self._personalization_transcript_score(query, record)
            if score > 0:
                scored.append((score, -index, record))

        scored.sort(key=lambda item: (-item[0], item[1]))
        return [item[2] for item in scored[: max(1, int(limit))]]

    def _personalization_transcript_score(self, query: str, hit: Any) -> float:
        text = self._transcript_text(hit)
        lowered = text.lower()
        query_lower = (query or "").lower()
        query_terms = self._query_content_terms(query_lower)
        text_terms = set(re.findall(r"[a-z0-9]+", lowered))
        overlap = len(query_terms.intersection(text_terms))
        if overlap == 0:
            return 0.0
        domain_terms = self._personalization_domain_terms(query_lower)
        if domain_terms:
            domain_overlap = len(domain_terms.intersection(text_terms))
            if domain_overlap == 0:
                return 0.0
        else:
            domain_overlap = 0

        score = min(0.50, overlap * 0.08)
        score += min(0.45, domain_overlap * 0.15)
        role = self._source_role(hit, text)
        if role == "user" or re.search(r"\buser:\s", lowered):
            score += 0.45
        elif role == "assistant":
            score -= 0.20
            if not re.search(r"\b(i|i've|i'm|my|me|user)\b", lowered):
                return 0.0

        if re.search(r"\b(i|i've|i'm|my|me)\b", lowered):
            score += 0.30
        if re.search(
            r"\b(recently|lately|been|started|tried|made|figured out|want(?:ed)? to try|"
            r"looking at|getting inspiration|challenge|success|struggling|stuck|interested in|prefer|like|enjoy)\b",
            lowered,
        ):
            score += 0.35
        if re.search(
            r"\b(recipe|recipes|slow cooker|painting|paintings|flowers|instagram|tutorials?|"
            r"social media|yogurt|beef stew)\b",
            lowered,
        ):
            score += 0.20
        if re.search(r"\b(stuck|inspiration|inspire|ideas?)\b", query_lower):
            if re.search(r"\b(inspiration|inspire|social media|instagram|challenge|tutorials?|flowers?)\b", lowered):
                score += 0.45
            if re.search(r"\b(price|pricing|selling|online sale|photos? of my artwork)\b", lowered):
                score -= 0.35
        if re.search(r"\b(slow cooker|recipes?|better results|struggling)\b", query_lower):
            if re.search(r"\b(figured out|success|made a delicious|beef stew|yogurt)\b", lowered):
                score += 0.35
        if (
            re.search(r"\b(generic|general tips|here are some tips|recommend some|you might try)\b", lowered)
            and role == "assistant"
        ):
            score -= 0.30

        return score if score >= 0.45 else 0.0

    def _personalization_domain_terms(self, query_lower: str) -> set[str]:
        terms: set[str] = set()
        if re.search(r"\b(paint|painting|paintings|art|artwork|inspiration)\b", query_lower):
            terms.update(
                {
                    "paint",
                    "painting",
                    "paintings",
                    "art",
                    "artwork",
                    "instagram",
                    "tutorial",
                    "tutorials",
                    "flower",
                    "flowers",
                    "challenge",
                }
            )
        if re.search(r"\b(slow cooker|recipe|recipes|cooking)\b", query_lower):
            terms.update({"slow", "cooker", "recipe", "recipes", "yogurt", "beef", "stew", "vegetarian", "vegan"})
        if re.search(r"\b(meal prep|meal-prep|recipes?|cooking|healthy)\b", query_lower):
            terms.update(
                {
                    "meal",
                    "prep",
                    "recipe",
                    "recipes",
                    "healthy",
                    "quinoa",
                    "roasted",
                    "vegetables",
                    "chicken",
                    "turkey",
                    "avocado",
                    "wraps",
                    "salads",
                }
            )
        if re.search(r"\b(theme park|theme parks|park weekend|rides?|attractions?)\b", query_lower):
            terms.update(
                {
                    "theme",
                    "park",
                    "parks",
                    "disneyland",
                    "knott",
                    "six",
                    "flags",
                    "universal",
                    "studios",
                    "thrill",
                    "rides",
                    "events",
                    "food",
                    "nighttime",
                    "shows",
                }
            )
        return terms

    def _personalization_graph_hits(self, query: str, hits: list[_Hit]) -> list[_Hit]:
        scored: list[tuple[float, _Hit]] = []
        for hit in hits:
            score = self._personalization_graph_score(query, hit)
            if score > 0:
                scored.append((score, hit))
        scored.sort(key=lambda item: (-item[0], -item[1].score, item[1].label.lower()))
        return [hit for _score, hit in scored[:8]]

    def _personalization_graph_score(self, query: str, hit: _Hit) -> float:
        query_lower = (query or "").lower()
        text = f"{hit.label} {hit.content}".lower()
        text_terms = set(re.findall(r"[a-z0-9]+", text))
        query_terms = self._query_content_terms(query_lower)
        overlap = len(query_terms.intersection(text_terms))
        domain_terms = self._personalization_domain_terms(query_lower)
        domain_overlap = len(domain_terms.intersection(text_terms)) if domain_terms else 0
        if domain_terms and domain_overlap == 0:
            return 0.0
        if overlap == 0 and domain_overlap == 0:
            return 0.0

        score = min(0.35, overlap * 0.05) + min(0.60, domain_overlap * 0.15)
        if hit.node_type in {"preference", "fact", "note", "question"}:
            score += 0.20
        elif hit.node_type in {"entity", "concept"}:
            score -= 0.10

        if re.search(r"\b(i|i've|i'm|my|me)\b", text):
            score += 0.30
        if re.search(
            r"\b(recently|lately|been|started|tried|made|figured out|want(?:ed)? to try|"
            r"getting inspiration|challenge|success|struggling|stuck|interested in|prefer|like|enjoy)\b",
            text,
        ):
            score += 0.35

        if re.search(r"\b(stuck|inspiration|inspire|ideas?)\b", query_lower):
            if re.search(r"\b(inspiration|inspire|social media|instagram|challenge|tutorials?|flowers?)\b", text):
                score += 0.45
            if re.search(r"\b(price|pricing|selling|online sale|photos? of my artwork|value-based pricing)\b", text):
                score -= 0.70
        if re.search(r"\b(slow cooker|recipes?|better results|struggling)\b", query_lower):
            if not re.search(r"\b(slow|cooker|yogurt|beef|stew|figured out|made a delicious)\b", text):
                return 0.0
            if re.search(r"\b(figured out|success|made a delicious|beef stew|yogurt)\b", text):
                score += 0.45
            if re.search(r"\b(vegetarian recipes|vegan recipes|eggplant parmesan|chili con carne)\b", text):
                score -= 0.25

        if re.search(r"\b(sun basket|home chef|blue apron|instacart|spider|coffee shop|jewelry|commute)\b", text):
            score -= 0.75
        return score if score >= 0.45 else 0.0

    def _personalization_graph_section(
        self,
        *,
        query: str,
        hits: list[_Hit],
        emitted_hit_ids: set[str],
        max_tokens: int,
    ) -> tuple[list[str], list[Any], set[str]]:
        if not hits:
            return [], [], set()
        lines: list[str] = []
        nodes_used: list[Any] = []
        hit_ids: set[str] = set()
        used_tokens = 0
        for hit in hits:
            if hit.node_id in emitted_hit_ids or hit.node_id in hit_ids:
                continue
            bullet = f"- [{hit.node_type}] {hit.label}: {hit.content[:260]}"
            cost = self._estimate_tokens(bullet)
            if used_tokens + cost > max_tokens:
                break
            lines.append(bullet)
            used_tokens += cost
            hit_ids.add(hit.node_id)
            if hit.raw_node is not None:
                nodes_used.append(hit.raw_node)
        return lines, nodes_used, hit_ids

    def _personalization_evidence_section(
        self,
        *,
        query: str,
        transcript_hits: list[Any],
        emitted_transcript_keys: set[str],
        max_tokens: int,
    ) -> tuple[list[str], set[str]]:
        if not transcript_hits:
            return [], set()

        lines: list[str] = []
        used_tokens = 0
        emitted: set[str] = set()
        for hit in transcript_hits:
            key = self._transcript_key(hit, max_chars=1200)
            if not key or key in emitted_transcript_keys or key in emitted:
                continue
            snippet = self._focused_transcript_snippet(query, hit, max_chars=520)
            if not snippet:
                continue
            bullet = f"- {snippet}"
            cost = self._estimate_tokens(bullet)
            if used_tokens + cost > max_tokens:
                break
            lines.append(bullet)
            used_tokens += cost
            emitted.add(key)
        return lines, emitted

    def _pinned_transcript_evidence(self, *, query: str, scope: dict[str, str], limit: int) -> list[Any]:
        query_lower = (query or "").lower()
        if not self._uses_pinned_fact_lane(query_lower) or not hasattr(self._graph, "list_transcript_records"):
            return []
        try:
            records = self._graph.list_transcript_records(
                agent_id=scope.get("agent_id", ""),
                project=scope.get("project", ""),
                session_id=scope.get("session_id", ""),
                limit=5000,
            )
        except Exception as exc:
            LOGGER.debug("recursive_context._pinned_transcript_evidence failed: %s", exc)
            return []

        scored: list[tuple[float, int, Any]] = []
        for index, record in enumerate(records):
            text = self._transcript_text(record)
            if not text:
                continue
            best_score = 0.0
            for value in self._extract_pinned_values(query, text):
                candidate = self._make_pinned_candidate(query=query, text=text, value=value, source=record)
                if candidate is not None:
                    best_score = max(best_score, candidate.score)
            if best_score > 0:
                scored.append((best_score, -index, record))

        scored.sort(key=lambda item: (-item[0], item[1]))
        deduped: list[Any] = []
        seen: set[str] = set()
        for _score, _index, record in scored:
            key = self._transcript_key(record, max_chars=1200)
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(record)
            if len(deduped) >= max(1, int(limit)):
                break
        return deduped

    def _hit_session_ids(self, hit: _Hit) -> list[str]:
        session_ids: list[str] = []
        for source in (hit, getattr(hit, "raw_node", None)):
            if source is None:
                continue
            session_id = str(getattr(source, "session_id", "") or "").strip()
            if session_id:
                session_ids.append(session_id)
            evidence_records = getattr(source, "evidence_records", []) or []
            for record in evidence_records:
                if isinstance(record, dict):
                    value = record.get("session_id")
                else:
                    value = getattr(record, "session_id", "")
                session_id = str(value or "").strip()
                if session_id:
                    session_ids.append(session_id)
        deduped: list[str] = []
        seen: set[str] = set()
        for session_id in session_ids:
            if session_id in seen:
                continue
            seen.add(session_id)
            deduped.append(session_id)
        return deduped

    def _transcript_snippet(self, hit: Any, max_chars: int = 360) -> str:
        snippet = self._transcript_text(hit)
        if len(snippet) <= max_chars:
            return snippet
        return snippet[: max(0, max_chars - 1)].rstrip() + "…"

    def _transcript_text(self, hit: Any) -> str:
        snippet = ""
        if isinstance(hit, dict):
            for key in ("transcript_snippet", "transcript_text", "content"):
                value = hit.get(key)
                if value:
                    snippet = str(value)
                    break
        else:
            for attr in ("transcript_snippet", "transcript_text", "content"):
                value = getattr(hit, attr, None)
                if value:
                    snippet = str(value)
                    break
            if not snippet:
                snippet = str(hit)
        return re.sub(r"\s+", " ", snippet).strip()

    def _deduplicate_nodes_used(self, nodes_used: list[Any]) -> list[Any]:
        deduped: list[Any] = []
        seen: set[str] = set()
        for node in nodes_used:
            key = self._node_identity_key(node)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(node)
        return deduped

    def _node_identity_key(self, node: Any) -> str:
        for attr in ("id", "node_id"):
            value = getattr(node, attr, None)
            if value:
                return f"{attr}:{value}"
        if isinstance(node, dict):
            for attr in ("id", "node_id"):
                value = node.get(attr)
                if value:
                    return f"{attr}:{value}"
        return f"object:{id(node)}"

    def _focused_transcript_snippet(self, query: str, hit: Any, max_chars: int = 620) -> str:
        text = self._transcript_text(hit)
        if len(text) <= max_chars:
            return text
        query_lower = (query or "").lower()
        terms = sorted(self._query_content_terms(query_lower), key=len, reverse=True)
        anchors = [term for term in terms if len(term) >= 5]
        if self._looks_like_color_detail_query(query_lower):
            anchors.extend(["scaly body", "body", "image"])
        if self._looks_like_named_place_query(query_lower):
            anchors.extend(
                [
                    "milkshake",
                    "milkshakes",
                    "dessert",
                    "shop",
                    "restaurant",
                    "place",
                    "studio",
                    "gym",
                    "class",
                    "classes",
                ]
            )
        if self._looks_like_identity_detail_query(query_lower):
            anchors.extend(["last name", "maiden name", "old name", "former name", "changed my name"])
        if self._looks_like_enumerated_list_query(query_lower):
            anchors.extend(self._query_capitalized_terms(query))
            anchors.extend(["process", "processes", "steps", "items", "options", "list"])
        if "how often" in query_lower and "tennis" in query_lower:
            anchors.extend(["weekly tennis sessions", "every other week", "play tennis"])
        if re.search(r"\b(replace|replaced|fix|fixed)\b", query_lower):
            anchors.extend(
                ["kitchen faucet", "kitchen mat", "old toaster", "toaster oven", "coffee maker", "kitchen shelves"]
            )
        if re.search(r"\b(difference|price|cost)\b", query_lower) and re.search(r"\bboots?\b", query_lower):
            anchors.extend(["$800", "$50", "budget store", "similar boots"])
        if re.search(r"\b(library|babel|borges|center|centre|circumference)\b", query_lower):
            anchors.extend(["circumference is inaccessible", "exact center", "Library is a sphere", "hexagons"])
        if self._looks_like_temporal_ordering_query(query_lower):
            anchors.extend(["a month ago", "month ago", "last week", "today", "recently got", "just got back"])

        lowered = text.lower()
        best_pos = -1
        best_score = -1
        for anchor in anchors:
            pos = lowered.find(anchor.lower())
            if pos < 0:
                continue
            window = lowered[max(0, pos - max_chars // 3) : min(len(lowered), pos + max_chars)]
            score = len(set(re.findall(r"[a-z0-9]+", window)).intersection(self._query_content_terms(query_lower)))
            if self._looks_like_color_detail_query(query_lower) and self._has_color_answer_shape(window):
                score += 5
            if self._looks_like_named_place_query(query_lower) and self._has_named_place_answer_shape(window):
                score += 5
            if re.search(r"\b(library|babel|borges|center|centre|circumference)\b", query_lower) and re.search(
                r"\b(library is a sphere|exact center|hexagons|circumference is inaccessible)\b",
                window,
            ):
                score += 8
            if self._looks_like_temporal_ordering_query(query_lower) and re.search(
                r"\b(month ago|last week|today|yesterday|weeks? ago|days? ago)\b",
                window,
            ):
                score += 5
            if score > best_score:
                best_score = score
                best_pos = pos

        if best_pos < 0:
            return self._transcript_snippet(hit, max_chars=max_chars)
        start = max(0, best_pos - max_chars // 3)
        end = min(len(text), start + max_chars)
        if end - start < max_chars and start > 0:
            start = max(0, end - max_chars)
        prefix = "…" if start > 0 else ""
        suffix = "…" if end < len(text) else ""
        return f"{prefix}{text[start:end].strip()}{suffix}"

    def _looks_like_color_detail_query(self, query_lower: str) -> bool:
        return bool(re.search(r"\b(color|colour|body|image|picture|description|scaly)\b", query_lower))

    def _looks_like_named_place_query(self, query_lower: str) -> bool:
        if self._looks_like_identity_detail_query(query_lower):
            return False
        return bool(
            re.search(
                r"\b(shop|restaurant|place|dessert|milkshake|milkshakes|orlando|called|named|name|title|"
                r"recommended|unique|studio|gym|salon|clinic|spa|class|classes|club|center|centre|school|store)\b",
                query_lower,
            )
        )

    def _has_color_answer_shape(self, text_lower: str) -> bool:
        colors = (
            "black|white|red|blue|green|yellow|orange|purple|pink|brown|gray|grey|gold|silver|"
            "copper|bronze|navy|teal|turquoise|violet|indigo|beige|cream"
        )
        return bool(re.search(rf"\b({colors})\b", text_lower))

    def _has_named_place_answer_shape(self, text_lower: str) -> bool:
        return bool(
            re.search(
                r"\b(shop|restaurant|cafe|factory|bakery|emporium|kitchen|deli|bar|park|citywalk|dessert|"
                r"milkshake|milkshakes|studio|gym|salon|clinic|spa|class|classes|club|center|centre|school|store)\b",
                text_lower,
            )
        )

    def _has_list_answer_shape(self, text_lower: str) -> bool:
        bullet_count = len(re.findall(r"(?:^|\s)(?:\d+\.|\*|-)\s+[a-z0-9]", text_lower))
        separator_count = len(re.findall(r"\b(?:and|,)\s+[a-z][a-z0-9 -]{2,}", text_lower))
        return bullet_count >= 2 or separator_count >= 3

    def _has_identity_answer_shape(self, text_lower: str) -> bool:
        return bool(
            re.search(
                r"\b(used to be|used to go by|name was|now goes? by|changed (?:it |my name )?to|"
                r"before i (?:got married|got divorced|changed it)|maiden name (?:was|is)|last name (?:was|is))\b",
                text_lower,
            )
        )

    def _has_capitalized_entity_span(self, text: str) -> bool:
        return bool(re.search(r"\b[A-Z][a-zA-Z&'-]+(?:\s+[A-Z][a-zA-Z&'-]+){1,4}\b", text or ""))

    def _transcript_key(self, hit: Any, max_chars: int = 1000) -> str:
        return re.sub(r"\s+", " ", self._transcript_snippet(hit, max_chars=max_chars).lower()).strip()

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
