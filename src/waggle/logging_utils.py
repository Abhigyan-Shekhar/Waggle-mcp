from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

from waggle.runtime_context import get_runtime_context


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        context = get_runtime_context()
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "tenant_id": context.tenant_id,
            "request_id": context.request_id,
            "transport": context.transport,
            "backend": context.backend,
            "api_key_id": context.api_key_id,
            "tool_name": context.tool_name,
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, sort_keys=True)


class PlainLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        context = get_runtime_context()
        timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        message = record.getMessage()
        line = f"{timestamp} {record.levelname:<5} {record.name}  {message}"
        extras = []
        if context.tenant_id:
            extras.append(f"tenant_id={context.tenant_id}")
        if context.request_id:
            extras.append(f"request_id={context.request_id}")
        if context.tool_name:
            extras.append(f"tool_name={context.tool_name}")
        if extras:
            line += "  " + " ".join(extras)
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def configure_logging(
    level: str = "INFO",
    *,
    stream: object | None = None,
    log_format: str = "json",
) -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    if log_format == "plain":
        handler.setFormatter(PlainLogFormatter())
    else:
        handler.setFormatter(JsonLogFormatter())
    root.handlers = [handler]
