import pytest

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

    # Find and parse the sum from the output
    sum_line = next(line for line in output.split("\n") if 'my_histogram_sum{label="test"}' in line)
    observed_sum = float(sum_line.split(" ")[1])

    assert 'my_histogram_count{label="test"} 10000' in output
    assert observed_sum == pytest.approx(expected_sum)


def test_format_labels_escapes_special_characters():
    labels = (
        (
            "message",
            'quote" backslash\\ newline\n',
        ),
    )
    result = MetricsRegistry._format_labels(labels)

    result = MetricsRegistry._format_labels(labels)

    assert result == '{message="quote\\" backslash\\\\ newline\\n"}'
