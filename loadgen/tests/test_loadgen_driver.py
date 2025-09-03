from __future__ import annotations

import random
import threading

import pytest

from loadgen.driver import TRANSPORT_FAILURE_STATUS, LoadGenerator
from loadgen.scenarios import Fault, Phase, Scenario
from loadgen.stats import Stats


class RecordingTransport:
    """In-memory stand-in for the HTTP client."""

    def __init__(self, api_status: int = 200, fault_status: int = 200) -> None:
        self.api_status = api_status
        self.fault_status = fault_status
        self.calls: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def send(self, method: str, url: str, body: bytes | None) -> int:
        with self._lock:
            self.calls.append((method, url))
        return self.fault_status if "/faults" in url else self.api_status

    def paths(self) -> list[str]:
        with self._lock:
            return [url.split("http://svc", 1)[-1] for _, url in self.calls]


def two_phase_scenario() -> Scenario:
    return Scenario(
        name="unit",
        description="short scenario used by the tests",
        phases=(
            Phase(name="baseline", duration_seconds=1, rps=40.0),
            Phase(
                name="burst",
                duration_seconds=1,
                rps=40.0,
                fault=Fault(
                    kind="errors",
                    payload={"ratio": 1.0, "status_code": 503, "ttl_seconds": 60},
                ),
            ),
        ),
        expected_alerts=("ErrorBudgetBurnFast",),
    )


def test_run_sends_traffic_and_arms_the_fault_once() -> None:
    transport = RecordingTransport()
    generator = LoadGenerator(
        target="http://svc/",
        scenario=two_phase_scenario(),
        transport=transport,
        rng=random.Random(5),
        workers=4,
    )

    stats = generator.run()

    paths = transport.paths()
    assert paths.count("/faults/errors") == 1, "the fault must be armed exactly once"
    # Two one-second phases at 40 req/s; CI machines are slow, so only assert the
    # generator actually produced sustained traffic rather than an exact count.
    assert stats.sent >= 20
    assert stats.by_class == {"2xx": stats.sent}
    assert all(path.startswith("/api/") for path in paths if not path.startswith("/faults"))


def test_trailing_slash_in_the_target_does_not_produce_double_slashes() -> None:
    transport = RecordingTransport()
    generator = LoadGenerator(
        target="http://svc/",
        scenario=Scenario(
            name="unit",
            description="",
            phases=(Phase(name="only", duration_seconds=1, rps=20.0),),
            expected_alerts=(),
        ),
        transport=transport,
        workers=2,
    )

    generator.run()

    assert all("//api" not in url for _, url in transport.calls)


def test_arming_a_fault_against_a_broken_endpoint_fails_loudly() -> None:
    generator = LoadGenerator(
        target="http://svc",
        scenario=two_phase_scenario(),
        transport=RecordingTransport(fault_status=500),
        workers=2,
    )

    with pytest.raises(RuntimeError, match="failed to arm fault errors"):
        generator.arm(Fault(kind="errors", payload={"ratio": 1.0}))


def test_stop_ends_the_run_before_the_scenario_completes() -> None:
    scenario = Scenario(
        name="long",
        description="",
        phases=(Phase(name="only", duration_seconds=600, rps=50.0),),
        expected_alerts=(),
    )
    generator = LoadGenerator(
        target="http://svc",
        scenario=scenario,
        transport=RecordingTransport(),
        workers=2,
    )
    threading.Timer(0.5, generator.stop).start()

    stats = generator.run()

    assert stats.sent > 0


def test_server_errors_are_classified_not_swallowed() -> None:
    transport = RecordingTransport(api_status=503)
    generator = LoadGenerator(
        target="http://svc",
        scenario=Scenario(
            name="unit",
            description="",
            phases=(Phase(name="only", duration_seconds=1, rps=30.0),),
            expected_alerts=(),
        ),
        transport=transport,
        workers=2,
    )

    stats = generator.run()

    assert stats.errors == stats.sent
    assert stats.error_ratio == 1.0


def test_percentiles_on_an_empty_run_do_not_explode() -> None:
    stats = Stats()

    assert stats.percentile(0.5) == 0.0
    assert stats.error_ratio == 0.0
    assert "sent=0" in stats.summary()


def test_percentile_uses_nearest_rank() -> None:
    stats = Stats()
    for index in range(1, 101):
        stats.record(200, index / 1000.0)

    assert stats.percentile(0.0) == pytest.approx(0.001)
    assert stats.percentile(0.5) == pytest.approx(0.050)
    assert stats.percentile(0.95) == pytest.approx(0.095)
    assert stats.percentile(1.0) == pytest.approx(0.100)


def test_percentile_rejects_a_quantile_outside_the_unit_interval() -> None:
    with pytest.raises(ValueError, match="quantile"):
        Stats().percentile(1.5)


def test_transport_failures_count_as_client_side_errors() -> None:
    stats = Stats()
    stats.record(TRANSPORT_FAILURE_STATUS, 10.0)
    stats.record(200, 0.01)

    # A connection that never got a response is an error for the client, but the
    # service never saw the request and its own metrics will not show it. That is
    # why the client-side ratio and the server-side SLI are allowed to disagree,
    # and why only the latter drives the alerts.
    assert stats.by_class == {"5xx": 1, "2xx": 1}
    assert stats.error_ratio == 0.5
