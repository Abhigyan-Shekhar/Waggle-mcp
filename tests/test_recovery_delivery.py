from datetime import date
import pytest
from waggle.retrieval.evidence_packet import CandidateSource, SourceSpan, digest
from waggle.retrieval.recovery_delivery import deliver_recovery


def source(sid, text, when='2024-01-01'):
    raw='user: '+text
    return CandidateSource(sid, when, raw, digest(raw), (SourceSpan(0,len(raw),0,len(raw.encode()),raw,digest(raw)),))


def test_exact_provenance_diversity_determinism_and_unknown_closure():
    inputs=[source('a','I attended a workshop. Café attendance was confirmed.'), source('b','I attended another workshop.')]
    args=dict(reference_date='2024-02-01',count_tokens=len,max_extra_tokens=2000)
    a=deliver_recovery('unchanged core','workshop attendance',inputs,**args)
    b=deliver_recovery('unchanged core','workshop attendance',list(reversed(inputs)),**args)
    assert a==b
    assert a['text'].startswith('unchanged core')
    assert {p['source_id'] for p in a['selected']}=={'a','b'}
    assert a['completeness']=='UNKNOWN'
    for p in a['selected']:
        raw=next(s.raw_text for s in inputs if s.source_id==p['source_id'])
        assert raw.encode()[p['byte_start']:p['byte_end']]==p['text'].encode()


def test_future_evidence_excluded_and_reference_time_explicit():
    got=deliver_recovery('core','workshop',[source('future','workshop tomorrow','2025-01-01')],reference_date='2024-02-01',count_tokens=len)
    assert not got['selected']
    assert 'REFERENCE DATE: 2024-02-01' in got['text']


def test_oversize_skipped_without_clipping():
    got=deliver_recovery('core','workshop',[source('a','workshop '+ 'x'*4000),source('b','workshop attended')],reference_date='2024-02-01',count_tokens=len,max_extra_tokens=800)
    assert got['extra_tokens']<=800
    assert [p['source_id'] for p in got['selected']]==['b']


def test_duplicate_text_not_counted_twice():
    got=deliver_recovery('core','workshop',[source('a','workshop attended'),source('b','workshop attended')],reference_date='2024-02-01',count_tokens=len)
    assert len(got['selected'])==1
    assert got['completeness']=='UNKNOWN'


def test_conflicting_identity_rejected():
    with pytest.raises(ValueError):
        deliver_recovery('core','workshop',[source('a','workshop 1'),source('a','workshop 2')],reference_date='2024-02-01',count_tokens=len)
