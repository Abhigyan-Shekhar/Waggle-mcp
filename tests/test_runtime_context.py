from __future__ import annotations

import asyncio

import pytest

from waggle.runtime_context import RuntimeContext, get_runtime_context, runtime_context


def test_get_runtime_context_returns_empty_default_context() -> None:
    context = get_runtime_context()

    assert context == RuntimeContext()


def test_runtime_context_sets_values_and_restores_after_exit() -> None:
    with runtime_context(request_id="req-1", tenant_id="tenant-a", transport="stdio") as context:
        assert context.request_id == "req-1"
        assert context.tenant_id == "tenant-a"
        assert context.transport == "stdio"
        assert get_runtime_context() is context

    assert get_runtime_context() == RuntimeContext()


def test_nested_runtime_context_inherits_unspecified_fields_and_restores_parent() -> None:
    with runtime_context(request_id="outer", tenant_id="tenant-a", backend="sqlite"):
        with runtime_context(request_id="inner", tool_name="build_context") as inner:
            assert inner.request_id == "inner"
            assert inner.tenant_id == "tenant-a"
            assert inner.backend == "sqlite"
            assert inner.tool_name == "build_context"

        restored = get_runtime_context()
        assert restored.request_id == "outer"
        assert restored.tenant_id == "tenant-a"
        assert restored.backend == "sqlite"
        assert restored.tool_name == ""


def test_runtime_context_resets_when_body_raises() -> None:
    with pytest.raises(RuntimeError, match="boom"), runtime_context(request_id="req-error"):
        assert get_runtime_context().request_id == "req-error"
        raise RuntimeError("boom")

    assert get_runtime_context() == RuntimeContext()


@pytest.mark.asyncio
async def test_runtime_context_is_isolated_between_async_tasks() -> None:
    async def worker(tenant_id: str) -> tuple[str, str]:
        with runtime_context(tenant_id=tenant_id):
            before_sleep = get_runtime_context().tenant_id
            await asyncio.sleep(0)
            after_sleep = get_runtime_context().tenant_id
            return before_sleep, after_sleep

    with runtime_context(request_id="parent"):
        first, second = await asyncio.gather(worker("task-a"), worker("task-b"))
        assert get_runtime_context().request_id == "parent"

    assert first == ("task-a", "task-a")
    assert second == ("task-b", "task-b")
    assert get_runtime_context() == RuntimeContext()
