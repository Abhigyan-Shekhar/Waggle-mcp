from waggle.retrieval.recovery_operations import grounded_result
from test_recovery_delivery import source


def test_two_dates_and_requested_unit():
    sources=[source('a','The ceramics workshop was on January 1, 2024.','2024-03-01'), source('b','The robotics conference was on February 12, 2024.','2024-03-01')]
    r=grounded_result('How many days between the ceramics workshop and the robotics conference?',sources,reference_date='2024-03-01')
    assert r['result']=='42' and r['unit']=='day'
    assert grounded_result('How many months between the ceramics workshop and the robotics conference?',sources,reference_date='2024-03-01') is None


def test_ambiguous_date_abstains():
    sources=[source('a','The ceramics workshop was on January 1, 2024.','2024-03-01'),source('c','The ceramics workshop was on January 2, 2024.','2024-03-01'),source('b','The robotics conference was on February 12, 2024.','2024-03-01')]
    assert grounded_result('How many days between the ceramics workshop and the robotics conference?',sources,reference_date='2024-03-01') is None


def test_explicit_entity_set_sum_and_missing_operand():
    q='What was the combined cost of "Blue vase" and "Green lamp"?'
    ss=[source('a','Blue vase cost $25.'),source('b','Green lamp cost $50.')]
    assert grounded_result(q,ss,reference_date='2024-02-01')['result']=='75'
    assert grounded_result(q,ss[:1],reference_date='2024-02-01') is None


def test_no_exhaustiveness_or_ambiguous_binding():
    ss=[source('a','Blue vase and Green lamp cost $25 and $50.')]
    assert grounded_result('What was the combined cost of "Blue vase" and "Green lamp"?',ss,reference_date='2024-02-01') is None
    assert grounded_result('What was the total cost of all my purchases?',ss,reference_date='2024-02-01') is None
