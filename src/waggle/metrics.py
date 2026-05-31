from __future__ import annotations

import threading
from collections import Counter, defaultdict


class MetricsRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: Counter[tuple[str, tuple[tuple[str, str], ...]]] = Counter()
        self._gauges: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._histograms: defaultdict[tuple[str, tuple[tuple[str, str], ...]], list[float]] = defaultdict(list)

    def increment(self, name: str, value: int = 1, **labels: str) -> None:
        key = (name, tuple(sorted((k, str(v)) for k, v in labels.items())))
        with self._lock:
            self._counters[key] += value

    def observe(self, name: str, value: float, **labels: str) -> None:
        key = (name, tuple(sorted((k, str(v)) for k, v in labels.items())))
        with self._lock:
            self._histograms[key].append(float(value))

    def set_gauge(self, name: str, value: float, **labels: str) -> None:
        key = (name, tuple(sorted((k, str(v)) for k, v in labels.items())))
        with self._lock:
            self._gauges[key] = float(value)

    def render_prometheus(self) -> str:
        lines: list[str] = []
        with self._lock:
            for (name, labels), value in sorted(self._counters.items()):
                label_text = self._format_labels(labels)
                lines.append(f"{name}{label_text} {value}")
            for (name, labels), value in sorted(self._gauges.items()):
                label_text = self._format_labels(labels)
                lines.append(f"{name}{label_text} {value}")
            for (name, labels), values in sorted(self._histograms.items()):
                label_text = self._format_labels(labels)
                count = len(values)
                total = sum(values)
                lines.append(f"{name}_count{label_text} {count}")
                lines.append(f"{name}_sum{label_text} {total}")
        return "\n".join(lines) + ("\n" if lines else "")

    @staticmethod
    def _format_labels(labels: tuple[tuple[str, str], ...]) -> str:
        if not labels:
            return ""

        # Keys are internal metric names and are assumed to be valid.
        escaped_pairs = []

        for key, value in labels:
            escaped_value = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
            escaped_pairs.append(f'{key}="{escaped_value}"')

        pairs = ",".join(escaped_pairs)
        return "{" + pairs + "}"
