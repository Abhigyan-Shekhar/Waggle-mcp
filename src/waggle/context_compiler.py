from __future__ import annotations

from dataclasses import dataclass
import re

from waggle.retrieval.assembler import AssembledEvidence
from waggle.retrieval.contracts import EvidenceType, EvidenceValidator


@dataclass(frozen=True, slots=True)
class CompiledEvidenceContext:
    text: str
    estimated_tokens: int
    included_evidence_ids: tuple[str, ...]
    missing_slots: tuple[str, ...]
    validation_issues: tuple[str, ...] = ()


class CompactEvidenceCompiler:
    def compile(self, assembled: AssembledEvidence, *, max_tokens: int = 1200) -> CompiledEvidenceContext:
        issues = assembled.validation_issues or EvidenceValidator().validate(assembled)
        issue_codes = tuple(issue.code for issue in issues)
        lines = [
            f"QUESTION TYPE: {assembled.plan.query_type.value.upper()}",
            f"QUESTION: {assembled.plan.query}",
        ]
        included: list[str] = []
        included_set: set[str] = set()
        if assembled.calculation is not None:
            lines.extend(("", "VERIFIED COMPUTATION", assembled.calculation.expression))
        for slot in assembled.plan.slots:
            evidence = assembled.per_slot.get(slot.name, [])
            if not evidence:
                continue
            lines.extend(("", f"{slot.name.replace('_', ' ').upper()}"))
            for item in evidence:
                if item.evidence_id in included_set:
                    continue
                candidate = self._render_item(item)
                if self._estimate_tokens("\n".join([*lines, candidate])) > max_tokens:
                    break
                lines.append(candidate)
                included.append(item.evidence_id)
                included_set.add(item.evidence_id)
        if assembled.missing_slots:
            lines.extend(("", "MISSING REQUIRED EVIDENCE", ", ".join(assembled.missing_slots)))
        if issues:
            lines.extend(("", "EVIDENCE CONTRACT WARNINGS"))
            lines.extend(f"- {issue.code}: {issue.message}" for issue in issues)
        text = "\n".join(lines).strip()
        return CompiledEvidenceContext(
            text=text,
            estimated_tokens=self._estimate_tokens(text),
            included_evidence_ids=tuple(included),
            missing_slots=tuple(assembled.missing_slots),
            validation_issues=issue_codes,
        )

    @staticmethod
    def _render_item(item: object) -> str:
        evidence_id = str(getattr(item, "evidence_id"))
        evidence_type = getattr(item, "evidence_type")
        content = str(getattr(item, "content"))
        structure = dict(getattr(item, "structure", {}) or {})
        if evidence_type == EvidenceType.LIST_ITEM:
            title = f"LIST: {structure['list_name']} | " if structure.get("list_name") else ""
            neighbours = structure.get("neighbours", [])
            neighbour_text = ""
            if neighbours:
                neighbour_text = " | ADJACENT: " + "; ".join(
                    f"{entry['list_index']}. {entry['value']}" for entry in neighbours
                )
            rendered = f"{title}ITEM {structure['list_index']}: {structure['value']}{neighbour_text}"
        elif evidence_type == EvidenceType.TABLE_CELL:
            rendered = (
                f"ROW: {structure['row_key']} | COLUMN: {structure['column_name']} | "
                f"VALUE: {structure['cell_value']}"
            )
        elif evidence_type == EvidenceType.ASSISTANT_ANSWER:
            rendered = f"ASSISTANT ANSWER: {content}"
        elif evidence_type == EvidenceType.PREFERENCE:
            grounding = str(structure.get("grounding", "history")).upper()
            scope = str(structure.get("scope", "general"))
            rendered = f"{grounding} PREFERENCE [{scope}]: {content}"
        elif evidence_type == EvidenceType.STATE_SET:
            rendered = f"{str(structure.get('status', 'state')).upper()} MEMBER: {structure.get('member', content)}"
        else:
            rendered = content
        return f"[{evidence_id}] {rendered}"

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        tokens = re.findall(r"\w+|[^\w\s]", text or "", flags=re.UNICODE)
        return max(1, int(len(tokens) * 1.25))
