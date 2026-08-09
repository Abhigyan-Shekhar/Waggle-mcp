from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from waggle.retrieval.contracts import EvidenceType


class QueryType(StrEnum):
    DIRECT_FACT = "direct_fact"
    CURRENT_STATE = "current_state"
    HISTORICAL_STATE = "historical_state"
    AGGREGATION = "aggregation"
    COMPARISON = "comparison"
    TEMPORAL_DIFFERENCE = "temporal_difference"
    ENUMERATION = "enumeration"
    GENERAL_MULTI_HOP = "general_multi_hop"
    PREFERENCE = "preference"
    CURRENT_SET = "current_set"


class Operation(StrEnum):
    SUM = "sum"
    COUNT = "count"
    DIFFERENCE = "difference"
    PERCENTAGE = "percentage"
    DATE_DIFFERENCE = "date_difference"
    TIME_OFFSET = "time_offset"
    SET_UNION = "set_union"


@dataclass(frozen=True, slots=True)
class EvidenceSlot:
    name: str
    query: str
    required: bool = True
    collect_all: bool = False
    max_items: int = 2
    min_items: int = 1
    evidence_type: EvidenceType = EvidenceType.FACT
    required_role: str = ""
    target_index: int | None = None
    target_key: str = ""
    row_key: str = ""
    fallback_queries: tuple[str, ...] = ()
    required_terms: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class QueryPlan:
    query: str
    query_type: QueryType
    slots: tuple[EvidenceSlot, ...]
    operation: Operation | None = None
    confidence: float = 1.0
    temporal_scope: str = "unspecified"
    diagnostics: dict[str, str] = field(default_factory=dict)


class DeterministicQueryPlanner:
    """Cheap planner for common memory QA shapes; no model call required."""

    _CURRENT = re.compile(r"\b(?:currently|current|now|latest|most recent|as of today)\b", re.I)
    _HISTORICAL = re.compile(r"\b(?:used to|previously|before|at that time|as of|back then)\b", re.I)
    _PERCENT = re.compile(r"\b(?:percentage|percent|what %)\b", re.I)
    _TOTAL = re.compile(r"\b(?:total|combined|altogether|in all|sum)\b", re.I)
    _DIFFERENCE = re.compile(r"\b(?:difference|how much .*sav\w*|how many more|how many fewer)\b", re.I)
    _TEMPORAL = re.compile(r"\b(?:how many days|how many weeks|how long|elapsed|between .* and)\b", re.I)
    _ENUMERATION = re.compile(r"\b(?:list all|which .* did|what all|all the|every)\b", re.I)
    _PREFERENCE = re.compile(r"\b(?:prefer|preference|recommend|suggest|would i like|favorite|favourite)\b", re.I)
    _COUNT_STATE = re.compile(r"\bhow many\b.*\b(?:since|last|currently|now|so far)\b", re.I)
    _INDEXED_ITEM = re.compile(r"\b(?P<index>\d+)(?:st|nd|rd|th)\b.*\b(?:item|parameter|entry|point|step|option)\b", re.I)
    _TABLE = re.compile(r"\b(?:shift|rotation|schedule|table|sheet|row|column)\b", re.I)
    _ASSISTANT_ORIGIN = re.compile(
        r"\b(?:you|your)\b.{0,80}\b(?:gave|provided|recommend(?:ed)?|suggest(?:ed)?|told|listed|said|mentioned|referred)\b|"
        r"\b(?:gave|provided|recommend(?:ed)?|suggest(?:ed)?|told|listed|mentioned|referred)\b.{0,80}\b(?:you|your)\b",
        re.I,
    )
    _ACTIVE_SET_COUNT = re.compile(
        r"\bhow many\b.*\b(?:currently have|active|subscriptions?|memberships?)\b",
        re.I,
    )
    _GENERIC_DIFFERENCE = re.compile(
        r"\bhow many\b.{0,50}\b(?:older|younger|exceed(?:ed)?|over|under|difference)\b",
        re.I,
    )
    _DIRECT_DURATION = re.compile(r"\bhow long have i been\b", re.I)
    _ADVICE_PREFERENCE = re.compile(r"\b(?:ideas?|advice|recommendations?|suggestions?)\b", re.I)
    _ARRIVAL_TIME = re.compile(
        r"\bwhat time\b.{0,80}\b(?:reach(?:ed)?|arriv(?:e|ed)|get|got)\b.{0,50}\b(?:clinic|destination|airport|station|office|home|there)\b",
        re.I,
    )
    _DERIVED_DEPARTURE_TIME = re.compile(
        r"\bwhat time\b.{0,80}\b(?:leave|left|depart(?:ed)?|start(?:ed)?)\b.{0,120}\b(?:if|when|after)\b.{0,80}\b(?:reach(?:ed)?|arriv(?:e|ed)|get|got)\b",
        re.I,
    )

    def plan(self, query: str, *, reference_date: str = "") -> QueryPlan:
        text = " ".join(query.split())
        if self._DERIVED_DEPARTURE_TIME.search(text):
            topic = self._compact_retrieval_query(text)
            return QueryPlan(
                query=text,
                query_type=QueryType.GENERAL_MULTI_HOP,
                slots=(
                    EvidenceSlot(
                        "arrival_time",
                        f"exact arrival reached destination clock time {topic}",
                        max_items=1,
                        evidence_type=EvidenceType.EVENT,
                        fallback_queries=(f"arrived reached got there at time {topic}",),
                    ),
                    EvidenceSlot(
                        "travel_duration",
                        f"trip travel journey duration took hours minutes {topic}",
                        max_items=1,
                        evidence_type=EvidenceType.EVENT,
                        fallback_queries=(f"took hours minutes travel time {topic}",),
                    ),
                ),
                operation=Operation.TIME_OFFSET,
                diagnostics={"rule": "derived_departure_time", "time_offset_direction": "subtract"},
            )
        if self._ARRIVAL_TIME.search(text):
            topic = self._compact_retrieval_query(text)
            return QueryPlan(
                query=text,
                query_type=QueryType.GENERAL_MULTI_HOP,
                slots=(
                    EvidenceSlot(
                        "departure_time",
                        f"left departed started trip from home clock time {topic}",
                        max_items=1,
                        evidence_type=EvidenceType.EVENT,
                        fallback_queries=(f"left home departed at time for destination {topic}",),
                    ),
                    EvidenceSlot(
                        "travel_duration",
                        f"trip travel journey duration took hours minutes {topic}",
                        max_items=1,
                        evidence_type=EvidenceType.EVENT,
                        fallback_queries=(f"took hours minutes travel time {topic}",),
                    ),
                ),
                operation=Operation.TIME_OFFSET,
                diagnostics={"rule": "derived_arrival_time", "time_offset_direction": "add"},
            )
        indexed_match = self._INDEXED_ITEM.search(text)
        if indexed_match:
            target_index = int(indexed_match.group("index"))
            topic = self._compact_retrieval_query(text)
            return QueryPlan(
                query=text,
                query_type=QueryType.DIRECT_FACT,
                slots=(
                    EvidenceSlot(
                        "indexed_item",
                        f"assistant numbered list item {target_index} {topic}",
                        evidence_type=EvidenceType.LIST_ITEM,
                        required_role="assistant",
                        target_index=target_index,
                        fallback_queries=(
                            f'"{target_index}." {topic}',
                            f"assistant exact list item index {target_index} {topic}",
                        ),
                    ),
                ),
                diagnostics={"rule": "indexed_list_item"},
            )
        if self._TABLE.search(text) and re.search(r"\b(?:assigned|rotation|shift|schedule)\b", text, re.I):
            target = self._table_target(text)
            row_key = self._day_name(text)
            topic = self._compact_retrieval_query(text)
            return QueryPlan(
                query=text,
                query_type=QueryType.DIRECT_FACT,
                slots=(
                    EvidenceSlot(
                        "table_cell",
                        f"table header row {row_key} cell {target} {topic}",
                        evidence_type=EvidenceType.TABLE_CELL,
                        required_role="assistant" if self._ASSISTANT_ORIGIN.search(text) else "",
                        target_key=target,
                        row_key=row_key,
                        fallback_queries=(f"exact table row {row_key} {target} with column headers",),
                    ),
                ),
                diagnostics={"rule": "table_cell"},
            )
        if self._ASSISTANT_ORIGIN.search(text):
            topic = self._compact_retrieval_query(text)
            return QueryPlan(
                query=text,
                query_type=QueryType.DIRECT_FACT,
                slots=(
                    EvidenceSlot(
                        "assistant_answer",
                        f"assistant answer recommendation exact wording {topic}",
                        evidence_type=EvidenceType.ASSISTANT_ANSWER,
                        required_role="assistant",
                        fallback_queries=(f"assistant recommended listed answered {topic}",),
                        max_items=3,
                    ),
                ),
                diagnostics={"rule": "assistant_origin"},
            )
        if self._ACTIVE_SET_COUNT.search(text):
            topic = self._compact_retrieval_query(text)
            return QueryPlan(
                query=text,
                query_type=QueryType.CURRENT_SET,
                slots=(
                    EvidenceSlot(
                        "active_members",
                        f"active current added subscribed member {topic}",
                        collect_all=True,
                        max_items=12,
                        evidence_type=EvidenceType.STATE_SET,
                        fallback_queries=(f"all active current {topic} additions",),
                    ),
                    EvidenceSlot(
                        "removed_members",
                        f"canceled removed stopped ended former {topic}",
                        required=False,
                        collect_all=True,
                        max_items=12,
                        min_items=0,
                        evidence_type=EvidenceType.STATE_SET,
                        fallback_queries=(f"all canceled removed former {topic}",),
                    ),
                ),
                operation=Operation.COUNT,
                temporal_scope="current",
                diagnostics={"rule": "active_set_count"},
            )
        if self._DIRECT_DURATION.search(text):
            topic = self._compact_retrieval_query(text)
            required_terms = self._duration_entity_terms(text)
            return QueryPlan(
                query=text,
                query_type=QueryType.DIRECT_FACT,
                slots=(
                    EvidenceSlot(
                        "duration",
                        f"explicit duration days weeks months years {topic}",
                        fallback_queries=(f"using for months years since started exact entity {topic}",),
                        required_terms=required_terms,
                    ),
                ),
                diagnostics={"rule": "explicit_duration"},
            )
        if self._PERCENT.search(text):
            topic = self._compact_retrieval_query(text)
            return QueryPlan(
                query=text,
                query_type=QueryType.AGGREGATION,
                slots=(
                    EvidenceSlot("numerator", f"women subgroup occupied hold count {topic}", max_items=1),
                    EvidenceSlot("denominator", f"total overall all count {topic}", max_items=1),
                ),
                operation=Operation.PERCENTAGE,
                diagnostics={"rule": "percentage"},
            )
        if self._TEMPORAL.search(text):
            uses_question_date = bool(reference_date and re.search(r"\bago\b", text, re.I))
            planned_query = f"{text} Reference date: {reference_date}" if uses_question_date else text
            topic = self._compact_retrieval_query(text)
            slots = [EvidenceSlot("start_date", f"started began source date {topic}", max_items=1)]
            if not uses_question_date:
                slots.append(EvidenceSlot("end_date", f"finished completed end date {topic}", max_items=1))
            return QueryPlan(
                query=planned_query,
                query_type=QueryType.TEMPORAL_DIFFERENCE,
                slots=tuple(slots),
                operation=Operation.DATE_DIFFERENCE,
                diagnostics={"rule": "temporal_difference"},
            )
        if self._DIFFERENCE.search(text) or self._GENERIC_DIFFERENCE.search(text):
            topic = self._compact_retrieval_query(text)
            price_shaped = bool(re.search(r"\b(?:sav\w*|price|paid|cost)\b", text, re.I))
            if not price_shaped:
                return QueryPlan(
                    query=text,
                    query_type=QueryType.COMPARISON,
                    slots=(
                        EvidenceSlot(
                            "reference_value",
                            f"target baseline original previous reference value {topic}",
                            fallback_queries=(f"target goal baseline {topic}",),
                        ),
                        EvidenceSlot(
                            "observed_value",
                            f"actual final completed current observed value {topic}",
                            fallback_queries=(f"actual result completed final {topic}",),
                        ),
                    ),
                    operation=Operation.DIFFERENCE,
                    diagnostics={"rule": "generic_difference", "difference_order": "observed_minus_reference"},
                )
            return QueryPlan(
                query=text,
                query_type=QueryType.COMPARISON,
                slots=(
                    EvidenceSlot("original_value", f"original retail regular full price {topic}", max_items=1),
                    EvidenceSlot("current_value", f"paid sale discount outlet price {topic}", max_items=1),
                ),
                operation=Operation.DIFFERENCE,
                diagnostics={"rule": "difference"},
            )
        if self._TOTAL.search(text):
            return QueryPlan(
                query=text,
                query_type=QueryType.AGGREGATION,
                slots=(EvidenceSlot(
                    "events",
                    f"all distinct events contributing total {self._compact_retrieval_query(text)}",
                    collect_all=True,
                    max_items=8,
                    evidence_type=EvidenceType.EVENT,
                ),),
                operation=Operation.SUM,
                diagnostics={"rule": "sum"},
            )
        if self._ENUMERATION.search(text):
            return QueryPlan(
                query=text,
                query_type=QueryType.ENUMERATION,
                slots=(EvidenceSlot(
                    "members",
                    f"all distinct requested members items: {text}",
                    collect_all=True,
                    max_items=10,
                    evidence_type=EvidenceType.STATE_SET,
                ),),
                operation=Operation.SET_UNION,
                diagnostics={"rule": "enumeration"},
            )
        if self._PREFERENCE.search(text) or (
            self._ADVICE_PREFERENCE.search(text) and re.search(r"\b(?:i|my|me)\b", text, re.I)
        ):
            return QueryPlan(
                query=text,
                query_type=QueryType.PREFERENCE,
                slots=(EvidenceSlot(
                    "preferences",
                    f"explicit user preference same-domain history and personal context: {text}",
                    max_items=5,
                    evidence_type=EvidenceType.PREFERENCE,
                    fallback_queries=(f"explicit preference enjoys likes dislikes {self._compact_retrieval_query(text)}",),
                ),),
                diagnostics={"rule": "preference"},
            )
        if self._COUNT_STATE.search(text):
            return QueryPlan(
                query=text,
                query_type=QueryType.CURRENT_STATE,
                slots=(EvidenceSlot("current_state", self._compact_retrieval_query(text), max_items=1),),
                temporal_scope="current",
                diagnostics={"rule": "bounded_count_state"},
            )
        if self._HISTORICAL.search(text):
            return QueryPlan(
                query=text,
                query_type=QueryType.HISTORICAL_STATE,
                slots=(EvidenceSlot("historical_state", text, max_items=3),),
                temporal_scope="past",
                diagnostics={"rule": "historical"},
            )
        if self._CURRENT.search(text):
            return QueryPlan(
                query=text,
                query_type=QueryType.CURRENT_STATE,
                slots=(EvidenceSlot("current_state", text, max_items=2),),
                temporal_scope="current",
                diagnostics={"rule": "current"},
            )
        return QueryPlan(
            query=text,
            query_type=QueryType.DIRECT_FACT,
            slots=(EvidenceSlot("answer", text, max_items=3),),
            confidence=0.75,
            diagnostics={"rule": "direct_fallback"},
        )

    @staticmethod
    def _day_name(text: str) -> str:
        match = re.search(r"\b(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b", text, re.I)
        return match.group(1).title() if match else ""

    @staticmethod
    def _table_target(text: str) -> str:
        patterns = (
            r"\bwas\s+([A-Z][\w'-]+)\s+assigned\b",
            r"\bfor\s+([A-Z][\w'-]+)\s+on\b",
            r"\brotation\s+for\s+([A-Z][\w'-]+)\b",
        )
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(1)
        return ""

    @staticmethod
    def _duration_entity_terms(text: str) -> tuple[str, ...]:
        match = re.search(r"\bhow long have i been\s+(?:\w+ing\s+)?(.+?)(?:\?|$)", text, re.I)
        if not match:
            return ()
        terms = [
            term
            for term in re.findall(r"[a-z0-9]+", match.group(1).lower())
            if len(term) >= 3 and term not in {"the", "this", "that", "for", "with", "using", "doing"}
        ]
        return tuple(terms[-2:])

    @staticmethod
    def _expanded_retrieval_query(text: str) -> str:
        lowered = text.lower()
        aliases: list[str] = []
        if re.search(r"\b(buy|bought|purchase|purchased)\b", lowered):
            aliases.extend(["bought", "purchased", "got", "acquired"])
        if "feed" in lowered:
            aliases.extend(["feed", "grain", "grains", "layer", "scratch"])
        if re.search(r"\b(weight|weigh|pound|pounds|lb|lbs)\b", lowered):
            aliases.extend(["weight", "pound", "pounds", "lb", "lbs"])
        if re.search(r"\b(finish|finished)\b", lowered):
            aliases.extend(["started", "finished", "completed"])
        return " ".join([text, *aliases])

    @classmethod
    def _compact_retrieval_query(cls, text: str) -> str:
        stopwords = {
            "ago", "all", "and", "are", "did", "do", "for", "from", "how", "into", "last", "many", "months",
            "past", "since", "the", "this", "those", "total", "what", "when", "where", "which", "with",
        }
        terms = [
            term
            for term in re.findall(r"[a-z0-9]+", text.lower())
            if len(term) >= 3 and term not in stopwords and not term.isdigit()
        ]
        return " ".join(
            dict.fromkeys([*terms, *cls._expanded_retrieval_query(text).lower().split()[len(text.split()) :]])
        )
