"""
waggle.server package init.
Proxies all attribute access and mutations to extracted submodules.
"""

from __future__ import annotations

import importlib
import json
import sys
import types
from typing import Any

_SUBMODULES = ["utils", "drive", "mcp", "routes", "cli"]


def main() -> None:
    """Console entry point with a lightweight path for standalone commands."""
    if len(sys.argv) >= 2 and sys.argv[1] == "telemetry":
        from waggle import __version__, telemetry

        command = sys.argv[2] if len(sys.argv) >= 3 else "status"
        if command == "enable":
            config = telemetry.enable()
            print("Anonymous telemetry enabled.")
            print(f"Installation ID: {config.installation_id}")
            return
        if command == "disable":
            config = telemetry.disable()
            print("Anonymous telemetry disabled.")
            print(f"Installation ID: {config.installation_id}")
            return
        if command == "show":
            payload = telemetry.preview_payload(
                "memory_retrieved",
                waggle_version=__version__,
                properties={
                    "client": "codex",
                    "transport": "stdio",
                    "backend": "sqlite",
                    "embedding_mode": "local",
                    "success": True,
                    "duration_bucket": "100-500ms",
                    "result_count_bucket": "1-5",
                    "query": "never sent",
                    "file_path": "never sent",
                },
            )
            print(json.dumps(payload, indent=2, sort_keys=True))
            return
        if command == "status":
            print(json.dumps(telemetry.status_payload(), indent=2, sort_keys=True))
            return
        print(f"Unsupported telemetry command: {command}", file=sys.stderr)
        raise SystemExit(1)

    from waggle.server.cli import main as cli_main

    cli_main()


class _ServerModuleProxy(types.ModuleType):
    def _get_submodule(self, name: str) -> types.ModuleType:
        loaded = self.__dict__.setdefault("_submodules_loaded", {})
        if name not in loaded:
            loaded[name] = importlib.import_module(f"waggle.server.{name}")
        return loaded[name]

    def __getattr__(self, name: str) -> Any:
        if name == "_submodules_loaded":
            raise AttributeError()

        if name in self.__dict__:
            return self.__dict__[name]

        if name in _SUBMODULES:
            return self._get_submodule(name)

        # Look up in submodules
        for sub in _SUBMODULES:
            try:
                sub_mod = self._get_submodule(sub)
                if hasattr(sub_mod, name):
                    return getattr(sub_mod, name)
            except Exception:
                pass

        raise AttributeError(f"module '{__name__}' has no attribute '{name}'")

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("__") or name == "_submodules_loaded":
            super().__setattr__(name, value)
            return

        super().__setattr__(name, value)

        # Propagate mutation to any submodules that define/import this attribute
        for sub in _SUBMODULES:
            try:
                sub_mod = self._get_submodule(sub)
                if hasattr(sub_mod, name):
                    setattr(sub_mod, name, value)
            except Exception:
                pass

    def __dir__(self) -> list[str]:
        # Collect all attributes from ourselves and all submodules
        attrs = set(super().__dir__())
        attrs.update(_SUBMODULES)
        for sub in _SUBMODULES:
            try:
                sub_mod = self._get_submodule(sub)
                attrs.update(dir(sub_mod))
            except Exception:
                pass
        return sorted(attrs)


# Update the module class to the proxy class
sys.modules[__name__].__class__ = _ServerModuleProxy
