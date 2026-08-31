"""Regression test for GH-704.

``mcp>=2.1`` removed ``request_ctx`` from ``mcp.server.lowlevel.server``.
``waggle.server.mcp`` guards the import with a fallback, but nothing
exercised that fallback path, so a future SDK change to the same import
could silently reintroduce the crash reported in
https://github.com/Abhigyan-Shekhar/Waggle-mcp/issues/704 (fresh
``pip install waggle-mcp`` + ``waggle --help`` raising ``ImportError``).
"""

from __future__ import annotations

import importlib
import subprocess
import sys

import mcp.server.lowlevel.server as mcp_lowlevel_server
import pytest


def _reload_waggle_server_mcp() -> object:
    sys.modules.pop("waggle.server.mcp", None)
    return importlib.import_module("waggle.server.mcp")


def test_waggle_server_mcp_survives_missing_request_ctx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(mcp_lowlevel_server, "request_ctx", raising=False)

    try:
        module = _reload_waggle_server_mcp()
        with pytest.raises(LookupError):
            module.request_ctx.get()
    finally:
        # Undo the patch before reloading so the module left in
        # sys.modules is rebuilt against the SDK's real state, instead of
        # leaking the fallback object into tests that run afterward.
        monkeypatch.undo()
        restored_module = _reload_waggle_server_mcp()

    if hasattr(mcp_lowlevel_server, "request_ctx"):
        assert restored_module.request_ctx is mcp_lowlevel_server.request_ctx
    else:
        # The installed SDK genuinely lacks request_ctx (the GH-704
        # scenario itself), so the fallback object is the correct,
        # freshly-restored state rather than a leaked stale instance.
        assert isinstance(
            restored_module.request_ctx, restored_module._MissingRequestContext
        )


def test_waggle_help_runs_without_request_ctx() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "waggle.server", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()
