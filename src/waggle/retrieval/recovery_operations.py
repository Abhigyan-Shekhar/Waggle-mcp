"""Conservative operations over locally bound, exact evidence sentences."""
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
import re

from .event_grounding import comparison_events, resolve_event_date
from .evidence_packet import CandidateSource, SourceSpan, _validate, digest


def grounded_result(query, sources, *, reference_date):
    clock = date.fromisoformat(reference_date[:10].replace('/', '-'))
    evidence=[]
    for source in sources:
        _validate(source)
        try: observed=date.fromisoformat(source.date[:10].replace('/', '-'))
        except ValueError: continue
        if observed>clock: continue
        for message in source.spans:
            if not message.text.startswith('user: '): continue
            for match in re.finditer(r'[^\n]+?(?:[.!?](?=\s|$)|$)',message.text[6:]):
                text=match.group()
                if re.search(r'\b(?:not|never|maybe|might|would|could|plan|if|about|approximately)\b|\?',text,re.I): continue
                start=message.char_start+6+match.start();end=start+len(text)
                span=SourceSpan(start,end,len(source.raw_text[:start].encode()),len(source.raw_text[:end].encode()),text,digest(text))
                _validate(CandidateSource(source.source_id,source.date,source.raw_text,source.raw_sha256,(span,)))
                evidence.append((source,span,observed))
    def pack(operation,value,unit,used):
        return dict(operation=operation,result=str(value),unit=unit,completeness='satisfied_fixed_operands',
                    evidence=[dict(source_id=s.source_id,source_sha256=s.raw_sha256,date=s.date,span=asdict(p)) for s,p,_ in used])
    events=comparison_events(query)
    unit=re.search(r'\b(days?|weeks?|months?|years?)\b',query,re.I)
    if events and unit:
        operands=[]
        for event,other in (events,events[::-1]):
            hits=[]
            for source,span,observed in evidence:
                value=resolve_event_date(span.text,datetime.combine(observed,datetime.min.time()),event=event,other_event=other)
                if value is not None and value<=clock:hits.append((value,(source,span,observed)))
            if len({v for v,_ in hits})!=1:return None
            operands.append(hits[0])
        (a,first),(b,second)=operands
        if a==b or first[1].sha256==second[1].sha256:return None
        a,b=sorted((a,b));u=unit.group().lower().rstrip('s')
        if u in {'month','year'}:
            # No arbitrary day-to-month conversion. Whole calendar intervals only.
            if a.day!=b.day:return None
            value=(b.year-a.year)*12+b.month-a.month
            if u=='year':
                if value%12:return None
                value//=12
        else:
            value=(b-a).days
            if u=='week':value=Decimal(value)/7
        return pack('date_difference',value,u,[first,second])
    names=[a or b for a,b in re.findall(r"(?<!\w)'([^']{3,})'(?!\w)|\"([^\"]{3,})\"",query)]
    if len(names)<2 or len(set(n.casefold() for n in names))!=len(names) or not re.search(r'\b(total|combined|altogether|sum)\b',query,re.I):return None
    operands=[]
    for name in names:
        hits=[]
        for item in evidence:
            text=item[1].text
            # Require one locally named operand and one explicit currency value.
            if name.casefold() not in text.casefold() or any(n.casefold() in text.casefold() for n in names if n!=name):continue
            values=re.findall(r'\$(\d[\d,]*(?:\.\d+)?)',text)
            if len(values)==1 and re.search(r'\b(cost|costs|spent|paid|raised)\b',text,re.I):hits.append((Decimal(values[0].replace(',','')),item))
        if len({v for v,_ in hits})!=1:return None
        operands.append(hits[0])
    return pack('sum',sum(v for v,_ in operands),'USD',[p for _,p in operands])
