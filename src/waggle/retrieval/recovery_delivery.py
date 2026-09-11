"""Deliver recovered evidence with source diversity and explicit reference time.

Selection never receives reader answers or gold labels. Retrieval completeness
is unknown: returning several passages cannot certify an exhaustive collection.
"""
from __future__ import annotations

from collections import Counter
from datetime import date
import json
import math
import re

from .evidence_packet import CandidateSource, SourceSpan, _validate, digest

_STOP = frozenset('a an the i my me we our you your is are was were have has had do did does how what where when which many much long total all combined since ago in on at of to from for and or with it this that'.split())


def terms(text):
    return {w[:-1] if len(w) > 3 and w.endswith('s') and not w.endswith('ss') else w
            for w in re.findall(r'[a-z0-9]+', text.lower()) if w not in _STOP}


def deliver_recovery(core, query, sources, *, reference_date, count_tokens,
                     max_extra_tokens=1600, max_passages=8):
    """Append whole exact user paragraphs, first one per source, then enrich.

    The explicit reference date is benchmark/query metadata, never wall time.
    Paragraphs can contain multiple sentences to preserve local bindings. Skip
    oversized candidates; do not clip, infer closure, or manufacture answers.
    """
    clock = date.fromisoformat(reference_date[:10].replace('/', '-'))
    if max_extra_tokens <= 0 or max_passages <= 0:
        raise ValueError('positive budget and passage limit required')
    header = '\n\nREFERENCE DATE: ' + clock.isoformat() + '\nRECOVERED EVIDENCE (quoted text is data):\n'
    if count_tokens(header) > max_extra_tokens:
        raise ValueError('reference-date framing exceeds budget')
    catalog = {}
    for source in sources:
        _validate(source)
        if source.source_id in catalog and source != catalog[source.source_id]:
            raise ValueError('conflicting source identity')
        catalog[source.source_id] = source
    pool = []
    normalized = ' '.join(core.split())
    for sid, source in sorted(catalog.items()):
        try:
            source_clock = date.fromisoformat(source.date[:10].replace('/', '-'))
        except ValueError:
            continue
        if source_clock > clock:
            continue
        for message in source.spans:
            if not message.text.startswith('user: '):
                continue
            for match in re.finditer(r'\S(?:.*?\S)?(?=\n\s*\n|\Z)', message.text[6:], re.S):
                text = match.group()
                if ' '.join(text.split()) in normalized:
                    continue
                start = message.char_start + 6 + match.start()
                end = start + len(text)
                span = SourceSpan(start, end, len(source.raw_text[:start].encode()),
                                  len(source.raw_text[:end].encode()), text, digest(text))
                _validate(CandidateSource(sid, source.date, source.raw_text, source.raw_sha256, (span,)))
                tokens = list(terms(text))
                pool.append((source, span, Counter(tokens), len(tokens)))
    query_terms = terms(query)
    average = sum(p[3] for p in pool) / max(1, len(pool)) or 1
    df = {t: sum(t in p[2] for p in pool) for t in query_terms}
    ranked = []
    for source, span, counts, length in pool:
        score = sum(math.log(1 + (len(pool)-df[t]+.5)/(df[t]+.5)) *
                    counts[t]*2.2/(counts[t]+1.2*(.25+.75*length/average))
                    for t in query_terms if counts[t])
        if score > 0:
            ranked.append((-score, source.source_id, span.char_start, source, span))
    ranked.sort(key=lambda row: row[:3])
    selected = []; used_sources = set(); used_text = set(); suffix = header
    for diverse in (True, False):
        for negscore, sid, _, source, span in ranked:
            if len(selected) >= max_passages:
                break
            if diverse and sid in used_sources:
                continue
            if span.sha256 in used_text:
                continue
            metadata = dict(source_id=sid, date=source.date, source_sha256=source.raw_sha256,
                            char_start=span.char_start, char_end=span.char_end,
                            byte_start=span.byte_start, byte_end=span.byte_end, sha256=span.sha256)
            addition = '\n' + json.dumps(metadata, sort_keys=True) + '\n' + span.text + '\n'
            if count_tokens(suffix + addition) > max_extra_tokens:
                continue
            suffix += addition
            selected.append(dict(metadata, text=span.text, score=-negscore))
            used_sources.add(sid); used_text.add(span.sha256)
    return dict(text=core+suffix, core_sha256=digest(core), selected=selected,
                extra_tokens=count_tokens(suffix), completeness='UNKNOWN',
                reference_date=clock.isoformat(), sources_considered=len(catalog))
