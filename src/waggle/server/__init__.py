"""
waggle.server package init.
Proxies all attribute access and mutations to extracted submodules.
"""
from __future__ import annotations

import sys
import types
import importlib
from typing import Any

_SUBMODULES = ["utils", "drive", "mcp", "routes", "cli"]

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
