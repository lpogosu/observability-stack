"""Scenario execution.

One dispatcher thread opens a request slot every 1/rps seconds and hands it to a
worker pool. Pacing in the dispatcher rather than in the workers means the offered
rate stays constant even when the service slows down - which is exactly what has
to happen during the latency scenario, otherwise the generator would back off and
the incident would never build up.
"""

from __future__ import annotations

import json
import logging
import queue
import random
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Final, Protocol

from loadgen.scenarios import Fault, Phase, Scenario
from loadgen.stats import Stats

logger: Final = logging.getLogger("loadgen")

# Status used when the request never produced an HTTP response at all. It sits
# outside the real 1xx-5xx range on purpose so it cannot be mistaken for one.
TRANSPORT_FAILURE_STATUS: Final = 599

_QUEUE_SLACK: Final = 4


class _Shutdown:
    __slots__ = ()


_SHUTDOWN: Final = _Shutdown()


@dataclass(frozen=True, slots=True)
class RequestSpec:
    method: str
    path: str
    body: dict[str, int] | None = None


# Weights approximate a read-heavy API: browsing dominates, ordering is rare.
_REQUEST_MIX: Final[tuple[tuple[RequestSpec, int], ...]] = (
    (RequestSpec("GET", "/api/items"), 6),
    (RequestSpec("GET", "/api/items/1"), 1),
    (RequestSpec("GET", "/api/items/2"), 1),
    (RequestSpec("GET", "/api/items/3"), 1),
    (RequestSpec("POST", "/api/orders", {"item_id": 2, "quantity": 1}), 1),
)


def choose_request(rng: random.Random) -> RequestSpec:
    specs = [spec for spec, _ in _REQUEST_MIX]
    weights = [weight for _, weight in _REQUEST_MIX]
    return rng.choices(specs, weights=weights, k=1)[0]


class Transport(Protocol):
    def send(self, method: str, url: str, body: bytes | None) -> int:
        """Perform the request and return its HTTP status code."""


class UrllibTransport:
    """Minimal HTTP client built on the standard library.

    A load generator with its own dependency tree stops working the moment that
    tree drifts. urllib is enough for this shape of traffic and keeps the image
    at a bare python:3.11-slim with nothing installed into it.
    """

    def __init__(self, timeout_seconds: float = 10.0) -> None:
        self._timeout = timeout_seconds

    def send(self, method: str, url: str, body: bytes | None) -> int:
        request = urllib.request.Request(url, data=body, method=method)
        if body is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                status: int = response.status
                response.read()
                return status
        except urllib.error.HTTPError as exc:
            # An injected 503 arrives here, not in the success path. Draining the
            # body keeps the connection from being torn down mid-response.
            exc.read()
            return exc.code
        except (urllib.error.URLError, TimeoutError, OSError):
            return TRANSPORT_FAILURE_STATUS


class LoadGenerator:
    def __init__(
        self,
        *,
        target: str,
        scenario: Scenario,
        transport: Transport,
        rng: random.Random | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        workers: int | None = None,
    ) -> None:
        self._target = target.rstrip("/")
        self._scenario = scenario
        self._transport = transport
        self._rng = rng if rng is not None else random.Random()
        self._clock = clock
        self._sleep = sleep
        # Enough workers to keep the offered rate up while every request is
        # parked in the injected 700 ms sleep, plus headroom.
        self._workers = workers if workers is not None else max(4, int(scenario.peak_rps * 2))
        self._stats = Stats()
        self._stop = threading.Event()

    @property
    def stats(self) -> Stats:
        return self._stats

    def stop(self) -> None:
        self._stop.set()

    def arm(self, fault: Fault) -> None:
        body = json.dumps(fault.payload).encode() if fault.kind != "clear" else None
        status = self._transport.send(fault.method, f"{self._target}{fault.path}", body)
        if status >= 400:
            raise RuntimeError(f"failed to arm fault {fault.kind}: HTTP {status}")
        logger.info("armed fault kind=%s payload=%s", fault.kind, fault.payload)

    def run(self) -> Stats:
        slots: queue.Queue[object] = queue.Queue(maxsize=self._workers * _QUEUE_SLACK)
        started = self._clock()
        # Absolute slot deadlines rather than a fixed sleep per iteration: the
        # latter drifts by the cost of every dispatch and silently under-delivers.
        next_slot = started
        current: Phase | None = None

        with ThreadPoolExecutor(max_workers=self._workers) as pool:
            for _ in range(self._workers):
                pool.submit(self._worker, slots)

            while not self._stop.is_set():
                now = self._clock()
                phase = self._scenario.phase_at(now - started)
                if phase is None:
                    break
                if phase is not current:
                    logger.info(
                        "phase=%s rps=%.1f elapsed=%ds of %ds",
                        phase.name,
                        phase.rps,
                        int(now - started),
                        self._scenario.duration_seconds,
                    )
                    if phase.fault is not None:
                        self.arm(phase.fault)
                    current = phase

                try:
                    slots.put_nowait(None)
                except queue.Full:
                    self._stats.record_dropped()

                next_slot = max(next_slot + 1.0 / phase.rps, now)
                remaining = next_slot - self._clock()
                if remaining > 0:
                    self._sleep(remaining)

            for _ in range(self._workers):
                slots.put(_SHUTDOWN)

        return self._stats

    def _send_one(self) -> None:
        spec = choose_request(self._rng)
        body = json.dumps(spec.body).encode() if spec.body is not None else None
        started = self._clock()
        status = self._transport.send(spec.method, f"{self._target}{spec.path}", body)
        self._stats.record(status, self._clock() - started)

    def _worker(self, slots: queue.Queue[object]) -> None:
        while True:
            item = slots.get()
            if item is _SHUTDOWN:
                return
            try:
                self._send_one()
            except Exception:
                # One malformed response must not silently remove a worker and
                # quietly halve the offered rate for the rest of the run.
                logger.exception("request failed unexpectedly")
