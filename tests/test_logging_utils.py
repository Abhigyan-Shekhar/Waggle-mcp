from __future__ import annotations

import json
import logging
import sys
from io import StringIO

import pytest

from waggle.config import AppConfig
from waggle.errors import ValidationFailure
from waggle.logging_utils import JsonLogFormatter, PlainLogFormatter, configure_logging
from waggle.runtime_context import runtime_context


class TestJsonLogFormatter:
    """Test the JsonLogFormatter class."""

    def test_basic_output_has_required_keys(self):
        """Test that formatter outputs JSON with all required keys."""
        formatter = JsonLogFormatter()
        record = logging.LogRecord(
            name="test_logger",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="Test message",
            args=(),
            exc_info=None,
        )

        output = formatter.format(record)
        data = json.loads(output)

        required_keys = [
            "timestamp",
            "level",
            "logger",
            "message",
            "tenant_id",
            "request_id",
            "transport",
            "backend",
            "api_key_id",
            "tool_name",
        ]

        for key in required_keys:
            assert key in data, f"Missing key: {key}"

    def test_message_formatting(self):
        """Test that message is correctly formatted."""
        formatter = JsonLogFormatter()
        record = logging.LogRecord(
            name="test_logger",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="Test message",
            args=(),
            exc_info=None,
        )

        output = formatter.format(record)
        data = json.loads(output)

        assert data["message"] == "Test message"

    def test_level_formatting(self):
        """Test that log level is correctly formatted."""
        formatter = JsonLogFormatter()

        for level, level_name in [
            (logging.INFO, "INFO"),
            (logging.WARNING, "WARNING"),
            (logging.ERROR, "ERROR"),
            (logging.DEBUG, "DEBUG"),
        ]:
            record = logging.LogRecord(
                name="test_logger",
                level=level,
                pathname="test.py",
                lineno=1,
                msg="Test",
                args=(),
                exc_info=None,
            )

            output = formatter.format(record)
            data = json.loads(output)

            assert data["level"] == level_name

    def test_logger_name(self):
        """Test that logger name is correctly set."""
        formatter = JsonLogFormatter()
        logger_names = ["app.module", "waggle.core", "test.logger"]

        for name in logger_names:
            record = logging.LogRecord(
                name=name,
                level=logging.INFO,
                pathname="test.py",
                lineno=1,
                msg="Test",
                args=(),
                exc_info=None,
            )

            output = formatter.format(record)
            data = json.loads(output)

            assert data["logger"] == name

    def test_timestamp_is_isoformat(self):
        """Test that timestamp is in ISO format."""
        formatter = JsonLogFormatter()
        record = logging.LogRecord(
            name="test_logger",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="Test",
            args=(),
            exc_info=None,
        )

        output = formatter.format(record)
        data = json.loads(output)

        # Should be able to parse as ISO format
        try:
            from datetime import datetime

            datetime.fromisoformat(data["timestamp"])
        except ValueError:
            pytest.fail("Timestamp is not in ISO format")

    def test_message_with_arguments(self):
        """Test that message formatting with arguments works."""
        formatter = JsonLogFormatter()
        record = logging.LogRecord(
            name="test_logger",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="Message with %s and %d",
            args=("argument", 42),
            exc_info=None,
        )

        output = formatter.format(record)
        data = json.loads(output)

        assert data["message"] == "Message with argument and 42"

    def test_exception_formatting(self):
        """Test that exception info is properly included."""
        formatter = JsonLogFormatter()

        try:
            raise ValueError("test error")
        except ValueError:
            import sys

            exc_info = sys.exc_info()

        record = logging.LogRecord(
            name="test_logger",
            level=logging.ERROR,
            pathname="test.py",
            lineno=1,
            msg="Error occurred",
            args=(),
            exc_info=exc_info,
        )

        output = formatter.format(record)
        data = json.loads(output)

        assert "exception" in data
        assert "ValueError" in data["exception"]
        assert "test error" in data["exception"]

    def test_no_exception_when_none(self):
        """Test that exception key is absent when there's no exception."""
        formatter = JsonLogFormatter()
        record = logging.LogRecord(
            name="test_logger",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="Normal message",
            args=(),
            exc_info=None,
        )

        output = formatter.format(record)
        data = json.loads(output)

        assert "exception" not in data

    def test_runtime_context_integration(self):
        """Test that runtime context values are included in output."""
        formatter = JsonLogFormatter()

        with runtime_context(
            request_id="req-123",
            tenant_id="tenant-456",
            transport="http",
            backend="openai",
            api_key_id="key-789",
            tool_name="search",
        ):
            record = logging.LogRecord(
                name="test_logger",
                level=logging.INFO,
                pathname="test.py",
                lineno=1,
                msg="Context test",
                args=(),
                exc_info=None,
            )

            output = formatter.format(record)
            data = json.loads(output)

            assert data["request_id"] == "req-123"
            assert data["tenant_id"] == "tenant-456"
            assert data["transport"] == "http"
            assert data["backend"] == "openai"
            assert data["api_key_id"] == "key-789"
            assert data["tool_name"] == "search"

    def test_empty_runtime_context(self):
        """Test that empty context defaults to empty strings."""
        formatter = JsonLogFormatter()
        record = logging.LogRecord(
            name="test_logger",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="Test",
            args=(),
            exc_info=None,
        )

        output = formatter.format(record)
        data = json.loads(output)

        assert data["tenant_id"] == ""
        assert data["request_id"] == ""
        assert data["transport"] == ""
        assert data["backend"] == ""
        assert data["api_key_id"] == ""
        assert data["tool_name"] == ""

    def test_output_is_valid_json(self):
        """Test that formatter output is always valid JSON."""
        formatter = JsonLogFormatter()

        test_cases = [
            ("simple message", ()),
            ('message with "quotes"', ()),
            ("message with newlines", ()),
            ("message with special chars", ()),
        ]

        for msg, args in test_cases:
            record = logging.LogRecord(
                name="test_logger",
                level=logging.INFO,
                pathname="test.py",
                lineno=1,
                msg=msg,
                args=args,
                exc_info=None,
            )

            output = formatter.format(record)
            try:
                json.loads(output)
            except json.JSONDecodeError:
                pytest.fail(f"Output is not valid JSON: {output}")

    def test_keys_are_sorted(self):
        """Test that JSON keys are sorted (for consistency)."""
        formatter = JsonLogFormatter()
        record = logging.LogRecord(
            name="test_logger",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="Test",
            args=(),
            exc_info=None,
        )

        output = formatter.format(record)
        data = json.loads(output)

        # Check that the output string has sorted keys
        # (json.dumps with sort_keys=True should produce consistent ordering)
        output_again = json.dumps(data, sort_keys=True)
        assert output == output_again


class TestConfigureLogging:
    """Test the configure_logging function."""

    def setup_method(self):
        """Reset logging before each test."""
        root_logger = logging.getLogger()
        self.original_handlers = root_logger.handlers[:]
        self.original_level = root_logger.level

    def teardown_method(self):
        """Restore logging after each test."""
        root_logger = logging.getLogger()
        root_logger.handlers = self.original_handlers
        root_logger.setLevel(self.original_level)

    def test_configure_logging_sets_json_formatter(self):
        """Test that configure_logging sets up JsonLogFormatter."""
        configure_logging()

        root_logger = logging.getLogger()
        has_json_formatter = any(
            isinstance(h.formatter, JsonLogFormatter) for h in root_logger.handlers if h.formatter is not None
        )

        assert has_json_formatter, "No handler with JsonLogFormatter found"

    def test_configure_logging_sets_level(self):
        """Test that configure_logging sets the correct log level."""
        for level_str in ["DEBUG", "INFO", "WARNING", "ERROR"]:
            root_logger = logging.getLogger()
            root_logger.handlers = []

            configure_logging(level=level_str)

            assert root_logger.level == getattr(logging, level_str)

    def test_configure_logging_default_level(self):
        """Test that default level is INFO."""
        root_logger = logging.getLogger()
        root_logger.handlers = []

        configure_logging()

        assert root_logger.level == logging.INFO

    def test_configure_logging_case_insensitive(self):
        """Test that level string is case insensitive."""
        for level_str in ["info", "Info", "INFO", "InFo"]:
            root_logger = logging.getLogger()
            root_logger.handlers = []

            configure_logging(level=level_str)

            assert root_logger.level == logging.INFO

    def test_configure_logging_custom_stream(self):
        """Test that configure_logging accepts custom stream."""
        custom_stream = StringIO()
        configure_logging(stream=custom_stream)

        root_logger = logging.getLogger()
        stream_handler = None
        for handler in root_logger.handlers:
            if isinstance(handler, logging.StreamHandler):
                stream_handler = handler
                break

        assert stream_handler is not None
        assert stream_handler.stream is custom_stream

    def test_configure_logging_default_stream(self):
        """Test that default stream is stdout."""
        configure_logging()

        root_logger = logging.getLogger()
        stream_handler = None
        for handler in root_logger.handlers:
            if isinstance(handler, logging.StreamHandler):
                stream_handler = handler
                break

        assert stream_handler is not None
        assert stream_handler.stream is sys.stdout

    def test_logging_with_configured_logger(self):
        """Test that logging actually produces JSON output."""
        stream = StringIO()
        configure_logging(stream=stream)

        logger = logging.getLogger("test.logger")
        logger.info("Test message")

        output = stream.getvalue().strip()
        data = json.loads(output)

        assert data["message"] == "Test message"
        assert data["level"] == "INFO"
        assert data["logger"] == "test.logger"

    def test_logging_with_runtime_context(self):
        """Test that runtime context is captured when logging."""
        stream = StringIO()
        configure_logging(stream=stream)

        with runtime_context(request_id="req-123", tenant_id="tenant-456"):
            logger = logging.getLogger("test.logger")
            logger.info("Test message")

        output = stream.getvalue().strip()
        data = json.loads(output)

        assert data["request_id"] == "req-123"
        assert data["tenant_id"] == "tenant-456"

    def test_logging_exception_with_configured_logger(self):
        """Test that exceptions are logged correctly."""
        stream = StringIO()
        configure_logging(stream=stream)

        logger = logging.getLogger("test.logger")

        try:
            raise ValueError("test error")
        except ValueError:
            logger.exception("An error occurred")

        output = stream.getvalue().strip()
        data = json.loads(output)

        assert data["message"] == "An error occurred"
        assert "exception" in data
        assert "ValueError" in data["exception"]

    def test_configure_logging_replaces_handlers(self):
        """Test that configure_logging replaces existing handlers."""
        root_logger = logging.getLogger()
        root_logger.handlers = [logging.StreamHandler()]

        configure_logging()

        # Should have exactly 1 handler (the new StreamHandler)
        assert len(root_logger.handlers) == 1
        assert isinstance(root_logger.handlers[0], logging.StreamHandler)


class TestIntegration:
    """Integration tests for logging utilities."""

    def setup_method(self):
        """Reset logging before each test."""
        root_logger = logging.getLogger()
        self.original_handlers = root_logger.handlers[:]
        self.original_level = root_logger.level

    def teardown_method(self):
        """Restore logging after each test."""
        root_logger = logging.getLogger()
        root_logger.handlers = self.original_handlers
        root_logger.setLevel(self.original_level)

    def test_multiple_loggers(self):
        """Test that multiple loggers work correctly."""
        stream = StringIO()
        configure_logging(stream=stream)

        logger1 = logging.getLogger("app.module1")
        logger2 = logging.getLogger("app.module2")

        logger1.info("Message from module 1")
        logger2.warning("Message from module 2")

        lines = stream.getvalue().strip().split("\n")
        assert len(lines) == 2

        data1 = json.loads(lines[0])
        data2 = json.loads(lines[1])

        assert data1["logger"] == "app.module1"
        assert data1["message"] == "Message from module 1"
        assert data1["level"] == "INFO"

        assert data2["logger"] == "app.module2"
        assert data2["message"] == "Message from module 2"
        assert data2["level"] == "WARNING"

    def test_nested_runtime_contexts(self):
        """Test that nested runtime contexts work correctly."""
        stream = StringIO()
        configure_logging(stream=stream)

        logger = logging.getLogger("test.logger")

        with runtime_context(request_id="req-1"):
            logger.info("Message 1")

            with runtime_context(request_id="req-2"):
                logger.info("Message 2")

            logger.info("Message 3")

        lines = stream.getvalue().strip().split("\n")
        data1 = json.loads(lines[0])
        data2 = json.loads(lines[1])
        data3 = json.loads(lines[2])

        assert data1["request_id"] == "req-1"
        assert data2["request_id"] == "req-2"
        assert data3["request_id"] == "req-1"

    def test_log_all_levels(self):
        """Test logging at all levels."""
        stream = StringIO()
        configure_logging(level="DEBUG", stream=stream)

        logger = logging.getLogger("test.logger")

        logger.debug("Debug message")
        logger.info("Info message")
        logger.warning("Warning message")
        logger.error("Error message")

        lines = stream.getvalue().strip().split("\n")
        assert len(lines) == 4

        levels = ["DEBUG", "INFO", "WARNING", "ERROR"]
        for i, level in enumerate(levels):
            data = json.loads(lines[i])
            assert data["level"] == level


class TestPlainLogFormatter:
    """Tests for PlainLogFormatter."""

    def test_basic_output_format(self):
        """Test that plain formatter produces expected format."""
        formatter = PlainLogFormatter()
        record = logging.LogRecord(
            name="waggle.graph",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Stored node abc123",
            args=(),
            exc_info=None,
        )
        output = formatter.format(record)
        parts = output.split()
        assert parts[1] == "INFO"
        assert parts[2] == "waggle.graph"
        assert "Stored node abc123" in output

    def test_timestamp_format(self):
        """Test that timestamp is in ISO format without microseconds."""
        formatter = PlainLogFormatter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="test message",
            args=(),
            exc_info=None,
        )
        output = formatter.format(record)
        timestamp = output.split()[0]
        assert "T" in timestamp
        assert len(timestamp) == 19  # YYYY-MM-DDTHH:MM:SS

    def test_extras_emitted_when_nonempty(self):
        """Test that non-empty context fields are included."""
        formatter = PlainLogFormatter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="test",
            args=(),
            exc_info=None,
        )
        with runtime_context(tenant_id="my-tenant"):
            output = formatter.format(record)
        assert "tenant_id=my-tenant" in output

    def test_extras_omitted_when_empty(self):
        """Test that empty context fields are not included."""
        formatter = PlainLogFormatter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="test",
            args=(),
            exc_info=None,
        )
        output = formatter.format(record)
        assert "tenant_id" not in output
        assert "request_id" not in output
        assert "tool_name" not in output

    def test_exception_on_second_line(self):
        """Test that exceptions appear on a second line."""
        formatter = PlainLogFormatter()
        try:
            raise ValueError("test error")
        except ValueError:
            exc_info = sys.exc_info()
        record = logging.LogRecord(
            name="test",
            level=logging.ERROR,
            pathname="",
            lineno=0,
            msg="something failed",
            args=(),
            exc_info=exc_info,
        )
        output = formatter.format(record)
        lines = output.split("\n")
        assert len(lines) >= 2
        assert "ValueError" in output


class TestConfigureLoggingFormat:
    """Tests for log_format parameter in configure_logging."""

    def setup_method(self):
        """Reset logging before each test."""
        root_logger = logging.getLogger()
        self.original_handlers = root_logger.handlers[:]
        self.original_level = root_logger.level

    def teardown_method(self):
        """Restore logging after each test."""
        root_logger = logging.getLogger()
        root_logger.handlers = self.original_handlers
        root_logger.setLevel(self.original_level)

    def test_plain_format_installs_plain_formatter(self):
        """Test that log_format=plain installs PlainLogFormatter."""
        stream = StringIO()
        configure_logging(stream=stream, log_format="plain")
        root = logging.getLogger()
        assert isinstance(root.handlers[0].formatter, PlainLogFormatter)

    def test_json_format_installs_json_formatter(self):
        """Test that log_format=json installs JsonLogFormatter."""
        stream = StringIO()
        configure_logging(stream=stream, log_format="json")
        root = logging.getLogger()
        assert isinstance(root.handlers[0].formatter, JsonLogFormatter)

    def test_default_format_installs_json_formatter(self):
        """Test that default log_format installs JsonLogFormatter."""
        stream = StringIO()
        configure_logging(stream=stream)
        root = logging.getLogger()
        assert isinstance(root.handlers[0].formatter, JsonLogFormatter)

class TestAppConfigLogFormat:
    """Tests for log_format validation in AppConfig."""

    def test_valid_json_format(self):
        """Test that log_format=json passes validation."""
        config = AppConfig(
            backend="sqlite",
            transport="stdio",
            model_name="all-MiniLM-L6-v2",
            db_path="~/.waggle/waggle.db",
            default_tenant_id="local-default",
            http_host="0.0.0.0",
            http_port=8080,
            log_level="INFO",
            log_format="json",
            rate_limit_rpm=120,
            write_rate_limit_rpm=60,
            max_concurrent_requests=8,
            max_payload_bytes=1048576,
            request_timeout_seconds=30,
            export_dir=None,
            neo4j_uri="",
            neo4j_username="",
            neo4j_password="",
            neo4j_database="",
        )
        config.validate()  # should not raise

    def test_valid_plain_format(self):
        """Test that log_format=plain passes validation."""
        config = AppConfig(
            backend="sqlite",
            transport="stdio",
            model_name="all-MiniLM-L6-v2",
            db_path="~/.waggle/waggle.db",
            default_tenant_id="local-default",
            http_host="0.0.0.0",
            http_port=8080,
            log_level="INFO",
            log_format="plain",
            rate_limit_rpm=120,
            write_rate_limit_rpm=60,
            max_concurrent_requests=8,
            max_payload_bytes=1048576,
            request_timeout_seconds=30,
            export_dir=None,
            neo4j_uri="",
            neo4j_username="",
            neo4j_password="",
            neo4j_database="",
        )
        config.validate()  # should not raise

    def test_invalid_format_raises(self):
        """Test that invalid log_format raises ValidationFailure."""
        config = AppConfig(
            backend="sqlite",
            transport="stdio",
            model_name="all-MiniLM-L6-v2",
            db_path="~/.waggle/waggle.db",
            default_tenant_id="local-default",
            http_host="0.0.0.0",
            http_port=8080,
            log_level="INFO",
            log_format="bogus",
            rate_limit_rpm=120,
            write_rate_limit_rpm=60,
            max_concurrent_requests=8,
            max_payload_bytes=1048576,
            request_timeout_seconds=30,
            export_dir=None,
            neo4j_uri="",
            neo4j_username="",
            neo4j_password="",
            neo4j_database="",
        )
        with pytest.raises(ValidationFailure, match="WAGGLE_LOG_FORMAT"):
            config.validate()
