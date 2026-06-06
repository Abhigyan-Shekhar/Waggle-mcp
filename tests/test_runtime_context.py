import asyncio

import pytest

from waggle.runtime_context import RuntimeContext, get_runtime_context, runtime_context


def test_default_runtime_context_is_empty() -> None:
    assert get_runtime_context() == RuntimeContext()


def test_runtime_context_pushes_and_pops_single_value() -> None:
    with runtime_context(tenant_id="t1"):
        assert get_runtime_context().tenant_id == "t1"

    assert get_runtime_context() == RuntimeContext()


def test_nested_runtime_contexts_merge_unset_fields() -> None:
    with runtime_context(tenant_id="t1", transport="http"), runtime_context(tool_name="query_graph"):
        ctx = get_runtime_context()

        assert ctx.tenant_id == "t1"
        assert ctx.transport == "http"
        assert ctx.tool_name == "query_graph"

    assert get_runtime_context() == RuntimeContext()


def test_runtime_context_resets_after_exception() -> None:
    with pytest.raises(RuntimeError, match="boom"), runtime_context(tenant_id="outer"), runtime_context(tenant_id="inner"):
        assert get_runtime_context().tenant_id == "inner"
        raise RuntimeError("boom")

    assert get_runtime_context() == RuntimeContext()


def test_runtime_context_is_isolated_between_asyncio_tasks() -> None:
    async def read_tenant(tenant_id: str) -> str:
        with runtime_context(tenant_id=tenant_id):
            await asyncio.sleep(0)
            return get_runtime_context().tenant_id

    async def read_both_tenants() -> tuple[str, str]:
        return await asyncio.gather(
            read_tenant("tenant-a"),
            read_tenant("tenant-b"),
        )

    tenant_a, tenant_b = asyncio.run(read_both_tenants())

    assert tenant_a == "tenant-a"
    assert tenant_b == "tenant-b"
    assert get_runtime_context() == RuntimeContext()
