from waggle.metrics import MetricsRegistry


def test_histogram_streaming_aggregation():
    """
    Tests that the histogram uses streaming aggregation (count + sum)
    and does not retain a per-value list.
    """
    registry = MetricsRegistry()

    expected_sum = 0.0
    for i in range(10000):
        value = float(i + 1)
        registry.observe("my_histogram", value, label="test")
        expected_sum += value

    output = registry.render_prometheus()

    assert f'my_histogram_count{{label="test"}} 10000' in output
    assert f'my_histogram_sum{{label="test"}} {expected_sum}' in output

    key = ("my_histogram", (("label", "test"),))
    series = registry._histograms.get(key)
    assert isinstance(series, dict)

