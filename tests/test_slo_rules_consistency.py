"""Repository-level checks that keep the documentation and the rules honest.

An SLO written in a YAML file and an alert written in PromQL drift apart the
moment somebody renegotiates one of them. These tests make that drift a build
failure instead of a surprise during the next incident review.
"""

from __future__ import annotations

import pathlib
import re
from typing import Any

import pytest
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SLO_FILE = REPO_ROOT / "slo" / "sample-app.yml"
ALERT_FILE = REPO_ROOT / "prometheus" / "rules" / "alerting.yml"
RECORDING_FILE = REPO_ROOT / "prometheus" / "rules" / "recording.yml"
RUNBOOKS = REPO_ROOT / "docs" / "runbooks.md"
RULE_TEST_DIR = REPO_ROOT / "prometheus" / "rules" / "tests"

RUNBOOK_BASE = "https://github.com/lpogosu/observability-stack/blob/main/docs/runbooks.md#"


def load(path: pathlib.Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def alert_rules() -> dict[str, dict[str, Any]]:
    document = load(ALERT_FILE)
    rules: dict[str, dict[str, Any]] = {}
    for group in document["groups"]:
        for rule in group["rules"]:
            rules[rule["alert"]] = rule
    return rules


def recorded_metric_names() -> set[str]:
    document = load(RECORDING_FILE)
    return {
        rule["record"] for group in document["groups"] for rule in group["rules"]
    }


def slo_alerting_entries() -> list[tuple[dict[str, Any], dict[str, Any]]]:
    document = load(SLO_FILE)
    return [
        (objective, entry)
        for objective in document["objectives"]
        for entry in objective["alerting"]
    ]


ALERTS = alert_rules()
SLO_ENTRIES = slo_alerting_entries()
RUNBOOK_TEXT = RUNBOOKS.read_text(encoding="utf-8")
RUNBOOK_ANCHORS = {
    re.sub(r"[^a-z0-9]", "", line[3:].strip().lower())
    for line in RUNBOOK_TEXT.splitlines()
    if line.startswith("## ")
}


@pytest.mark.parametrize(
    ("objective", "entry"),
    SLO_ENTRIES,
    ids=[f"{o['name']}-{e['alert']}" for o, e in SLO_ENTRIES],
)
def test_every_declared_slo_alert_exists_with_the_declared_shape(
    objective: dict[str, Any], entry: dict[str, Any]
) -> None:
    name = entry["alert"]
    assert name in ALERTS, f"{SLO_FILE.name} declares {name} but no rule implements it"
    rule = ALERTS[name]

    assert rule["labels"]["severity"] == entry["severity"]
    assert rule["labels"]["slo"] == objective["name"]
    assert rule["for"] == entry["for"]

    # The threshold is written into the rule as `burn_rate * budget` precisely so
    # that it can be read back and compared with the objective.
    threshold = f"({entry['burn_rate']} * {objective['error_budget']})"
    assert threshold in rule["expr"], (
        f"{name} must compare against {threshold}; found:\n{rule['expr']}"
    )

    prefix = objective["sli"]["recording_rule_prefix"]
    for window in (entry["long_window"], entry["short_window"]):
        assert f"{prefix}{window}" in rule["expr"], (
            f"{name} must use the {window} window via {prefix}{window}"
        )


@pytest.mark.parametrize(
    ("objective", "entry"),
    SLO_ENTRIES,
    ids=[f"{o['name']}-{e['alert']}" for o, e in SLO_ENTRIES],
)
def test_declared_burn_rates_match_the_documented_arithmetic(
    objective: dict[str, Any], entry: dict[str, Any]
) -> None:
    """burn_rate = budget_consumed * (SLO window / alert window).

    This is the formula in slo/README.md; if someone edits one number without
    the other the objective stops meaning what the document says it means.
    """
    slo_window_hours = 30 * 24
    alert_window_hours = {"1h": 1.0, "6h": 6.0, "3d": 72.0}[entry["long_window"]]
    expected = entry["budget_consumed_at_detection"] * slo_window_hours / alert_window_hours

    assert entry["burn_rate"] == pytest.approx(expected, rel=1e-9)


@pytest.mark.parametrize(
    ("objective", "entry"),
    SLO_ENTRIES,
    ids=[f"{o['name']}-{e['alert']}" for o, e in SLO_ENTRIES],
)
def test_short_window_is_one_twelfth_of_the_long_one(
    objective: dict[str, Any], entry: dict[str, Any]
) -> None:
    """The ratio that keeps the alert from firing for a full long window after
    the incident has already ended."""
    minutes = {"5m": 5, "30m": 30, "1h": 60, "6h": 360}
    assert minutes[entry["long_window"]] / minutes[entry["short_window"]] == 12


@pytest.mark.parametrize("name", sorted(ALERTS))
def test_every_alert_is_actionable(name: str) -> None:
    rule = ALERTS[name]
    annotations = rule.get("annotations", {})
    for required in ("summary", "description", "runbook_url"):
        assert annotations.get(required, "").strip(), f"{name} is missing {required}"
    assert rule["labels"]["severity"] in {"critical", "warning"}
    # Every alert waits before firing. An alert without `for` fires on a single
    # scrape, which on a 15s interval is one unlucky sample.
    assert rule.get("for"), f"{name} has no `for` clause"


@pytest.mark.parametrize("name", sorted(ALERTS))
def test_runbook_links_resolve_to_a_real_section(name: str) -> None:
    url = ALERTS[name]["annotations"]["runbook_url"]
    assert url.startswith(RUNBOOK_BASE), f"{name} points outside docs/runbooks.md"
    anchor = url[len(RUNBOOK_BASE) :]
    assert anchor == name.lower(), f"{name} should anchor at #{name.lower()}"
    assert anchor in RUNBOOK_ANCHORS, f"docs/runbooks.md has no section for {name}"


@pytest.mark.parametrize("name", sorted(ALERTS))
def test_every_alert_is_covered_by_a_rule_unit_test(name: str) -> None:
    """A rule that no test exercises is a rule nobody has ever seen fire."""
    covered = any(
        name in path.read_text(encoding="utf-8") for path in RULE_TEST_DIR.glob("*_test.yml")
    )
    assert covered, f"no promtool unit test mentions {name}"


def test_alert_expressions_only_reference_recording_rules_that_exist() -> None:
    recorded = recorded_metric_names()
    referenced = set()
    for rule in ALERTS.values():
        referenced.update(re.findall(r"\b[a-z_]+(?::[a-z0-9_]+){2}\b", rule["expr"]))

    missing = referenced - recorded
    assert not missing, f"alerts reference undefined recording rules: {sorted(missing)}"


def test_recording_rules_follow_the_level_metric_operations_convention() -> None:
    for name in recorded_metric_names():
        assert re.fullmatch(r"[a-z_]+:[a-z0-9_]+:[a-z0-9_]+", name), (
            f"{name} does not match level:metric:operations"
        )
        level = name.split(":", 1)[0]
        assert level in {"job", "job_path"}, (
            f"{name} aggregates to an undocumented level {level!r}"
        )
