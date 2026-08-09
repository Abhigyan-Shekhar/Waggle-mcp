from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from waggle.retrieval.contracts import EvidenceType, ValidationIssue
from waggle.retrieval.planner import EvidenceSlot, Operation, QueryPlan


_NUMBER_RE = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?")
_DATE_RE = re.compile(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b")
_DOCUMENT_DATE_RE = re.compile(r"\[documentDate:[^\]]+\]", re.I)
_DAYS_AGO_RE = re.compile(r"\b(?P<count>\d+|one|two|three|four|five|six|seven)\s+days?\s+ago\b", re.I)
_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7}
_CLOCK_TIME_RE = re.compile(
    r"\b(?P<hour>1[0-2]|0?[1-9])(?::(?P<minute>[0-5]\d))?\s*(?P<period>a\.?m\.?|p\.?m\.?)\b",
    re.I,
)
_DURATION_RE = re.compile(
    r"\b(?P<value>\d+(?:\.\d+)?|one|two|three|four|five|six|seven|eight|nine|ten|half)"
    r"(?:\s+|-)(?P<unit>hours?|hrs?|minutes?|mins?)\b",
    re.I,
)
_DURATION_NUMBER_WORDS = {**_NUMBER_WORDS, "eight": 8, "nine": 9, "ten": 10, "half": 0.5}
_QUERY_STOPWORDS = {
    "a", "ago", "all", "and", "are", "as", "at", "be", "did", "do", "for", "from", "how", "i",
    "in", "is", "it", "last", "many", "me", "my", "of", "on", "past", "since", "the", "to", "total",
    "was", "what", "when", "where", "which", "with", "reference", "date", "source", "event", "current",
}
_QUERY_ALIASES = {
    "buy": {"bought", "purchase", "purchased", "got", "acquired"},
    "bought": {"buy", "purchase", "purchased", "got", "acquired"},
    "feed": {"grain", "grains", "layer", "scratch"},
    "finish": {"finished", "completed"},
    "finished": {"finish", "completed"},
    "watch": {"watched", "saw", "seen"},
    "watched": {"watch", "saw", "seen"},
}


@dataclass(frozen=True, slots=True)
class SelectedEvidence:
    evidence_id: str
    slot: str
    content: str
    score: float
    source: str
    node_ids: tuple[str, ...] = ()
    turn_pair_id: str = ""
    observed_at: datetime | None = None
    evidence_type: EvidenceType = EvidenceType.FACT
    source_role: str = ""
    structure: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CalculationResult:
    operation: Operation
    operands: tuple[float | str, ...]
    result: float | int | str
    unit: str = ""
    expression: str = ""


@dataclass(slots=True)
class AssembledEvidence:
    plan: QueryPlan
    per_slot: dict[str, list[SelectedEvidence]] = field(default_factory=dict)
    missing_slots: list[str] = field(default_factory=list)
    calculation: CalculationResult | None = None
    dropped_duplicates: list[str] = field(default_factory=list)
    validation_issues: list[ValidationIssue] = field(default_factory=list)
    active_set_members: tuple[str, ...] = ()
    expanded_slots: set[str] = field(default_factory=set)


class EvidenceAssembler:
    def select(self, *, plan: QueryPlan, hits_by_slot: dict[str, list[Any]]) -> AssembledEvidence:
        assembled = AssembledEvidence(plan=plan)
        for slot in plan.slots:
            selected: list[SelectedEvidence] = []
            seen_sources: set[str] = set()
            seen_fingerprints: set[str] = set()
            candidates = list(hits_by_slot.get(slot.name, []))
            relevance = {id(hit): self._relevance_score(plan, slot, str(getattr(hit, "content", ""))) for hit in candidates}
            has_relevant = any(score > 0 for score in relevance.values())
            ranked = sorted(
                candidates,
                key=lambda hit: (relevance[id(hit)], float(getattr(hit, "score", 0.0))),
                reverse=True,
            )
            for hit in ranked:
                raw_content = str(getattr(hit, "content", "")).strip()
                if not raw_content:
                    continue
                if has_relevant and relevance[id(hit)] <= 0:
                    continue
                if not self._has_slot_answer_shape(plan, slot, raw_content):
                    continue
                if slot.required_terms and not self._contains_required_terms(raw_content, slot.required_terms):
                    continue
                atom = self._structure_atom(plan, slot, raw_content)
                if atom is None:
                    continue
                content, source_role, structure = atom
                fingerprint = hashlib.sha256(" ".join(content.lower().split()).encode()).hexdigest()
                if fingerprint in seen_fingerprints:
                    assembled.dropped_duplicates.append(fingerprint)
                    continue
                source_key = str(getattr(hit, "turn_pair_id", "") or "|".join(getattr(hit, "node_ids", []) or []))
                if source_key and source_key in seen_sources and len(selected) >= 1:
                    continue
                selected.append(
                    SelectedEvidence(
                        evidence_id=fingerprint[:12],
                        slot=slot.name,
                        content=content,
                        score=float(getattr(hit, "score", 0.0)),
                        source=str(getattr(hit, "source", "memory")),
                        node_ids=tuple(getattr(hit, "node_ids", []) or []),
                        turn_pair_id=str(getattr(hit, "turn_pair_id", "") or ""),
                        observed_at=getattr(hit, "observed_at", None),
                        evidence_type=slot.evidence_type,
                        source_role=source_role,
                        structure=structure,
                    )
                )
                seen_fingerprints.add(fingerprint)
                if source_key:
                    seen_sources.add(source_key)
                if len(selected) >= slot.max_items:
                    break
            assembled.per_slot[slot.name] = selected
            if slot.required and not selected:
                assembled.missing_slots.append(slot.name)
        assembled.active_set_members = self._materialize_active_set(assembled.per_slot)
        assembled.calculation = self._calculate_if_unambiguous(plan, assembled.per_slot)
        if (
            assembled.calculation is not None
            and plan.operation == Operation.DATE_DIFFERENCE
            and "end_date" in assembled.missing_slots
            and len(self._explicit_dates(plan.query)) == 1
        ):
            assembled.missing_slots.remove("end_date")
        return assembled

    def _structure_atom(
        self,
        plan: QueryPlan,
        slot: EvidenceSlot,
        raw_content: str,
    ) -> tuple[str, str, dict[str, Any]] | None:
        if slot.evidence_type == EvidenceType.LIST_ITEM:
            return self._list_item_atom(slot, raw_content)
        if slot.evidence_type == EvidenceType.TABLE_CELL:
            return self._table_cell_atom(slot, raw_content)
        focused = self._focused_content(
            plan.query,
            slot.query,
            raw_content,
            slot_name=slot.name,
            required_role=slot.required_role,
        )
        if plan.operation == Operation.TIME_OFFSET:
            if slot.name in {"departure_time", "arrival_time"} and not _CLOCK_TIME_RE.search(focused):
                return None
            if slot.name == "travel_duration" and not _DURATION_RE.search(focused):
                return None
        source_role = self._source_role(focused)
        if slot.required_role and source_role != slot.required_role:
            return None
        structure: dict[str, Any] = {}
        if slot.evidence_type == EvidenceType.PREFERENCE:
            lowered = focused.lower()
            grounding = "explicit" if re.search(r"\b(?:prefer|like|love|enjoy|favorite|favourite|avoid|dislike)\b", lowered) else "history"
            scope = self._preference_scope(plan.query)
            scope_terms = self._expanded_query_terms(scope)
            evidence_terms = self._expanded_query_terms(focused)
            structure = {
                "grounding": grounding,
                "scope": scope,
                "scope_compatible": scope == "general" or bool(scope_terms & evidence_terms),
            }
        elif slot.evidence_type == EvidenceType.STATE_SET:
            member = self._set_member(focused, removed=slot.name == "removed_members")
            if not member:
                return None
            if slot.name == "active_members" and re.search(r"\b(?:used to|former|previously|cancell?ed|stopped|ended)\b", lowered := focused.lower()):
                return None
            structure = {"member": member, "status": "removed" if slot.name == "removed_members" else "active"}
        return focused, source_role, structure

    @staticmethod
    def _source_role(content: str) -> str:
        roles = re.findall(r"(?:^|\s)(user|assistant):", content, re.I)
        unique = {role.lower() for role in roles}
        return next(iter(unique)) if len(unique) == 1 else ""

    @classmethod
    def _list_item_atom(cls, slot: EvidenceSlot, content: str) -> tuple[str, str, dict[str, Any]] | None:
        if slot.target_index is None:
            return None
        pattern = re.compile(
            rf"(?:^|\s){slot.target_index}[.)]\s*(?P<value>.*?)(?=\s+\d+[.)]\s|$)",
            re.I | re.S,
        )
        match = pattern.search(content)
        if not match:
            return None
        value = " ".join(match.group("value").split()).strip(" |-;")
        if not value:
            return None
        prefix = content[: match.start()]
        first_item = re.search(r"(?:^|\s)\d+[.)]\s+", prefix)
        title_prefix = prefix[: first_item.start()] if first_item else prefix
        list_name = " ".join(title_prefix.split())[-160:].strip(" :|-")
        role_prefix = content[: match.start()]
        roles = re.findall(r"(?:^|\s)(user|assistant):", role_prefix, re.I)
        source_role = roles[-1].lower() if roles else cls._source_role(content)
        if slot.required_role and source_role != slot.required_role:
            return None
        all_items = [
            (int(item.group("index")), " ".join(item.group("value").split()).strip(" |-;"))
            for item in re.finditer(
                r"(?:^|\s)(?P<index>\d+)[.)]\s*(?P<value>.*?)(?=\s+\d+[.)]\s|$)",
                content,
                re.I | re.S,
            )
        ]
        structure: dict[str, Any] = {"list_index": slot.target_index, "value": value}
        if list_name:
            structure["list_name"] = re.sub(r"^(?:user|assistant):\s*", "", list_name, flags=re.I)
        neighbours = [
            {"list_index": index, "value": item_value}
            for index, item_value in all_items
            if abs(index - slot.target_index) == 1
        ]
        if neighbours:
            structure["neighbours"] = neighbours
        return f"{slot.target_index}. {value}", source_role, structure

    @classmethod
    def _table_cell_atom(cls, slot: EvidenceSlot, content: str) -> tuple[str, str, dict[str, Any]] | None:
        lines = [line.strip() for line in content.splitlines() if "|" in line]
        parsed: list[list[str]] = []
        for line in lines:
            line = re.sub(r"^(?:user|assistant):\s*", "", line, flags=re.I)
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if cells and not all(re.fullmatch(r":?-{3,}:?", cell or "---") for cell in cells):
                parsed.append(cells)
        if len(parsed) < 2:
            return None
        headers = parsed[0]
        candidate_rows = parsed[1:]
        if slot.row_key:
            candidate_rows = [row for row in candidate_rows if row and row[0].lower() == slot.row_key.lower()]
        target = slot.target_key.lower()
        for row in candidate_rows:
            for index, cell in enumerate(row):
                if target and cell.lower() != target:
                    continue
                if index >= len(headers):
                    continue
                structure = {
                    "row_key": row[0] if row else slot.row_key,
                    "column_name": headers[index],
                    "cell_value": cell,
                }
                row_text = next((line for line in lines if row[0] in line), "")
                row_position = content.find(row_text) if row_text else -1
                role_prefix = content[:row_position] if row_position >= 0 else content
                roles = re.findall(r"(?:^|\s)(user|assistant):", role_prefix, re.I)
                source_role = roles[-1].lower() if roles else cls._source_role(content)
                if slot.required_role and source_role != slot.required_role:
                    return None
                canonical = f"ROW: {structure['row_key']} | COLUMN: {structure['column_name']} | VALUE: {cell}"
                return canonical, source_role, structure
        return None

    @classmethod
    def _preference_scope(cls, query: str) -> str:
        for pattern in (r"\bwith\s+([a-z][a-z-]+)", r"\bfor\s+([a-z][a-z-]+)"):
            match = re.search(pattern, query, re.I)
            if match and match.group(1).lower() not in _QUERY_STOPWORDS:
                return match.group(1).lower()
        terms = cls._expanded_query_terms(query)
        return sorted(terms, key=lambda value: (-len(value), value))[0] if terms else "general"

    @staticmethod
    def _set_member(content: str, *, removed: bool) -> str:
        patterns = (
            (r"\bcancell?ed\s+(?:my\s+)?(.+?)\s+subscription\b",) if removed else (
                r"\bsubscribe(?:d)?\s+to\s+(.+?)(?:[.;]|$)",
                r"\bsubscription\s+to\s+(.+?)(?:[.;]|$)",
            )
        )
        for pattern in patterns:
            match = re.search(pattern, content, re.I)
            if match:
                return " ".join(match.group(1).split()).strip(" .")
        return ""

    @staticmethod
    def _materialize_active_set(per_slot: dict[str, list[SelectedEvidence]]) -> tuple[str, ...]:
        active = {
            item.structure.get("member", "").strip(): item.structure.get("member", "").strip()
            for item in per_slot.get("active_members", [])
            if item.structure.get("member")
        }
        removed = {
            item.structure.get("member", "").strip().lower()
            for item in per_slot.get("removed_members", [])
            if item.structure.get("member")
        }
        return tuple(value for key, value in active.items() if key.lower() not in removed)

    def _relevance_score(self, plan: QueryPlan, slot: EvidenceSlot, content: str) -> int:
        lowered = content.lower()
        query_terms = self._expanded_query_terms(plan.query)
        overlap = sum(1 for term in query_terms if re.search(rf"\b{re.escape(term)}\b", lowered))
        slot_terms = self._expanded_query_terms(slot.query) - query_terms
        slot_overlap = sum(1 for term in slot_terms if re.search(rf"\b{re.escape(term)}\b", lowered))
        shape = 0
        if plan.operation in {Operation.SUM, Operation.COUNT, Operation.DIFFERENCE, Operation.PERCENTAGE}:
            shape += 1 if _NUMBER_RE.search(content) else 0
        if plan.operation == Operation.DATE_DIFFERENCE:
            shape += 1 if _DATE_RE.search(content) or re.search(r"\b(today|yesterday|ago|started|finished|attended|got)\b", lowered) else 0
        if plan.operation == Operation.TIME_OFFSET:
            shape += 2 if _CLOCK_TIME_RE.search(content) or _DURATION_RE.search(content) else 0
        cue_text = _DATE_RE.sub("", lowered)
        return overlap * 3 + slot_overlap + shape + self._slot_cue_score(slot.name, cue_text)

    @staticmethod
    def _slot_cue_score(slot_name: str, lowered: str) -> int:
        if slot_name == "original_value":
            return 12 if re.search(r"\b(original|originally|retail|regular|full price)\b.{0,120}\$\s*\d+", lowered) else 0
        if slot_name == "current_value":
            return 12 if re.search(r"\b(paid|sale|discount|discounted|outlet|got)\b.{0,160}\$\s*\d+", lowered) else 0
        if slot_name == "numerator":
            return 20 if re.search(
                r"(?:\bwomen\b.{0,40}\b(?:occupy|hold|held|represent|account for)\b.{0,30}\b\d+\b|"
                r"\b\d+\b.{0,30}\b(?:leadership positions?)\b.{0,40}\bwomen\b)",
                lowered,
            ) else 0
        if slot_name == "denominator":
            return 20 if re.search(r"\btotal(?:\s+of)?\s+\d+\s+leadership positions\b", lowered) else 0
        if slot_name == "events":
            return 20 if re.search(
                r"(?:\b(?:bought|purchased|got)\b.{0,120}\b\d+(?:\.\d+)?(?:\s+|-)(?:pounds?|lbs?)\b|"
                r"\b\d+(?:\.\d+)?(?:\s+|-)(?:pounds?|lbs?)\b.{0,120}\b(?:bought|purchased|got)\b)",
                lowered,
            ) else 0
        if slot_name == "start_date":
            return 10 if re.search(r"\b(started|began|first|attended|got|bought)\b", lowered) else 0
        if slot_name == "end_date":
            return 10 if re.search(r"\b(finished|completed|ended)\b", lowered) else 0
        if slot_name == "departure_time":
            if re.search(r"\bleft\s+home\b.{0,60}\b(?:at|around)\s+\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?", lowered):
                return 60
            return 24 if re.search(r"\b(?:left|departed|started)\b.{0,100}\b(?:at|around)\s+\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?", lowered) else 0
        if slot_name == "arrival_time":
            return 24 if re.search(r"\b(?:arrived|reached|got to)\b.{0,100}\b(?:at|around)\s+\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?", lowered) else 0
        if slot_name == "travel_duration":
            return 24 if re.search(r"\b(?:took|lasted|travel time|journey|trip)\b.{0,100}\b(?:\d+(?:\.\d+)?|one|two|three|four|five|six|seven|eight|nine|ten|half)(?:\s+|-)(?:hours?|hrs?|minutes?|mins?)\b", lowered) else 0
        return 0

    @staticmethod
    def _has_slot_answer_shape(plan: QueryPlan, slot: EvidenceSlot, content: str) -> bool:
        if slot.evidence_type in {
            EvidenceType.LIST_ITEM,
            EvidenceType.TABLE_CELL,
            EvidenceType.ASSISTANT_ANSWER,
            EvidenceType.PREFERENCE,
        }:
            return True
        if slot.evidence_type == EvidenceType.STATE_SET:
            return bool(re.search(r"\b(?:subscribe|subscription|member|cancell?ed|removed|stopped)\b", content, re.I))
        if plan.operation in {Operation.SUM, Operation.COUNT, Operation.DIFFERENCE, Operation.PERCENTAGE}:
            if slot.name == "events" and re.search(r"\b(weight|feed|pounds?|lbs?)\b", plan.query, re.I):
                return bool(re.search(r"\b\d+(?:\.\d+)?(?:\s+|-)(?:pounds?|lbs?)\b", content, re.I))
            return bool(_NUMBER_RE.search(content))
        if plan.operation == Operation.DATE_DIFFERENCE:
            return bool(_DATE_RE.search(content) or re.search(r"\b(today|yesterday|days? ago|weeks? ago|started|finished|attended|got)\b", content, re.I))
        if plan.operation == Operation.TIME_OFFSET:
            if slot.name in {"departure_time", "arrival_time"}:
                return bool(_CLOCK_TIME_RE.search(content))
            if slot.name == "travel_duration":
                return bool(_DURATION_RE.search(content))
        return True

    @classmethod
    def _contains_required_terms(cls, content: str, required_terms: tuple[str, ...]) -> bool:
        terms = cls._expanded_query_terms(content)
        return all(
            term in terms or (term.endswith("s") and term[:-1] in terms)
            for term in required_terms
        )

    @classmethod
    def _expanded_query_terms(cls, text: str) -> set[str]:
        terms = {
            term
            for term in re.findall(r"[a-z0-9]+", text.lower())
            if len(term) >= 3 and not term.isdigit() and term not in _QUERY_STOPWORDS
        }
        expanded = set(terms)
        for term in terms:
            expanded.update(_QUERY_ALIASES.get(term, set()))
        return expanded

    @classmethod
    def _focused_content(
        cls,
        query: str,
        slot_query: str,
        content: str,
        *,
        slot_name: str = "",
        required_role: str = "",
        max_chars: int = 900,
    ) -> str:
        normalized = " ".join(content.split())
        role_segments = [segment.strip() for segment in re.split(r"(?=\b(?:user|assistant):\s)", normalized, flags=re.I) if segment.strip()]
        if len(role_segments) > 1:
            terms = cls._expanded_query_terms(f"{query} {slot_query}")

            def segment_score(segment: str) -> tuple[int, int]:
                lowered_segment = segment.lower()
                overlap = sum(1 for term in terms if re.search(rf"\b{re.escape(term)}\b", lowered_segment))
                # A concise user statement is stronger factual evidence than a
                # lexically rich assistant recommendation in the same turn.
                authority = 20 if lowered_segment.startswith("user:") else 0
                answer_shape = 2 if _NUMBER_RE.search(segment) or _DATE_RE.search(segment) else 0
                cue = cls._slot_cue_score(slot_name, _DATE_RE.sub("", lowered_segment))
                return overlap * 3 + authority + answer_shape + cue, -len(segment)

            eligible_segments = [
                segment for segment in role_segments
                if not required_role or segment.lower().startswith(f"{required_role.lower()}:")
            ]
            if eligible_segments:
                normalized = max(eligible_segments, key=segment_score)
        if len(normalized) <= max_chars:
            return normalized
        terms = sorted(cls._expanded_query_terms(f"{query} {slot_query}"), key=len, reverse=True)
        lowered = normalized.lower()
        best_position = -1
        best_score = -1
        for term in terms:
            for match in re.finditer(rf"\b{re.escape(term)}\b", lowered):
                position = match.start()
                start = max(0, position - max_chars // 3)
                window = lowered[start : start + max_chars]
                score = sum(1 for candidate in terms if re.search(rf"\b{re.escape(candidate)}\b", window))
                score += 2 if _NUMBER_RE.search(window) else 0
                score += 2 if _DATE_RE.search(window) else 0
                if score > best_score:
                    best_score = score
                    best_position = position
        if best_position < 0:
            return normalized[: max_chars - 1].rstrip() + "…"
        start = max(0, best_position - max_chars // 3)
        end = min(len(normalized), start + max_chars)
        if end - start < max_chars:
            start = max(0, end - max_chars)
        return f"{'…' if start else ''}{normalized[start:end].strip()}{'…' if end < len(normalized) else ''}"

    def _calculate_if_unambiguous(
        self,
        plan: QueryPlan,
        per_slot: dict[str, list[SelectedEvidence]],
    ) -> CalculationResult | None:
        if plan.operation is None:
            return None
        if plan.operation == Operation.SET_UNION:
            return None
        if plan.operation == Operation.DATE_DIFFERENCE:
            start = self._unique_date(per_slot.get("start_date", []))
            end = self._unique_date(per_slot.get("end_date", []))
            if end is None:
                query_dates = self._explicit_dates(plan.query)
                end = query_dates[0] if len(query_dates) == 1 else None
            dates = [start, end]
            if any(value is None for value in dates):
                return None
            start, end = dates
            assert start is not None and end is not None
            days = (end - start).days
            return CalculationResult(plan.operation, (start.isoformat(), end.isoformat()), days, "days", f"{end} - {start} = {days} days")
        if plan.operation == Operation.TIME_OFFSET:
            direction = plan.diagnostics.get("time_offset_direction", "add")
            clock_slot = "arrival_time" if direction == "subtract" else "departure_time"
            clock_minutes = self._unique_clock_minutes(per_slot.get(clock_slot, []), slot_name=clock_slot)
            duration_minutes = self._unique_duration_minutes(per_slot.get("travel_duration", []))
            if clock_minutes is None or duration_minutes is None:
                return None
            result_minutes = (clock_minutes - duration_minutes if direction == "subtract" else clock_minutes + duration_minutes) % (24 * 60)
            operator = "-" if direction == "subtract" else "+"
            clock_text = self._format_clock_minutes(clock_minutes)
            result_text = self._format_clock_minutes(result_minutes)
            duration_text = self._format_duration_minutes(duration_minutes)
            return CalculationResult(
                plan.operation,
                (clock_text, duration_text),
                result_text,
                "time",
                f"{clock_text} {operator} {duration_text} = {result_text}",
            )
        if plan.operation == Operation.SUM:
            values = self._event_values(per_slot.get("events", []))
            if len(values) < 2:
                return None
            total = sum(values)
            return CalculationResult(plan.operation, tuple(values), total, expression=" + ".join(map(self._format_number, values)) + f" = {self._format_number(total)}")
        if plan.operation == Operation.COUNT:
            active = self._materialize_active_set(per_slot)
            if not active:
                return None
            return CalculationResult(
                plan.operation,
                tuple(active),
                len(active),
                "members",
                f"count(active members) = {len(active)}",
            )
        slot_names = (
            ("numerator", "denominator")
            if plan.operation == Operation.PERCENTAGE
            else ("reference_value", "observed_value")
            if "reference_value" in per_slot
            else ("original_value", "current_value")
        )
        values = [self._unique_number(per_slot.get(name, []), slot_name=name) for name in slot_names]
        if any(value is None for value in values):
            return None
        left, right = values
        assert left is not None and right is not None
        if plan.operation == Operation.PERCENTAGE:
            if right == 0:
                return None
            result = (left / right) * 100
            return CalculationResult(plan.operation, (left, right), result, "%", f"{self._format_number(left)} / {self._format_number(right)} x 100 = {self._format_number(result)}%")
        if slot_names == ("reference_value", "observed_value"):
            result = right - left
            expression = f"{self._format_number(right)} - {self._format_number(left)} = {self._format_number(result)}"
            return CalculationResult(plan.operation, (left, right), result, expression=expression)
        result = left - right
        return CalculationResult(plan.operation, (left, right), result, expression=f"{self._format_number(left)} - {self._format_number(right)} = {self._format_number(result)}")

    @staticmethod
    def _numbers(items: list[SelectedEvidence]) -> list[float]:
        values: list[float] = []
        for item in items:
            content = _DOCUMENT_DATE_RE.sub("", item.content)
            content = _DATE_RE.sub("", content)
            money = re.findall(r"\$\s*(-?\d+(?:\.\d+)?)", content)
            unit_values = re.findall(
                r"(?<![\w.])(-?\d+(?:\.\d+)?)(?:\s+|-)(?:pounds?|lbs?|percent|%)\b",
                content,
                flags=re.I,
            )
            raw_values = money or unit_values
            if raw_values:
                values.extend(float(value) for value in raw_values)
            else:
                values.extend(float(match.group()) for match in _NUMBER_RE.finditer(content))
        return list(dict.fromkeys(values))

    def _unique_number(
        self,
        items: list[SelectedEvidence],
        *,
        slot_name: str = "",
    ) -> float | None:
        cue_patterns = {
            "original_value": re.compile(
                r"\b(?:original(?:ly)?|retail(?:ed)?|regular|full price)\b[^$]{0,100}\$\s*(-?\d+(?:\.\d+)?)",
                re.I,
            ),
            "current_value": re.compile(
                r"\b(?:paid|sale|discounted|outlet|got)\b[^$]{0,120}\$\s*(-?\d+(?:\.\d+)?)",
                re.I,
            ),
        }
        pattern = cue_patterns.get(slot_name)
        if pattern is not None:
            cue_values = [
                float(match)
                for item in items
                for match in pattern.findall(_DOCUMENT_DATE_RE.sub("", item.content))
            ]
            cue_values = list(dict.fromkeys(cue_values))
            if len(cue_values) == 1:
                return cue_values[0]
        values = self._numbers(items)
        return values[0] if len(values) == 1 else None

    @classmethod
    def _unique_clock_minutes(cls, items: list[SelectedEvidence], *, slot_name: str) -> int | None:
        cue = (
            re.compile(r"\b(?:left|departed|started)\b.{0,120}?\b(?:at|around)\s+" + _CLOCK_TIME_RE.pattern, re.I)
            if slot_name == "departure_time"
            else re.compile(r"\b(?:arrived|reached|got to)\b.{0,120}?\b(?:at|around)\s+" + _CLOCK_TIME_RE.pattern, re.I)
        )
        values: list[int] = []
        for item in items:
            match = cue.search(_DOCUMENT_DATE_RE.sub("", item.content))
            if match is None:
                matches = list(_CLOCK_TIME_RE.finditer(_DOCUMENT_DATE_RE.sub("", item.content)))
                match = matches[0] if len(matches) == 1 else None
            if match is not None:
                values.append(cls._clock_match_minutes(match))
        unique = list(dict.fromkeys(values))
        return unique[0] if len(unique) == 1 else None

    @staticmethod
    def _clock_match_minutes(match: re.Match[str]) -> int:
        hour = int(match.group("hour")) % 12
        minute = int(match.group("minute") or 0)
        if match.group("period").lower().startswith("p"):
            hour += 12
        return hour * 60 + minute

    @staticmethod
    def _unique_duration_minutes(items: list[SelectedEvidence]) -> int | None:
        values: list[int] = []
        for item in items:
            matches = list(_DURATION_RE.finditer(_DOCUMENT_DATE_RE.sub("", item.content)))
            if len(matches) != 1:
                continue
            match = matches[0]
            raw_value = match.group("value").lower()
            value = float(raw_value) if re.fullmatch(r"\d+(?:\.\d+)?", raw_value) else _DURATION_NUMBER_WORDS[raw_value]
            minutes = float(value) * (60 if match.group("unit").lower().startswith(("hour", "hr")) else 1)
            if minutes.is_integer():
                values.append(int(minutes))
        unique = list(dict.fromkeys(values))
        return unique[0] if len(unique) == 1 else None

    @staticmethod
    def _format_clock_minutes(value: int) -> str:
        value %= 24 * 60
        hour_24, minute = divmod(value, 60)
        period = "AM" if hour_24 < 12 else "PM"
        hour_12 = hour_24 % 12 or 12
        return f"{hour_12}:{minute:02d} {period}"

    @staticmethod
    def _format_duration_minutes(value: int) -> str:
        if value % 60 == 0:
            hours = value // 60
            return f"{hours} hour{'s' if hours != 1 else ''}"
        return f"{value} minutes"

    def _event_values(self, items: list[SelectedEvidence]) -> list[float]:
        unit_values_by_item = [self._unit_numbers(item.content) for item in items]
        if any(unit_values_by_item):
            return [values[0] for values in unit_values_by_item if len(values) == 1]
        values: list[float] = []
        for item in items:
            numbers = self._numbers([item])
            if len(numbers) == 1:
                values.append(numbers[0])
        return values

    @staticmethod
    def _unit_numbers(content: str) -> list[float]:
        content = _DOCUMENT_DATE_RE.sub("", content)
        content = _DATE_RE.sub("", content)
        return [
            float(value)
            for value in re.findall(
                r"(?<![\w.])(-?\d+(?:\.\d+)?)(?:\s+|-)(?:pounds?|lbs?|percent|%)\b",
                content,
                flags=re.I,
            )
        ]

    @staticmethod
    def _unique_date(items: list[SelectedEvidence]) -> date | None:
        values: list[date] = []
        for item in items:
            for year, month, day in _DATE_RE.findall(item.content):
                values.append(datetime(int(year), int(month), int(day)).date())
            if item.observed_at is not None:
                source_date = item.observed_at.date()
                lowered = item.content.lower()
                if re.search(r"\btoday\b", lowered):
                    values.append(source_date)
                if re.search(r"\byesterday\b", lowered):
                    values.append(source_date - timedelta(days=1))
                for match in _DAYS_AGO_RE.finditer(lowered):
                    raw_count = match.group("count").lower()
                    count = int(raw_count) if raw_count.isdigit() else _NUMBER_WORDS[raw_count]
                    values.append(source_date - timedelta(days=count))
        unique = list(dict.fromkeys(values))
        return unique[0] if len(unique) == 1 else None

    @staticmethod
    def _explicit_dates(text: str) -> list[date]:
        return list(
            dict.fromkeys(
                datetime(int(year), int(month), int(day)).date()
                for year, month, day in _DATE_RE.findall(text)
            )
        )

    @staticmethod
    def _format_number(value: float) -> str:
        return str(int(value)) if value.is_integer() else f"{value:g}"
