from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


class BenchmarkCache:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.json"

    def get(self, key: str) -> Any | None:
        path = self._path(key)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def set(self, key: str, value: Any) -> None:
        self._path(key).write_text(json.dumps(value, indent=2), encoding="utf-8")

    def stats(self) -> dict[str, int]:
        return {"entries": len(list(self.root.glob("*.json")))}
