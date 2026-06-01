from waggle.metrics import MetricsRegistry


def test_empty_registry():
    registry = MetricsRegistry()

    assert registry.render_prometheus() == ""


def test_increment_accumulates():
    registry = MetricsRegistry()

    registry.increment("requests")
    registry.increment("requests")

    output = registry.render_prometheus()

    assert "requests 2" in output


def test_observe_tracks_count_and_sum():
    registry = MetricsRegistry()

    registry.observe("latency", 1.5)
    registry.observe("latency", 2.5)

    output = registry.render_prometheus()

    assert "latency_count 2" in output
    assert "latency_sum 4.0" in output


def test_set_gauge_overwrites():
    registry = MetricsRegistry()

    registry.set_gauge("memory", 100)
    registry.set_gauge("memory", 200)

    output = registry.render_prometheus()

    assert "memory 200.0" in output


def test_labels_render_correctly():
    registry = MetricsRegistry()

    registry.increment("requests", endpoint="/health")

    output = registry.render_prometheus()

    assert 'requests{endpoint="/health"} 1' in output


def test_multiple_metrics_sorted():
    registry = MetricsRegistry()

    registry.increment("z_metric")
    registry.increment("a_metric")

    output = registry.render_prometheus()

    assert output.index("a_metric") < output.index("z_metric")


def test_format_labels_escapes_special_characters():
    labels = (
        (
            "message",
            'quote" backslash\\ newline\n',
        ),
    )

    result = MetricsRegistry._format_labels(labels)

    assert result == '{message="quote\\" backslash\\\\ newline\\n"}'
