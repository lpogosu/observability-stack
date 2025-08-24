from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from http.server import ThreadingHTTPServer

import pytest

from alert_sink import server
from alert_sink.check import (
    AlertRow,
    alertmanager_alerts,
    prometheus_alerts,
    render,
    sink_alerts,
)
from alert_sink.model import MalformedNotificationError, ReceivedAlert, parse_notification
from alert_sink.store import AlertStore


def notification(
    *, status: str = "firing", alertname: str = "ErrorBudgetBurnFast", fingerprint: str = "abc"
) -> dict[str, object]:
    return {
        "version": "4",
        "receiver": "pager",
        "status": status,
        "alerts": [
            {
                "status": status,
                "labels": {
                    "alertname": alertname,
                    "severity": "critical",
                    "job": "sample-app",
                    "slo": "availability",
                },
                "annotations": {
                    "summary": "budget burning",
                    "runbook_url": "https://example.invalid/runbook",
                },
                "startsAt": "2025-08-14T20:11:03.481Z",
                "fingerprint": fingerprint,
            }
        ],
    }


def alert(name: str = "A", status: str = "firing", fingerprint: str = "f1") -> ReceivedAlert:
    return ReceivedAlert(
        receiver="pager",
        status=status,
        alertname=name,
        severity="critical",
        job="sample-app",
        instance="sample-app:8000",
        summary="",
        runbook_url="",
        starts_at="",
        fingerprint=fingerprint,
    )


def test_a_well_formed_notification_is_flattened_into_alerts() -> None:
    parsed = parse_notification(notification())

    assert len(parsed) == 1
    assert parsed[0].alertname == "ErrorBudgetBurnFast"
    assert parsed[0].severity == "critical"
    assert parsed[0].receiver == "pager"
    assert parsed[0].runbook_url == "https://example.invalid/runbook"


def test_missing_labels_fall_back_instead_of_crashing() -> None:
    payload = {
        "version": "4",
        "receiver": "fallback",
        "alerts": [{"labels": {"alertname": "Bare"}}],
    }

    parsed = parse_notification(payload)

    assert parsed[0].severity == "none"
    assert parsed[0].job == ""
    assert parsed[0].status == "unknown"


@pytest.mark.parametrize(
    "payload",
    [
        [],
        "not an object",
        {"version": "4"},
        {"version": "4", "alerts": {}},
        {"version": "4", "alerts": ["nope"]},
        {"version": "4", "alerts": [{"labels": {"severity": "critical"}}]},
        {"version": "3", "alerts": []},
    ],
)
def test_malformed_notifications_are_rejected(payload: object) -> None:
    with pytest.raises(MalformedNotificationError):
        parse_notification(payload)


def test_store_evicts_the_oldest_entries_when_full() -> None:
    store = AlertStore(capacity=3)

    store.add_all(alert(name=f"A{index}", fingerprint=str(index)) for index in range(5))

    assert len(store) == 3
    assert [item.alertname for item in store.recent()] == ["A4", "A3", "A2"]


def test_store_rejects_a_nonpositive_capacity() -> None:
    with pytest.raises(ValueError, match="capacity"):
        AlertStore(capacity=0)


def test_firing_reflects_the_latest_state_per_fingerprint() -> None:
    store = AlertStore()
    store.add_all([alert(name="Burn", status="firing", fingerprint="f1")])
    store.add_all([alert(name="Down", status="firing", fingerprint="f2")])

    assert {item.alertname for item in store.firing()} == {"Burn", "Down"}

    # Alertmanager re-sends the same fingerprint on resolution.
    store.add_all([alert(name="Burn", status="resolved", fingerprint="f1")])

    assert {item.alertname for item in store.firing()} == {"Down"}
    # The resolved notification is still in the history for the audit trail.
    assert len(store.recent()) == 3


def test_recent_limit_is_validated() -> None:
    with pytest.raises(ValueError, match="limit"):
        AlertStore().recent(limit=-1)


def test_prometheus_response_is_projected_onto_rows() -> None:
    payload = {
        "status": "success",
        "data": {
            "alerts": [
                {
                    "labels": {"alertname": "TargetDown", "severity": "critical", "job": "x"},
                    "state": "firing",
                }
            ]
        },
    }

    assert prometheus_alerts(payload) == [
        AlertRow("prometheus", "TargetDown", "critical", "x", "firing")
    ]


def test_alertmanager_response_is_projected_onto_rows() -> None:
    payload = [
        {
            "labels": {"alertname": "TargetDown", "severity": "critical", "job": "x"},
            "status": {"state": "active"},
        }
    ]

    assert alertmanager_alerts(payload) == [
        AlertRow("alertmanager", "TargetDown", "critical", "x", "active")
    ]


@pytest.mark.parametrize("payload", [None, {}, [1, 2], {"data": {"alerts": "no"}}])
def test_unexpected_api_shapes_produce_no_rows_rather_than_an_exception(
    payload: object,
) -> None:
    assert prometheus_alerts(payload) == []
    assert alertmanager_alerts(payload) == []
    assert sink_alerts(payload) == []


def test_render_says_so_when_nothing_is_firing() -> None:
    assert "quiet" in render([])


def test_render_groups_the_three_sources_under_each_alert() -> None:
    rows = [
        AlertRow("notified", "Burn", "critical", "sample-app", "firing"),
        AlertRow("prometheus", "Burn", "critical", "sample-app", "firing"),
        AlertRow("alertmanager", "Burn", "critical", "sample-app", "active"),
    ]

    body = render(rows).splitlines()[2:]

    # Same alert, ordered along the delivery path, so a gap is obvious.
    assert [line.split()[0] for line in body] == ["prometheus", "alertmanager", "notified"]


@pytest.fixture
def running_sink() -> Iterator[str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.WebhookHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def post(url: str, payload: object) -> tuple[int, object]:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def get(url: str) -> object:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read())


def test_webhook_accepts_a_notification_and_serves_it_back(running_sink: str) -> None:
    server.STORE.recent()  # touch the module-level store before asserting on it
    status, body = post(f"{running_sink}/webhook/pager", notification(fingerprint="e2e"))

    assert status == 200
    assert body == {"accepted": 1}

    firing = get(f"{running_sink}/alerts/firing")
    assert isinstance(firing, list)
    assert any(item["fingerprint"] == "e2e" for item in firing)


def test_webhook_rejects_garbage_without_dying(running_sink: str) -> None:
    request = urllib.request.Request(
        f"{running_sink}/webhook/pager", data=b"{not json", method="POST"
    )
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request, timeout=5)
    assert caught.value.code == 400

    # The server survived and still answers.
    assert get(f"{running_sink}/healthz")["status"] == "ok"  # type: ignore[index]


def test_unknown_paths_are_404(running_sink: str) -> None:
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(f"{running_sink}/nope", timeout=5)
    assert caught.value.code == 404
