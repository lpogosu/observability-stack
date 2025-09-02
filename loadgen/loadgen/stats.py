"""Client-side view of a run.

The server-side metrics are the source of truth for the alerts; these numbers
exist so the operator running the demo can tell "the alert did not fire" from
"the load generator never managed to send the load".
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field


@dataclass(slots=True)
class Stats:
    sent: int = 0
    dropped: int = 0
    by_class: dict[str, int] = field(default_factory=dict)
    _latencies: list[float] = field(default_factory=list, repr=False, compare=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def record(self, status: int, latency_seconds: float) -> None:
        bucket = f"{status // 100}xx"
        with self._lock:
            self.sent += 1
            self.by_class[bucket] = self.by_class.get(bucket, 0) + 1
            self._latencies.append(latency_seconds)

    def record_dropped(self) -> None:
        """A request slot that no worker was free to serve.

        Worth counting separately: if this number is large the generator, not the
        service, is the bottleneck and the whole run is meaningless.
        """
        with self._lock:
            self.dropped += 1

    @property
    def errors(self) -> int:
        with self._lock:
            return self.by_class.get("5xx", 0)

    @property
    def error_ratio(self) -> float:
        with self._lock:
            if self.sent == 0:
                return 0.0
            return self.by_class.get("5xx", 0) / self.sent

    def percentile(self, quantile: float) -> float:
        """Nearest-rank percentile of observed latency, in seconds."""
        if not 0.0 <= quantile <= 1.0:
            raise ValueError("quantile must be within [0, 1]")
        with self._lock:
            if not self._latencies:
                return 0.0
            ordered = sorted(self._latencies)
            rank = max(1, math.ceil(quantile * len(ordered)))
            return ordered[rank - 1]

    def summary(self) -> str:
        with self._lock:
            classes = " ".join(
                f"{name}={count}" for name, count in sorted(self.by_class.items())
            )
        return (
            f"sent={self.sent} dropped={self.dropped} {classes} "
            f"p50={self.percentile(0.5) * 1000:.0f}ms "
            f"p95={self.percentile(0.95) * 1000:.0f}ms "
            f"errors={self.error_ratio * 100:.1f}%"
        )
