import pytest
from waggle.metrics import MetricsRegistry


def test_histogram_streaming_aggregation():
    """
    Tests that the histogram implementation uses a streaming aggregate design
    and does not store a list of all observed values.
    """
    registry = MetricsRegistry()

    # 1. Observe 10000 values
    expected_sum = 0
    for i in range(10000):
        value = float(i + 1)
        registry.observe("my_histogram", value, label="test")
        expected_sum += value

    # 2. Access internal state for assertions (for testing purposes)
    key = ("my_histogram", (("label", "test"),))
    series = registry._histograms.get(key)

    assert series is not None, "Histogram series was not created"

    # 3. Assert that _count equals 10000
    assert series["count"] == 10000

    # 4. Assert that _sum equals the expected total
    assert series["sum"] == expected_sum

    # 5. Assert that the stored series is a dict, not a list
    assert isinstance(series, dict)
    assert not isinstance(series, list)
