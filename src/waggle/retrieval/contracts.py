from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from waggle.retrieval.assembler import AssembledEvidence


class EvidenceType(StrEnum):
    FACT = "fact"
    LIST_ITEM = "list_item"
    TABLE_CELL = "table_cell"
    TABLE_ROW = "table_row"
    ASSISTANT_ANSWER = "assistant_answer"
    EVENT = "event"
    PREFERENCE = "preference"
    STATE = "state"
    STATE_SET = "state_set"
    DATE = "date"


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    code: str
    slot: str
    message: str


class EvidenceValidator:
    """Validate evidence contracts before compact context reaches a reader."""

    def validate(self, assembled: AssembledEvidence) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        for slot in assembled.plan.slots:
            evidence = assembled.per_slot.get(slot.name, [])
            if slot.required and len(evidence) < slot.min_items:
                issues.append(
                    ValidationIssue("missing_required_slot", slot.name, f"Required slot {slot.name} is incomplete.")
                )
                continue
            if slot.required_role and evidence and not any(
                item.source_role == slot.required_role for item in evidence
            ):
                issues.append(
                    ValidationIssue(
                        "missing_required_role",
                        slot.name,
                        f"Slot {slot.name} requires {slot.required_role} evidence.",
                    )
                )
            if slot.required_terms and evidence and not any(
                self._contains_required_terms(item.content, slot.required_terms) for item in evidence
            ):
                issues.append(
                    ValidationIssue(
                        "missing_entity_anchor",
                        slot.name,
                        f"Slot {slot.name} does not contain its required entity anchor.",
                    )
                )
            for item in evidence:
                if slot.evidence_type == EvidenceType.LIST_ITEM:
                    if not item.structure.get("list_index") or not item.structure.get("value"):
                        issues.append(
                            ValidationIssue("incomplete_list_item", slot.name, "Indexed list evidence lacks index or value.")
                        )
                elif slot.evidence_type == EvidenceType.TABLE_CELL:
                    if not all(item.structure.get(key) for key in ("row_key", "column_name", "cell_value")):
                        issues.append(
                            ValidationIssue("incomplete_table_cell", slot.name, "Table evidence lacks row, column, or value.")
                        )
                elif slot.evidence_type == EvidenceType.PREFERENCE:
                    if item.structure.get("grounding") not in {"explicit", "history", "inferred"}:
                        issues.append(
                            ValidationIssue("unscoped_preference", slot.name, "Preference grounding is unknown.")
                        )
                    if not item.structure.get("scope"):
                        issues.append(
                            ValidationIssue("unscoped_preference", slot.name, "Preference scope is unknown.")
                        )
                    if not item.structure.get("scope_compatible", False):
                        issues.append(
                            ValidationIssue(
                                "incompatible_preference_scope",
                                slot.name,
                                "Preference evidence is not demonstrably compatible with the query scope.",
                            )
                        )

        if assembled.plan.query_type.value == "current_set" and "active_members" not in assembled.expanded_slots:
            issues.append(
                ValidationIssue(
                    "unverified_active_set_completeness",
                    "active_members",
                    "Current-set membership has not received its bounded enumeration pass.",
                )
            )

        calculated_operations = {"sum", "count", "difference", "percentage", "date_difference", "time_offset"}
        operation = assembled.plan.operation
        if operation is not None and operation.value in calculated_operations and assembled.calculation is None:
            issues.append(
                ValidationIssue(
                    "incomplete_operation",
                    "operation",
                    f"Operation {assembled.plan.operation.value} lacks complete unambiguous operands.",
                )
            )
        if assembled.plan.query_type.value == "current_state":
            current = assembled.per_slot.get("current_state", [])
            if len(current) > 1:
                issues.append(
                    ValidationIssue("ambiguous_current_state", "current_state", "Current-state evidence is not singular.")
                )
        return self._dedupe(issues)

    @staticmethod
    def _contains_required_terms(content: str, required_terms: tuple[str, ...]) -> bool:
        words = set(content.lower().replace("-", " ").split())
        normalized = {word.strip(".,:;!?()[]{}\"'") for word in words}
        return all(term in normalized or (term.endswith("s") and term[:-1] in normalized) for term in required_terms)

    @staticmethod
    def _dedupe(issues: list[ValidationIssue]) -> list[ValidationIssue]:
        seen: set[tuple[str, str]] = set()
        result: list[ValidationIssue] = []
        for issue in issues:
            key = (issue.code, issue.slot)
            if key not in seen:
                seen.add(key)
                result.append(issue)
        return result
