"""Deliberate fault injection.

The alerts in this repository are only credible if something can actually break,
so the service ships with a switch for the two failure modes its SLOs are written
against: added latency and 5xx responses.

Every fault carries a TTL. A demo that leaves the service broken because someone
forgot to run the cleanup command is worse than no demo, and in a real system an
expiring fault is the difference between a chaos experiment and an outage.
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

MAX_TTL_SECONDS: Final = 3600
MAX_LATENCY_MS: Final = 10_000


@dataclass(frozen=True, slots=True)
class LatencyFault:
    delay_ms: int
    jitter_ms: int
    expires_at: float


@dataclass(frozen=True, slots=True)
class ErrorFault:
    ratio: float
    status_code: int
    expires_at: float


@dataclass(frozen=True, slots=True)
class FaultState:
    latency: LatencyFault | None
    errors: ErrorFault | None


class FaultInjector:
    """Thread-safe holder for the currently active faults.

    The clock and the RNG are injected so the tests can advance time and get
    deterministic draws instead of sleeping and hoping.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ) -> None:
        self._clock = clock
        self._rng = rng if rng is not None else random.Random()
        self._lock = threading.Lock()
        self._latency: LatencyFault | None = None
        self._errors: ErrorFault | None = None

    def inject_latency(self, *, delay_ms: int, jitter_ms: int, ttl_seconds: int) -> LatencyFault:
        if delay_ms < 0 or delay_ms > MAX_LATENCY_MS:
            raise ValueError(f"delay_ms must be within [0, {MAX_LATENCY_MS}]")
        if jitter_ms < 0 or jitter_ms > delay_ms:
            raise ValueError("jitter_ms must be within [0, delay_ms]")
        if ttl_seconds <= 0 or ttl_seconds > MAX_TTL_SECONDS:
            raise ValueError(f"ttl_seconds must be within (0, {MAX_TTL_SECONDS}]")

        fault = LatencyFault(
            delay_ms=delay_ms,
            jitter_ms=jitter_ms,
            expires_at=self._clock() + ttl_seconds,
        )
        with self._lock:
            self._latency = fault
        return fault

    def inject_errors(self, *, ratio: float, status_code: int, ttl_seconds: int) -> ErrorFault:
        if not 0.0 <= ratio <= 1.0:
            raise ValueError("ratio must be within [0, 1]")
        if not 500 <= status_code <= 599:
            raise ValueError("status_code must be a 5xx code")
        if ttl_seconds <= 0 or ttl_seconds > MAX_TTL_SECONDS:
            raise ValueError(f"ttl_seconds must be within (0, {MAX_TTL_SECONDS}]")

        fault = ErrorFault(
            ratio=ratio,
            status_code=status_code,
            expires_at=self._clock() + ttl_seconds,
        )
        with self._lock:
            self._errors = fault
        return fault

    def clear(self) -> None:
        with self._lock:
            self._latency = None
            self._errors = None

    def state(self) -> FaultState:
        now = self._clock()
        with self._lock:
            latency = self._latency if self._latency and self._latency.expires_at > now else None
            errors = self._errors if self._errors and self._errors.expires_at > now else None
            return FaultState(latency=latency, errors=errors)

    def next_delay_seconds(self) -> float:
        """Delay to apply to the next request, in seconds."""
        latency = self.state().latency
        if latency is None:
            return 0.0
        if latency.jitter_ms == 0:
            return latency.delay_ms / 1000.0
        low = latency.delay_ms - latency.jitter_ms
        high = latency.delay_ms + latency.jitter_ms
        return self._rng.uniform(low, high) / 1000.0

    def next_error_status(self) -> int | None:
        """Status code the next request should fail with, or None to let it through."""
        errors = self.state().errors
        if errors is None:
            return None
        # Strict `<` so ratio=0.0 never fails: random() is in [0, 1).
        if self._rng.random() < errors.ratio:
            return errors.status_code
        return None
