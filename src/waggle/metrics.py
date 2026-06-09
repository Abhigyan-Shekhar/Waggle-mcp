from __future__ import annotations

import threading
from collections import Counter, defaultdict
from typing import TypedDict


class HistogramSeries(TypedDict):
    count: int
    sum: float


class MetricsRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: Counter[tuple[str, tuple[tuple[str, str], ...]]] = Counter()
        self._gauges: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._histograms: defaultdict[tuple[str, tuple[tuple[str, str], ...]], HistogramSeries] = defaultdict(
            lambda: {"count": 0, "sum": 0.0}
        )

    def increment(self, name: str, value: int = 1, **labels: str) -> None:
        key = (name, tuple(sorted((k, str(v)) for k, v in labels.items())))
        with self._lock:
            self._counters[key] += value

    def observe(self, name: str, value: float, **labels: str) -> None:
        key = (name, tuple(sorted((k, str(v)) for k, v in labels.items())))
        with self._lock:
            series = self._histograms[key]
            series["count"] += 1
            series["sum"] += float(value)

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
            for (name, labels), series in sorted(self._histograms.items()):
                label_text = self._format_labels(labels)
                lines.append(f"{name}_count{label_text} {series['count']}")
                lines.append(f"{name}_sum{label_text} {series['sum']}")
        return "\n".join(lines) + ("\n" if lines else "")

    @staticmethod
    def _format_labels(labels: tuple[tuple[str, str], ...]) -> str:
        if not labels:
            return ""

        def escape(value: str) -> str:
            return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

        pairs = ",".join(f'{key}="{escape(value)}"' for key, value in labels)
        return "{" + pairs + "}"
