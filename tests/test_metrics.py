from waggle.metrics import MetricsRegistry


def test_format_labels_escapes_special_characters():
    labels = (
        (
            "message",
            'quote" backslash\\ newline\n',
        ),
    )

    result = MetricsRegistry._format_labels(labels)

    assert result == '{message="quote\\" backslash\\\\ newline\\n"}'
