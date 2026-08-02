from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from waggle.context_compiler import CompactEvidenceCompiler, CompiledEvidenceContext
from waggle.retrieval.assembler import AssembledEvidence, EvidenceAssembler
from waggle.retrieval.contracts import EvidenceValidator, ValidationIssue
from waggle.retrieval.planner import DeterministicQueryPlanner, QueryPlan


@dataclass(frozen=True, slots=True)
class TemporalSlotResult:
    plan: QueryPlan
    assembled: AssembledEvidence
    context: CompiledEvidenceContext
    retrieval_trace: dict[str, list[dict[str, Any]]]
    fallback_used: bool = False
    validation_issues: tuple[str, ...] = ()


class TemporalSlotRetriever:
    def __init__(self, graph: Any) -> None:
        self.graph = graph
        self.planner = DeterministicQueryPlanner()
        self.assembler = EvidenceAssembler()
        self.compiler = CompactEvidenceCompiler()
        self.validator = EvidenceValidator()

    def retrieve(
        self,
        *,
        query: str,
        project: str = "",
        agent_id: str = "",
        session_id: str = "",
        max_context_tokens: int = 1200,
        reference_date: str = "",
    ) -> TemporalSlotResult:
        plan = self.planner.plan(query, reference_date=reference_date)
        hits_by_slot: dict[str, list[Any]] = {}
        trace: dict[str, list[dict[str, Any]]] = {}
        retriever = self.graph.hybrid_retriever()
        for slot in plan.slots:
            payload = retriever.retrieve_debug(
                query=slot.query,
                project=project,
                agent_id=agent_id,
                session_id=session_id,
                top_k=20,
            )
            hits = payload["hits"]
            hits_by_slot[slot.name] = hits
            trace[slot.name] = [
                {
                    "score": hit.score,
                    "source": hit.source,
                    "node_ids": list(hit.node_ids),
                    "turn_pair_id": hit.turn_pair_id,
                    "content_preview": " ".join(str(hit.content).split())[:500],
                    "layer_scores": dict(getattr(hit, "layer_scores", {}) or {}),
                    "score_explanation": dict(getattr(hit, "score_explanation", {}) or {}),
                }
                for hit in hits
            ]
        assembled = self.assembler.select(plan=plan, hits_by_slot=hits_by_slot)
        issues = self.validator.validate(assembled)
        fallback_used = False
        if issues:
            fallback_slots = self._fallback_slots(plan, issues)
            for slot in fallback_slots:
                fallback_query = " ".join((slot.query, *slot.fallback_queries, "exact complete structure"))
                payload = retriever.retrieve_debug(
                    query=fallback_query,
                    project=project,
                    agent_id=agent_id,
                    session_id=session_id,
                    top_k=40,
                )
                expanded_hits = list(payload["hits"])
                hits_by_slot[slot.name] = self._merge_hits(hits_by_slot.get(slot.name, []), expanded_hits)
                trace[f"{slot.name}__fallback"] = self._trace_hits(expanded_hits)
                fallback_used = True
            assembled = self.assembler.select(plan=plan, hits_by_slot=hits_by_slot)
            assembled.expanded_slots.update(slot.name for slot in fallback_slots)
            issues = self.validator.validate(assembled)
        assembled.validation_issues = issues
        context = self.compiler.compile(assembled, max_tokens=max_context_tokens)
        return TemporalSlotResult(
            plan=plan,
            assembled=assembled,
            context=context,
            retrieval_trace=trace,
            fallback_used=fallback_used,
            validation_issues=tuple(issue.code for issue in issues),
        )

    @staticmethod
    def _fallback_slots(plan: QueryPlan, issues: list[ValidationIssue]) -> list[Any]:
        names = {issue.slot for issue in issues if issue.slot != "operation"}
        if any(issue.slot == "operation" for issue in issues):
            names.update(slot.name for slot in plan.slots if slot.required)
        return [slot for slot in plan.slots if slot.name in names]

    @staticmethod
    def _hit_key(hit: Any) -> tuple[str, tuple[str, ...], str]:
        return (
            str(getattr(hit, "turn_pair_id", "") or ""),
            tuple(getattr(hit, "node_ids", []) or []),
            " ".join(str(getattr(hit, "content", "")).lower().split()),
        )

    @classmethod
    def _merge_hits(cls, original: list[Any], expanded: list[Any]) -> list[Any]:
        merged: list[Any] = []
        seen: set[tuple[str, tuple[str, ...], str]] = set()
        for hit in [*original, *expanded]:
            key = cls._hit_key(hit)
            if key not in seen:
                seen.add(key)
                merged.append(hit)
        return merged

    @staticmethod
    def _trace_hits(hits: list[Any]) -> list[dict[str, Any]]:
        return [
            {
                "score": hit.score,
                "source": hit.source,
                "node_ids": list(hit.node_ids),
                "turn_pair_id": hit.turn_pair_id,
                "content_preview": " ".join(str(hit.content).split())[:500],
                "layer_scores": dict(getattr(hit, "layer_scores", {}) or {}),
                "score_explanation": dict(getattr(hit, "score_explanation", {}) or {}),
            }
            for hit in hits
        ]
