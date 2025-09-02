"""Load scenarios.

Each scenario is a timeline of phases: a request rate plus, optionally, a fault
to arm when the phase starts. Scenarios are pure data with no I/O, which is what
makes the timeline arithmetic testable without a running stack.

The durations are not arbitrary. A burn-rate alert with a 1h long window cannot
fire until the incident occupies enough of that hour to push the ratio past the
threshold, so a scenario that only runs for five minutes proves nothing. Each
scenario below runs long enough for the alerts it names in `expected_alerts` to
actually reach the firing state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

FaultKind = Literal["latency", "errors", "clear"]


@dataclass(frozen=True, slots=True)
class Fault:
    kind: FaultKind
    payload: dict[str, float]

    @property
    def path(self) -> str:
        if self.kind == "clear":
            return "/faults"
        return f"/faults/{self.kind}"

    @property
    def method(self) -> str:
        return "DELETE" if self.kind == "clear" else "POST"


@dataclass(frozen=True, slots=True)
class Phase:
    name: str
    duration_seconds: int
    rps: float
    fault: Fault | None = None

    def __post_init__(self) -> None:
        if self.duration_seconds <= 0:
            raise ValueError(f"phase {self.name!r} must last at least one second")
        if self.rps <= 0:
            raise ValueError(f"phase {self.name!r} must have a positive request rate")


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    description: str
    phases: tuple[Phase, ...]
    expected_alerts: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.phases:
            raise ValueError(f"scenario {self.name!r} has no phases")

    @property
    def duration_seconds(self) -> int:
        return sum(phase.duration_seconds for phase in self.phases)

    @property
    def peak_rps(self) -> float:
        return max(phase.rps for phase in self.phases)

    def phase_at(self, elapsed_seconds: float) -> Phase | None:
        """Phase covering `elapsed_seconds`, or None once the scenario is over.

        Boundaries are half-open: a phase owns [start, start + duration).
        """
        if elapsed_seconds < 0:
            raise ValueError("elapsed_seconds must not be negative")
        boundary = 0.0
        for phase in self.phases:
            boundary += phase.duration_seconds
            if elapsed_seconds < boundary:
                return phase
        return None


# Every scenario opens with a quiet baseline so the burn-rate windows have
# something to contrast the incident against.
_BASELINE_SECONDS: Final = 300

SCENARIOS: Final[dict[str, Scenario]] = {
    "normal": Scenario(
        name="normal",
        description=(
            "Steady healthy traffic. Used to establish the baseline every other "
            "scenario is compared against, and to confirm the stack is silent "
            "when nothing is wrong."
        ),
        phases=(Phase(name="steady", duration_seconds=1800, rps=20.0),),
        expected_alerts=(),
    ),
    "error-burst": Scenario(
        name="error-burst",
        description=(
            "35% of API requests start failing with 503. At a 0.1% error budget "
            "that is a 350x burn rate, so both burn-rate windows cross their "
            "thresholds within minutes."
        ),
        phases=(
            Phase(
                name="baseline",
                duration_seconds=_BASELINE_SECONDS,
                rps=20.0,
                fault=Fault(kind="clear", payload={}),
            ),
            Phase(
                name="burst",
                duration_seconds=1500,
                rps=20.0,
                fault=Fault(
                    kind="errors",
                    payload={"ratio": 0.35, "status_code": 503, "ttl_seconds": 1500},
                ),
            ),
            Phase(
                name="recovery",
                duration_seconds=600,
                rps=20.0,
                fault=Fault(kind="clear", payload={}),
            ),
        ),
        expected_alerts=("ErrorBudgetBurnFast", "ErrorBudgetBurnSlow"),
    ),
    "latency-spike": Scenario(
        name="latency-spike",
        description=(
            "Every API request gains 700 +/- 200 ms, putting 100% of them past "
            "the 300 ms objective. The 5m window saturates immediately; the 1h "
            "window needs about nine minutes of it before the page is justified."
        ),
        phases=(
            Phase(
                name="baseline",
                duration_seconds=_BASELINE_SECONDS,
                rps=10.0,
                fault=Fault(kind="clear", payload={}),
            ),
            Phase(
                name="spike",
                duration_seconds=1800,
                rps=10.0,
                fault=Fault(
                    kind="latency",
                    payload={"delay_ms": 700, "jitter_ms": 200, "ttl_seconds": 1800},
                ),
            ),
            Phase(
                name="recovery",
                duration_seconds=600,
                rps=10.0,
                fault=Fault(kind="clear", payload={}),
            ),
        ),
        expected_alerts=("LatencyBudgetBurnFast", "LatencyBudgetBurnSlow"),
    ),
    "traffic-drop": Scenario(
        name="traffic-drop",
        description=(
            "Traffic collapses from 25 req/s to 0.2 req/s with nothing failing "
            "and nothing slow. Every ratio-based alert stays silent because the "
            "denominator disappeared along with the numerator; only TrafficDropped "
            "notices."
        ),
        phases=(
            Phase(
                name="baseline",
                duration_seconds=1200,
                rps=25.0,
                fault=Fault(kind="clear", payload={}),
            ),
            Phase(name="collapse", duration_seconds=1800, rps=0.2),
        ),
        expected_alerts=("TrafficDropped",),
    ),
}
