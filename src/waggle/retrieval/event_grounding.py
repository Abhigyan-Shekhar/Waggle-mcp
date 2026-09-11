"""Conservative lexical binding for temporal operands; no reference answers."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

_DATE = re.compile(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b")
_DOCUMENT = re.compile(r"\[documentDate:[^\]]+\]", re.I)
_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
          "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_RELATIVE = re.compile(r"\b(\d+|" + "|".join(_WORDS) + r")\s+(days?|weeks?)\s+ago\b", re.I)
_MONTHS = {name.lower(): index for index, name in enumerate(
    ('January','February','March','April','May','June','July','August','September','October','November','December'), 1)}
_NAMED_DATE = re.compile(r'\b(' + '|'.join(_MONTHS) + r')\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(20\d{2}))?\b', re.I)
_STOP = frozenset("a an the i my me we our you your of in on at to from for with and or "
                  "between day days date event events time when that did do was were is am are "
                  "have had has been got get started start began begin finished finish ended end "
                  "attend attended participate participated discovered discover replaced replace "
                  "bought buy received receive completed complete today yesterday ago just "
                  "recently last first later old new local source question".split())


def event_terms(text: str) -> set[str]:
    """Light plural normalization only; never expand an event into another event."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w[:-1] if w.endswith('s') and not w.endswith('ss') and len(w)>3 else w
            for w in words if len(w)>2 and not w.isdigit() and w not in _STOP}


def comparison_events(query: str) -> tuple[str, str] | None:
    match = re.search(r"\bbetween\s+(.+?)\s+and\s+(.+?)(?:\?|$)", query, re.I)
    if not match:
        return None
    start, end = match.groups()
    # Multiple conjunctions are ambiguous without a semantic parser.
    if re.search(r"\band\b", start + ' ' + end, re.I):
        return None
    if not event_terms(start) or not event_terms(end):
        return None
    if not (event_terms(start) - event_terms(end)) or not (event_terms(end) - event_terms(start)):
        return None
    return start.strip(), end.strip()


def matches_event(text: str, event: str, other_event: str = '') -> bool:
    own = event_terms(event)
    distinctive = own - event_terms(other_event)
    present = event_terms(text)
    return bool(distinctive & present) and len(own & present) >= min(2, len(own))


def explicit_dates(text: str) -> set[date]:
    found = set()
    for year, month, day in _DATE.findall(text):
        try:
            found.add(date(int(year), int(month), int(day)))
        except ValueError:
            # An invalid date must not crash retrieval or turn into an operand.
            continue
    return found


def resolve_event_date(text: str, observed_at: datetime | None, *, event: str = '',
                       other_event: str = '') -> date | None:
    """Resolve only a uniquely dated relevant sentence.

    A document timestamp anchors 'today/yesterday/N days ago'; it is not itself
    evidence that the event happened on that date. Conflicting dates abstain.
    """
    documents = set().union(*(explicit_dates(m.group()) for m in _DOCUMENT.finditer(text)))
    clock = next(iter(documents)) if len(documents)==1 else observed_at.date() if observed_at else None
    if len(documents)>1:
        return None
    body = _DOCUMENT.sub('', text)
    # Keep common name/title abbreviations attached to their dated clause.
    sentences = re.split(r"(?<!St\.)(?<!Mr\.)(?<!Ms\.)(?<!Dr\.)(?<!Mrs\.)(?<=[.!?])\s+|\n", body)
    values: set[date] = set()
    matched = False
    for sentence in sentences:
        if event and not matches_event(sentence, event, other_event):
            continue
        matched = True
        found = explicit_dates(sentence)
        for match in _NAMED_DATE.finditer(sentence):
            month, day, year = match.groups()
            if not year and clock is None:
                return None
            try:
                value = date(int(year) if year else clock.year, _MONTHS[month.lower()], int(day))
                if not year and value > clock:
                    # A yearless date ahead of the source date is ambiguous.
                    # Do not silently assume either a future or previous year.
                    return None
                found.add(value)
            except ValueError:
                return None
        relative: set[date] = set()
        if re.search(r"\b(?:today|yesterday)\b", sentence, re.I) or _RELATIVE.search(sentence):
            if clock is None:
                return None
            if re.search(r"\btoday\b", sentence, re.I):
                relative.add(clock)
            if re.search(r"\byesterday\b", sentence, re.I):
                relative.add(clock - timedelta(days=1))
            for match in _RELATIVE.finditer(sentence):
                n, unit = match.groups()
                count = int(n) if n.isdigit() else _WORDS[n.lower()]
                relative.add(clock - timedelta(days=count * (7 if unit.lower().startswith('week') else 1)))
        values.update(found | relative)
    return next(iter(values)) if matched and len(values)==1 else None
