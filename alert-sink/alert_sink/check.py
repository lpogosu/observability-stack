"""`make check-alerts`: what is firing, and did the notification actually land?

Three questions get asked in one place, because during a demo (and during a real
incident) they fail independently:

  1. Prometheus - did the rule evaluate to firing?
  2. Alertmanager - did the alert survive routing and inhibition?
  3. alert-sink  - did a notification actually get delivered?

An alert that is firing in Prometheus but absent from the sink means the routing
tree or an inhibition rule swallowed it, and that is a configuration bug worth
seeing immediately rather than after the next outage.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

DEFAULT_TIMEOUT_SECONDS = 5.0


class QueryError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AlertRow:
    source: str
    alertname: str
    severity: str
    job: str
    state: str


def fetch_json(url: str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> Any:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise QueryError(f"{url} returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise QueryError(f"{url} is unreachable: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise QueryError(f"{url} did not return JSON: {exc}") from exc


def _labels(entry: object) -> dict[str, str]:
    if not isinstance(entry, dict):
        return {}
    raw = entry.get("labels")
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items()}


def prometheus_alerts(payload: object) -> list[AlertRow]:
    """Rows from Prometheus' /api/v1/alerts response."""
    rows: list[AlertRow] = []
    if not isinstance(payload, dict):
        return rows
    data = payload.get("data")
    alerts = data.get("alerts") if isinstance(data, dict) else None
    if not isinstance(alerts, list):
        return rows
    for entry in alerts:
        if not isinstance(entry, dict):
            continue
        labels = _labels(entry)
        state = str(entry.get("state", "unknown"))
        rows.append(
            AlertRow(
                source="prometheus",
                alertname=labels.get("alertname", "?"),
                severity=labels.get("severity", "-"),
                job=labels.get("job", "-"),
                state=state,
            )
        )
    return rows


def alertmanager_alerts(payload: object) -> list[AlertRow]:
    """Rows from Alertmanager's /api/v2/alerts response."""
    rows: list[AlertRow] = []
    if not isinstance(payload, list):
        return rows
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        labels = _labels(entry)
        status = entry.get("status")
        state = str(status.get("state", "unknown")) if isinstance(status, dict) else "unknown"
        rows.append(
            AlertRow(
                source="alertmanager",
                alertname=labels.get("alertname", "?"),
                severity=labels.get("severity", "-"),
                job=labels.get("job", "-"),
                state=state,
            )
        )
    return rows


def sink_alerts(payload: object) -> list[AlertRow]:
    """Rows from the local webhook receiver's /alerts/firing response."""
    rows: list[AlertRow] = []
    if not isinstance(payload, list):
        return rows
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        rows.append(
            AlertRow(
                source="notified",
                alertname=str(entry.get("alertname", "?")),
                severity=str(entry.get("severity", "-")),
                job=str(entry.get("job", "-")),
                state=str(entry.get("status", "unknown")),
            )
        )
    return rows


def render(rows: list[AlertRow]) -> str:
    if not rows:
        return "nothing is firing anywhere - the stack is quiet"
    header = f"{'SOURCE':<13}{'ALERT':<34}{'SEVERITY':<10}{'JOB':<18}STATE"
    lines = [header, "-" * len(header)]
    ordering = {"prometheus": 0, "alertmanager": 1, "notified": 2}
    for row in sorted(rows, key=lambda r: (r.alertname, ordering.get(r.source, 9))):
        lines.append(
            f"{row.source:<13}{row.alertname:<34}{row.severity:<10}{row.job:<18}{row.state}"
        )
    return "\n".join(lines)


def collect(prometheus: str, alertmanager: str, sink: str) -> tuple[list[AlertRow], list[str]]:
    rows: list[AlertRow] = []
    problems: list[str] = []
    sources = (
        (f"{prometheus.rstrip('/')}/api/v1/alerts", prometheus_alerts),
        (f"{alertmanager.rstrip('/')}/api/v2/alerts", alertmanager_alerts),
        (f"{sink.rstrip('/')}/alerts/firing", sink_alerts),
    )
    for url, parser in sources:
        try:
            rows.extend(parser(fetch_json(url)))
        except QueryError as exc:
            problems.append(str(exc))
    return rows, problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check-alerts",
        description="Show what is firing in Prometheus, Alertmanager and the webhook sink.",
    )
    parser.add_argument("--prometheus", default="http://localhost:9090")
    parser.add_argument("--alertmanager", default="http://localhost:9093")
    parser.add_argument("--sink", default="http://localhost:9095")
    args = parser.parse_args(argv)

    rows, problems = collect(args.prometheus, args.alertmanager, args.sink)
    print(render(rows))
    for problem in problems:
        print(f"warning: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
