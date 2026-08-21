"""Small dependency-free runtime metrics registry.

The service intentionally keeps metrics local and process-scoped.  Deployment
systems can scrape ``/api/metrics``; no business payloads, source credentials,
or RDF rows are recorded in metric labels.
"""
from __future__ import annotations

import threading
from collections import defaultdict
from typing import Any


class RuntimeMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: defaultdict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._observations: defaultdict[tuple[str, tuple[tuple[str, str], ...]], dict[str, float]] = defaultdict(
            lambda: {"count": 0.0, "sum": 0.0, "max": 0.0}
        )

    @staticmethod
    def _key(name: str, labels: dict[str, Any] | None) -> tuple[str, tuple[tuple[str, str], ...]]:
        normalized = tuple(sorted((str(k), str(v)) for k, v in (labels or {}).items()))
        return name, normalized

    def inc(self, name: str, value: float = 1.0, labels: dict[str, Any] | None = None) -> None:
        with self._lock:
            self._counters[self._key(name, labels)] += float(value)

    def observe(self, name: str, value: float, labels: dict[str, Any] | None = None) -> None:
        key = self._key(name, labels)
        with self._lock:
            item = self._observations[key]
            item["count"] += 1.0
            item["sum"] += float(value)
            item["max"] = max(item["max"], float(value))

    @staticmethod
    def _labels(labels: tuple[tuple[str, str], ...]) -> str:
        if not labels:
            return ""
        escaped = []
        for key, value in labels:
            safe = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
            escaped.append(f'{key}="{safe}"')
        return "{" + ",".join(escaped) + "}"

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "counters": [
                    {"name": name, "labels": dict(labels), "value": value}
                    for (name, labels), value in self._counters.items()
                ],
                "observations": [
                    {"name": name, "labels": dict(labels), **values}
                    for (name, labels), values in self._observations.items()
                ],
            }

    def prometheus(self) -> str:
        lines = [
            "# HELP semantic_runtime_info Semantic semantic runtime information.",
            "# TYPE semantic_runtime_info gauge",
            'semantic_runtime_info{source_write="false",formal_publication="false"} 1',
        ]
        snapshot = self.snapshot()
        for item in snapshot["counters"]:
            name = item["name"]
            labels = tuple(sorted((str(k), str(v)) for k, v in item["labels"].items()))
            lines.append(f"# TYPE {name} counter")
            lines.append(f"{name}{self._labels(labels)} {item['value']}")
        for item in snapshot["observations"]:
            name = item["name"]
            labels = tuple(sorted((str(k), str(v)) for k, v in item["labels"].items()))
            suffix = self._labels(labels)
            lines.append(f"# TYPE {name} summary")
            lines.append(f"{name}_count{suffix} {item['count']}")
            lines.append(f"{name}_sum{suffix} {item['sum']}")
            lines.append(f"{name}_max{suffix} {item['max']}")
        return "\n".join(lines) + "\n"


runtime_metrics = RuntimeMetrics()
