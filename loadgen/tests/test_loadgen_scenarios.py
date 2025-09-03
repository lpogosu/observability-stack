from __future__ import annotations

import random

import pytest

from loadgen.driver import choose_request
from loadgen.scenarios import SCENARIOS, Fault, Phase, Scenario


def test_phase_boundaries_are_half_open() -> None:
    scenario = Scenario(
        name="t",
        description="",
        phases=(
            Phase(name="a", duration_seconds=60, rps=1.0),
            Phase(name="b", duration_seconds=30, rps=2.0),
        ),
        expected_alerts=(),
    )

    assert scenario.phase_at(0.0) is scenario.phases[0]
    assert scenario.phase_at(59.999) is scenario.phases[0]
    # The instant a phase ends belongs to the next one, not to both.
    assert scenario.phase_at(60.0) is scenario.phases[1]
    assert scenario.phase_at(89.999) is scenario.phases[1]
    assert scenario.phase_at(90.0) is None


def test_duration_and_peak_are_derived_from_the_phases() -> None:
    scenario = SCENARIOS["error-burst"]

    assert scenario.duration_seconds == sum(p.duration_seconds for p in scenario.phases)
    assert scenario.peak_rps == max(p.rps for p in scenario.phases)


def test_negative_elapsed_time_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="negative"):
        SCENARIOS["normal"].phase_at(-1.0)


@pytest.mark.parametrize(
    ("duration", "rps"),
    [(0, 1.0), (-5, 1.0), (60, 0.0), (60, -1.0)],
)
def test_degenerate_phases_are_rejected(duration: int, rps: float) -> None:
    with pytest.raises(ValueError):
        Phase(name="bad", duration_seconds=duration, rps=rps)


def test_a_scenario_needs_at_least_one_phase() -> None:
    with pytest.raises(ValueError, match="no phases"):
        Scenario(name="empty", description="", phases=(), expected_alerts=())


def test_fault_routing_matches_the_service_api() -> None:
    assert Fault(kind="errors", payload={"ratio": 0.5}).path == "/faults/errors"
    assert Fault(kind="errors", payload={"ratio": 0.5}).method == "POST"
    assert Fault(kind="latency", payload={"delay_ms": 700}).path == "/faults/latency"
    # Clearing is a DELETE on the collection, not a POST of an empty fault.
    assert Fault(kind="clear", payload={}).path == "/faults"
    assert Fault(kind="clear", payload={}).method == "DELETE"


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_every_fault_carries_a_ttl_that_outlives_its_phase(name: str) -> None:
    """A fault that expires mid-phase would silently end the incident early."""
    scenario = SCENARIOS[name]
    for phase in scenario.phases:
        if phase.fault is None or phase.fault.kind == "clear":
            continue
        ttl = phase.fault.payload["ttl_seconds"]
        assert ttl >= phase.duration_seconds, f"{name}/{phase.name}"


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_failure_scenarios_outlast_the_long_burn_rate_window(name: str) -> None:
    """A scenario shorter than the alert's detection time cannot demonstrate it.

    The fast burn-rate alert needs the incident to occupy 1/14.4 of its 1h window
    plus the 2m `for`, so anything under ~10 minutes of sustained fault would end
    before the alert had a chance to fire.
    """
    scenario = SCENARIOS[name]
    if not scenario.expected_alerts:
        return
    faulted = sum(
        phase.duration_seconds
        for phase in scenario.phases
        if phase.fault is not None and phase.fault.kind != "clear"
    )
    if scenario.name == "traffic-drop":
        # This one has no fault: the incident is the low-rate phase itself.
        faulted = scenario.phases[-1].duration_seconds
    assert faulted >= 900, f"{name} sustains its fault for only {faulted}s"


def test_scenario_catalogue_is_self_consistent() -> None:
    for key, scenario in SCENARIOS.items():
        assert key == scenario.name
        assert scenario.description.strip()


def test_normal_scenario_promises_silence() -> None:
    assert SCENARIOS["normal"].expected_alerts == ()
    assert all(phase.fault is None for phase in SCENARIOS["normal"].phases)


def test_request_mix_is_deterministic_under_a_seed() -> None:
    first_rng, second_rng = random.Random(99), random.Random(99)

    first = [choose_request(first_rng).path for _ in range(20)]
    second = [choose_request(second_rng).path for _ in range(20)]

    assert first == second
    assert len(set(first)) > 1


def test_request_mix_is_read_heavy() -> None:
    rng = random.Random(2024)

    sample = [choose_request(rng) for _ in range(4000)]

    # 9 of the 10 weight units are GETs; 4000 draws put the 3-sigma band at ~1.5%.
    reads = sum(spec.method == "GET" for spec in sample)
    assert 0.87 <= reads / len(sample) <= 0.93
    assert {spec.path for spec in sample} == {
        "/api/items",
        "/api/items/1",
        "/api/items/2",
        "/api/items/3",
        "/api/orders",
    }
