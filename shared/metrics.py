"""In-process metrics — counters exportable as Prometheus text or JSON.

Deliberately dependency-free: a tiny thread-safe registry so agents can expose a
``/metrics`` endpoint without pulling `prometheus_client`. Agents run multiple
bus-handler threads, so mutations are locked.

Swap for `prometheus_client` / OpenTelemetry when a metrics backend is stood up;
until then this gives real, scrapeable counters (events emitted / dispatched /
dead-lettered, per agent + subject).
"""
from __future__ import annotations

import threading
from collections import defaultdict

# Metric names (Prometheus convention: <namespace>_<unit>_total for counters).
EVENTS_EMITTED = "cavi_events_emitted_total"
EVENTS_DISPATCHED = "cavi_events_dispatched_total"
DEADLETTERS = "cavi_deadletters_total"
# Redis Streams delivery (see agents/base/agent.py + ADR 0003).
EVENTS_ACKED = "cavi_events_acked_total"        # handle() succeeded -> XACK
EVENTS_RETRIED = "cavi_events_retried_total"    # handle() failed -> left pending for redelivery
EVENTS_POISONED = "cavi_events_poisoned_total"  # failed stream_max_deliveries times -> dead-lettered

_LabelKey = tuple[tuple[str, str], ...]


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, _LabelKey], float] = defaultdict(float)

    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            self._counters[key] += value

    def value(self, name: str, **labels: str) -> float:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            return self._counters.get(key, 0.0)

    def snapshot(self) -> dict[str, float]:
        """Flat {metric{labels}: value} map — handy for the JSON health body."""
        with self._lock:
            return {self._render(name, labels): v for (name, labels), v in self._counters.items()}

    def prometheus(self) -> str:
        """Prometheus text exposition (one line per series)."""
        with self._lock:
            items = sorted(self._counters.items())
        lines = [f"{self._render(name, labels)} {value}" for (name, labels), value in items]
        return "".join(f"{line}\n" for line in lines)

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()

    @staticmethod
    def _render(name: str, labels: _LabelKey) -> str:
        if not labels:
            return name
        inner = ",".join(f'{k}="{v}"' for k, v in labels)
        return f"{name}{{{inner}}}"


# Process-wide default registry the agents write to.
REGISTRY = Metrics()
