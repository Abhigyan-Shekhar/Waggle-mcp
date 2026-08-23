from __future__ import annotations

import json
import os
import platform
import threading
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

CONFIG_PATH = Path.home() / ".waggle" / "telemetry.json"
QUEUE_PATH = Path.home() / ".waggle" / "telemetry-queue.jsonl"
ENDPOINT = "https://analytics.waggle.dev/v1/events"
REQUEST_TIMEOUT_SECONDS = 0.75
MAX_QUEUE_EVENTS = 100
MAX_QUEUE_AGE = timedelta(days=7)
MAX_BATCH_SIZE = 20
QUEUE_LOCK = threading.Lock()

ALLOWED_EVENTS = {
    "setup_completed",
    "server_started",
    "memory_stored",
    "memory_retrieved",
    "context_primed",
    "demo_completed",
    "export_completed",
    "operation_failed",
}

ALLOWED_PROPERTIES = {
    "waggle_version",
    "python_version",
    "os",
    "architecture",
    "client",
    "transport",
    "backend",
    "embedding_mode",
    "success",
    "duration_bucket",
    "result_count_bucket",
    "error_category",
    "doctor_ran",
}

FORBIDDEN_PROPERTY_NAMES = {
    "query",
    "prompt",
    "memory_text",
    "node_content",
    "content",
    "file_path",
    "path",
    "repository_name",
    "repo_name",
    "project",
    "project_name",
    "tenant",
    "tenant_name",
    "session_transcript",
    "transcript",
    "exception_message",
    "stack",
    "stack_trace",
    "traceback",
}


@dataclass(frozen=True)
class TelemetryConfig:
    enabled: bool
    installation_id: str
    overridden_by_env: bool = False


def load_config() -> TelemetryConfig:
    env_value = _normalized_env_value()
    installation_id = str(uuid.uuid4())
    enabled = False

    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            data = {}
        if isinstance(data, dict):
            enabled = bool(data.get("enabled", False))
            raw_installation_id = data.get("installation_id")
            if isinstance(raw_installation_id, str) and raw_installation_id.strip():
                installation_id = raw_installation_id.strip()

    if env_value == "0":
        return TelemetryConfig(enabled=False, installation_id=installation_id, overridden_by_env=True)
    if env_value == "1":
        return TelemetryConfig(enabled=True, installation_id=installation_id, overridden_by_env=True)
    return TelemetryConfig(enabled=enabled, installation_id=installation_id)


def save_config(config: TelemetryConfig) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "enabled": bool(config.enabled),
        "installation_id": config.installation_id or str(uuid.uuid4()),
    }
    CONFIG_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def enable() -> TelemetryConfig:
    current = load_config()
    config = TelemetryConfig(enabled=True, installation_id=current.installation_id)
    save_config(config)
    return config


def disable() -> TelemetryConfig:
    current = load_config()
    config = TelemetryConfig(enabled=False, installation_id=current.installation_id)
    save_config(config)
    return config


def status_payload() -> dict[str, Any]:
    config = load_config()
    return {
        "enabled": config.enabled,
        "installation_id": config.installation_id,
        "config_path": str(CONFIG_PATH),
        "queue_path": str(QUEUE_PATH),
        "queue_depth": len(_read_queue()),
        "endpoint": endpoint_url(),
        "overridden_by_env": config.overridden_by_env,
    }


def preview_payload(
    event: str = "memory_retrieved",
    *,
    waggle_version: str,
    properties: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = load_config()
    return _build_payload(event, config.installation_id, waggle_version=waggle_version, properties=properties)


def capture(
    event: str,
    *,
    waggle_version: str,
    properties: dict[str, Any] | None = None,
) -> None:
    config = load_config()
    if not config.enabled:
        return
    if not CONFIG_PATH.exists():
        save_config(TelemetryConfig(enabled=False, installation_id=config.installation_id))

    try:
        payload = _build_payload(event, config.installation_id, waggle_version=waggle_version, properties=properties)
        _append_event(payload)
    except Exception:
        return

    thread = threading.Thread(target=flush, daemon=True)
    thread.start()


def capture_tool_event(
    tool_name: str,
    *,
    structured: dict[str, Any] | list[Any],
    is_error: bool,
    waggle_version: str,
    transport: str,
    backend: str,
    embedding_mode: str,
) -> None:
    event = _event_for_tool(tool_name, structured=structured, is_error=is_error)
    if event is None:
        return
    properties: dict[str, Any] = {
        "success": not is_error,
        "transport": transport,
        "backend": backend,
        "embedding_mode": embedding_mode,
    }
    count = _result_count_for_tool(tool_name, structured)
    if count is not None:
        properties["result_count_bucket"] = bucket_count(count)
    if is_error:
        properties["error_category"] = _safe_error_category(structured)
    capture(event, waggle_version=waggle_version, properties=properties)


def flush() -> int:
    config = load_config()
    if not config.enabled or not QUEUE_PATH.exists():
        return 0

    with QUEUE_LOCK:
        try:
            queued = _read_queue()
        except Exception:
            return 0

        if not queued:
            _replace_queue([])
            return 0

        batch = queued[:MAX_BATCH_SIZE]
        delivered = 0
        remaining = queued
        try:
            _send_batch(batch)
            delivered = len(batch)
            remaining = queued[delivered:]
        except Exception:
            return 0

        try:
            _replace_queue(remaining)
        except Exception:
            return delivered
        return delivered


def endpoint_url() -> str:
    return os.getenv("WAGGLE_TELEMETRY_ENDPOINT", "").strip() or ENDPOINT


def smoke_check(*, waggle_version: str) -> dict[str, Any]:
    endpoint = endpoint_url()
    payload = _build_payload(
        "server_started",
        load_config().installation_id,
        waggle_version=waggle_version,
        properties={"success": True, "transport": "smoke", "backend": "unknown", "embedding_mode": "unknown"},
    )
    try:
        _send_batch([payload], endpoint=endpoint)
    except Exception as exc:
        return {
            "ok": False,
            "endpoint": endpoint,
            "error_type": type(exc).__name__,
            "message": str(exc)[:240],
        }
    return {"ok": True, "endpoint": endpoint}


def bucket_count(count: int) -> str:
    if count <= 0:
        return "0"
    if count <= 5:
        return "1-5"
    if count <= 20:
        return "6-20"
    if count <= 100:
        return "21-100"
    return "101+"


def embedding_mode(model_name: str, embedding_backend: str = "") -> str:
    if model_name == "deterministic":
        return "deterministic"
    return "local"


def _event_for_tool(tool_name: str, *, structured: dict[str, Any] | list[Any], is_error: bool) -> str | None:
    if is_error:
        return "operation_failed"

    normalized = tool_name.strip()
    if normalized in {
        "store_node",
        "store_edge",
        "observe_conversation",
        "decompose_and_store",
        "import_graph_backup",
        "import_abhi",
        "pull",
    }:
        return "memory_stored"
    if normalized in {"query_graph", "get_related", "get_node_history"}:
        return "memory_retrieved" if (_result_count_for_tool(normalized, structured) or 0) > 0 else None
    if normalized in {"prime_context", "build_context"}:
        return "context_primed" if (_result_count_for_tool(normalized, structured) or 0) > 0 else None
    if normalized in {"export_abhi", "commit", "export_graph_backup", "export_context_bundle"}:
        return "export_completed"
    return None


def _result_count_for_tool(tool_name: str, structured: dict[str, Any] | list[Any]) -> int | None:
    if isinstance(structured, list):
        return len(structured)
    if not isinstance(structured, dict):
        return None

    for key in (
        "created_count",
        "nodes_extracted",
        "nodes_created",
        "nodes_updated",
        "node_count",
        "total_nodes",
        "total_nodes_in_graph",
    ):
        value = structured.get(key)
        if isinstance(value, int):
            return value

    total = 0
    found = False
    for key in ("nodes", "related_nodes", "replay_hits", "fusion_hits", "hybrid_hits", "stored_nodes"):
        value = structured.get(key)
        if isinstance(value, list):
            total += len(value)
            found = True
    if found:
        return total

    if tool_name in {"store_node", "store_edge"} and structured:
        return 1
    return None


def _safe_error_category(structured: dict[str, Any] | list[Any]) -> str:
    if not isinstance(structured, dict):
        return "unknown"
    value = structured.get("error_code") or structured.get("error_type") or "unknown"
    if not isinstance(value, str):
        return "unknown"
    normalized = value.strip().lower().replace(" ", "_")
    return normalized[:64] or "unknown"


def _build_payload(
    event: str,
    installation_id: str,
    *,
    waggle_version: str,
    properties: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if event not in ALLOWED_EVENTS:
        raise ValueError(f"Unsupported telemetry event: {event}")

    safe_properties = {
        "waggle_version": waggle_version,
        "python_version": platform.python_version(),
        "os": platform.system().lower(),
        "architecture": platform.machine().lower(),
    }
    safe_properties.update(_sanitize_properties(properties or {}))

    return {
        "event": event,
        "installation_id": installation_id,
        "timestamp": datetime.now(UTC).isoformat(),
        "properties": safe_properties,
    }


def _sanitize_properties(properties: dict[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for key, value in properties.items():
        if key in FORBIDDEN_PROPERTY_NAMES or key not in ALLOWED_PROPERTIES:
            continue
        if isinstance(value, bool | int | float | str):
            sanitized[key] = value
    return sanitized


def _append_event(payload: dict[str, Any]) -> None:
    with QUEUE_LOCK:
        queued = _read_queue()
        queued.append(payload)
        queued = queued[-MAX_QUEUE_EVENTS:]
        _replace_queue(queued)


def _read_queue() -> list[dict[str, Any]]:
    if not QUEUE_PATH.exists():
        return []
    cutoff = datetime.now(UTC) - MAX_QUEUE_AGE
    events: list[dict[str, Any]] = []
    for line in QUEUE_PATH.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        timestamp = event.get("timestamp")
        if isinstance(timestamp, str):
            try:
                if datetime.fromisoformat(timestamp) < cutoff:
                    continue
            except ValueError:
                continue
        events.append(event)
    return events[-MAX_QUEUE_EVENTS:]


def _replace_queue(events: list[dict[str, Any]]) -> None:
    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not events:
        with suppress(FileNotFoundError):
            QUEUE_PATH.unlink()
        return
    text = "".join(json.dumps(event, sort_keys=True) + "\n" for event in events[-MAX_QUEUE_EVENTS:])
    QUEUE_PATH.write_text(text, encoding="utf-8")


def _send_batch(batch: list[dict[str, Any]], *, endpoint: str | None = None) -> None:
    request = Request(
        endpoint or endpoint_url(),
        data=json.dumps({"events": batch}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS):
        pass


def _normalized_env_value() -> str | None:
    value = os.getenv("WAGGLE_TELEMETRY")
    if value is None:
        return None
    value = value.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return "1"
    if value in {"0", "false", "no", "off"}:
        return "0"
    return None
