from __future__ import annotations

import random

import pytest

from app.faults import MAX_TTL_SECONDS, FaultInjector


class FakeClock:
    """Monotonic clock the test drives by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_injector(seed: int = 7) -> tuple[FaultInjector, FakeClock]:
    clock = FakeClock()
    return FaultInjector(clock=clock, rng=random.Random(seed)), clock


def test_latency_fault_expires_exactly_at_its_ttl() -> None:
    injector, clock = make_injector()
    injector.inject_latency(delay_ms=400, jitter_ms=0, ttl_seconds=60)

    clock.advance(59.9)
    assert injector.next_delay_seconds() == pytest.approx(0.4)

    # `expires_at` is exclusive: at exactly the TTL the fault is already gone.
    clock.advance(0.1)
    assert injector.next_delay_seconds() == 0.0
    assert injector.state().latency is None


def test_jitter_stays_inside_the_requested_band() -> None:
    injector, _ = make_injector()
    injector.inject_latency(delay_ms=500, jitter_ms=200, ttl_seconds=60)

    draws = [injector.next_delay_seconds() for _ in range(500)]

    assert all(0.3 <= value <= 0.7 for value in draws)
    # A constant delay would also satisfy the bounds above, so check it varies.
    assert len(set(draws)) > 1


def test_error_ratio_zero_never_fails_and_one_always_does() -> None:
    injector, _ = make_injector()

    injector.inject_errors(ratio=0.0, status_code=500, ttl_seconds=60)
    assert all(injector.next_error_status() is None for _ in range(200))

    injector.inject_errors(ratio=1.0, status_code=503, ttl_seconds=60)
    assert all(injector.next_error_status() == 503 for _ in range(200))


def test_error_ratio_is_honoured_within_sampling_noise() -> None:
    injector, _ = make_injector(seed=1234)
    injector.inject_errors(ratio=0.3, status_code=502, ttl_seconds=600)

    failures = sum(injector.next_error_status() is not None for _ in range(5000))

    # 5000 Bernoulli(0.3) draws: sigma ~ 32, so 3% is well over five sigma.
    assert 0.27 <= failures / 5000 <= 0.33


def test_expired_error_fault_stops_failing_requests() -> None:
    injector, clock = make_injector()
    injector.inject_errors(ratio=1.0, status_code=500, ttl_seconds=30)

    assert injector.next_error_status() == 500
    clock.advance(31)
    assert injector.next_error_status() is None


def test_clear_removes_both_faults() -> None:
    injector, _ = make_injector()
    injector.inject_latency(delay_ms=100, jitter_ms=0, ttl_seconds=60)
    injector.inject_errors(ratio=1.0, status_code=500, ttl_seconds=60)

    injector.clear()

    state = injector.state()
    assert state.latency is None
    assert state.errors is None


@pytest.mark.parametrize(
    ("delay_ms", "jitter_ms", "ttl_seconds"),
    [
        (-1, 0, 60),
        (10, 20, 60),  # jitter larger than the delay would allow negative sleeps
        (100, 0, 0),
        (100, 0, MAX_TTL_SECONDS + 1),
        (10_001, 0, 60),
    ],
)
def test_invalid_latency_faults_are_rejected(
    delay_ms: int, jitter_ms: int, ttl_seconds: int
) -> None:
    injector, _ = make_injector()
    with pytest.raises(ValueError):
        injector.inject_latency(delay_ms=delay_ms, jitter_ms=jitter_ms, ttl_seconds=ttl_seconds)


@pytest.mark.parametrize(
    ("ratio", "status_code", "ttl_seconds"),
    [
        (1.5, 500, 60),
        (-0.1, 500, 60),
        (0.5, 404, 60),  # a 4xx is a client problem, not an availability fault
        (0.5, 500, 0),
    ],
)
def test_invalid_error_faults_are_rejected(
    ratio: float, status_code: int, ttl_seconds: int
) -> None:
    injector, _ = make_injector()
    with pytest.raises(ValueError):
        injector.inject_errors(ratio=ratio, status_code=status_code, ttl_seconds=ttl_seconds)


def test_a_second_injection_replaces_the_first() -> None:
    injector, _ = make_injector()
    injector.inject_latency(delay_ms=100, jitter_ms=0, ttl_seconds=60)
    injector.inject_latency(delay_ms=900, jitter_ms=0, ttl_seconds=60)

    assert injector.next_delay_seconds() == pytest.approx(0.9)
