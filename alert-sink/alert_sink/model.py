"""Parsing of the Alertmanager webhook payload (schema version 4).

The payload arrives over the network from a component that can be upgraded
independently, so nothing here trusts its shape: every field is checked and a
malformed notification is rejected with a message rather than crashing the
receiver. A webhook that dies on an unexpected key is a webhook that loses the
one alert you needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SUPPORTED_VERSION = "4"


class MalformedNotificationError(ValueError):
    """The body was JSON, but not an Alertmanager notification."""


@dataclass(frozen=True, slots=True)
class ReceivedAlert:
    receiver: str
    status: str
    alertname: str
    severity: str
    job: str
    instance: str
    summary: str
    runbook_url: str
    starts_at: str
    fingerprint: str

    def as_log_fields(self) -> dict[str, str]:
        return {
            "receiver": self.receiver,
            "status": self.status,
            "alertname": self.alertname,
            "severity": self.severity,
            "job": self.job,
            "instance": self.instance,
            "summary": self.summary,
            "runbook_url": self.runbook_url,
            "starts_at": self.starts_at,
            "fingerprint": self.fingerprint,
        }


def _string_map(value: object, field: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise MalformedNotificationError(f"{field} must be an object")
    return {str(key): str(item) for key, item in value.items()}


def parse_notification(payload: object) -> list[ReceivedAlert]:
    """Flatten one webhook body into individual alerts."""
    if not isinstance(payload, dict):
        raise MalformedNotificationError("notification must be a JSON object")

    version = payload.get("version")
    if version is not None and str(version) != SUPPORTED_VERSION:
        raise MalformedNotificationError(
            f"unsupported webhook schema version {version!r}, expected {SUPPORTED_VERSION}"
        )

    receiver = str(payload.get("receiver", "unknown"))
    raw_alerts: Any = payload.get("alerts")
    if not isinstance(raw_alerts, list):
        raise MalformedNotificationError("notification must carry an `alerts` array")

    parsed: list[ReceivedAlert] = []
    for entry in raw_alerts:
        if not isinstance(entry, dict):
            raise MalformedNotificationError("every element of `alerts` must be an object")
        labels = _string_map(entry.get("labels"), "labels")
        annotations = _string_map(entry.get("annotations"), "annotations")
        alertname = labels.get("alertname", "")
        if not alertname:
            raise MalformedNotificationError("alert is missing the `alertname` label")
        parsed.append(
            ReceivedAlert(
                receiver=receiver,
                status=str(entry.get("status", "unknown")),
                alertname=alertname,
                severity=labels.get("severity", "none"),
                job=labels.get("job", ""),
                instance=labels.get("instance", ""),
                summary=annotations.get("summary", ""),
                runbook_url=annotations.get("runbook_url", ""),
                starts_at=str(entry.get("startsAt", "")),
                fingerprint=str(entry.get("fingerprint", "")),
            )
        )
    return parsed
