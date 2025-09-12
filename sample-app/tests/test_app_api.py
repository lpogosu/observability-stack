from __future__ import annotations

import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.main import FAULTS, app
from app.metrics import REGISTRY


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _clear_faults() -> Iterator[None]:
    FAULTS.clear()
    yield
    FAULTS.clear()


def requests_total(method: str, path: str, status: int) -> float:
    value = REGISTRY.get_sample_value(
        "http_requests_total",
        {"method": method, "path": path, "status": str(status)},
    )
    return value or 0.0


def test_healthz_is_not_counted_in_the_sli(client: TestClient) -> None:
    before = requests_total("GET", "/healthz", 200)

    assert client.get("/healthz").json() == {"status": "ok"}

    # Health-check traffic in the denominator makes availability look better than
    # it is; /healthz is watched by a blackbox probe instead.
    assert requests_total("GET", "/healthz", 200) == before == 0.0


def test_successful_request_is_counted_once(client: TestClient) -> None:
    before = requests_total("GET", "/api/items", 200)

    response = client.get("/api/items", params={"limit": 2})

    assert response.status_code == 200
    assert len(response.json()) == 2
    assert requests_total("GET", "/api/items", 200) == before + 1


def test_path_label_is_the_route_template_not_the_url(client: TestClient) -> None:
    before = requests_total("GET", "/api/items/{item_id}", 200)

    for item_id in (1, 2, 3):
        assert client.get(f"/api/items/{item_id}").status_code == 200

    # Three distinct URLs, one time series.
    assert requests_total("GET", "/api/items/{item_id}", 200) == before + 3


def test_unmatched_paths_collapse_into_one_series(client: TestClient) -> None:
    before = requests_total("GET", "__unmatched__", 404)

    for suffix in ("a", "b", "c"):
        assert client.get(f"/nothing/{suffix}").status_code == 404

    assert requests_total("GET", "__unmatched__", 404) == before + 3


def test_missing_item_is_a_client_error_not_an_availability_failure(
    client: TestClient,
) -> None:
    before_404 = requests_total("GET", "/api/items/{item_id}", 404)

    assert client.get("/api/items/999").status_code == 404

    assert requests_total("GET", "/api/items/{item_id}", 404) == before_404 + 1
    # 4xx must stay out of the 5xx family the availability SLI is built on.
    assert requests_total("GET", "/api/items/{item_id}", 500) == 0.0


def test_injected_errors_produce_5xx_and_are_reversible(client: TestClient) -> None:
    before = requests_total("GET", "/api/items", 503)

    armed = client.post(
        "/faults/errors", json={"ratio": 1.0, "status_code": 503, "ttl_seconds": 60}
    )
    assert armed.status_code == 200
    assert armed.json()["error_ratio"] == 1.0

    assert client.get("/api/items").status_code == 503
    assert requests_total("GET", "/api/items", 503) == before + 1

    assert client.delete("/faults").json()["error_ratio"] is None
    assert client.get("/api/items").status_code == 200


def test_injected_latency_actually_delays_the_response(client: TestClient) -> None:
    client.post(
        "/faults/latency", json={"delay_ms": 250, "jitter_ms": 0, "ttl_seconds": 60}
    )

    started = time.perf_counter()
    assert client.get("/api/items").status_code == 200
    elapsed = time.perf_counter() - started

    assert elapsed >= 0.25


def test_fault_endpoints_reject_out_of_range_input(client: TestClient) -> None:
    assert client.post("/faults/errors", json={"ratio": 2.0}).status_code == 422
    assert client.post("/faults/errors", json={"ratio": 0.5, "status_code": 404}).status_code == 422
    assert client.post("/faults/latency", json={"delay_ms": 99_999}).status_code == 422


def test_order_creation_charges_the_catalog_price(client: TestClient) -> None:
    response = client.post("/api/orders", json={"item_id": 2, "quantity": 3})

    assert response.status_code == 201
    body = response.json()
    assert body["total_cents"] == 7_450 * 3
    assert len(body["id"]) == 12


def test_ordering_an_unknown_item_is_rejected(client: TestClient) -> None:
    assert client.post("/api/orders", json={"item_id": 99, "quantity": 1}).status_code == 404
    assert client.post("/api/orders", json={"item_id": 1, "quantity": 0}).status_code == 422


def test_metrics_endpoint_exposes_the_slo_bucket(client: TestClient) -> None:
    client.get("/api/items")

    response = client.get("/metrics")

    assert response.status_code == 200, "the scrape must not be answered with a redirect"
    body = response.text
    # The latency SLO is written against 300 ms, so that boundary has to exist as
    # a bucket - a quantile cannot be reconstructed from buckets that miss it.
    assert 'http_request_duration_seconds_bucket{le="0.3"' in body
    assert "http_requests_total" in body
    assert "http_requests_in_flight" in body


def test_openmetrics_scrape_carries_trace_exemplars(client: TestClient) -> None:
    """The metrics-to-traces link only exists in the OpenMetrics encoding."""
    client.get("/api/items")

    response = client.get(
        "/metrics",
        headers={"Accept": "application/openmetrics-text; version=1.0.0; charset=utf-8"},
    )

    assert "openmetrics-text" in response.headers["content-type"]
    exemplars = [
        line
        for line in response.text.splitlines()
        if line.startswith("http_request_duration_seconds_bucket") and "# {trace_id=" in line
    ]
    assert exemplars, "no exemplar was attached to the latency histogram"


def test_default_scrape_omits_exemplars(client: TestClient) -> None:
    """A client that did not ask for OpenMetrics gets plain text, not a parse error."""
    client.get("/api/items")

    response = client.get("/metrics", headers={"Accept": "text/plain"})

    assert "text/plain" in response.headers["content-type"]
    assert "# {trace_id=" not in response.text
