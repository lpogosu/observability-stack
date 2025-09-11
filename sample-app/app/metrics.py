"""RED metrics for the HTTP surface.

Rate, Errors and Duration all come from two series families so that a single
scrape answers every question the SLOs ask. The label set is deliberately small:

  * `path` is the *route template* (`/api/items/{item_id}`), never the raw URL -
    otherwise every item id in production becomes its own time series;
  * `status` is the full code rather than a `2xx`/`5xx` class, because
    distinguishing a 503 from a 500 is worth three extra series per route and the
    recording rules collapse the label anyway;
  * the duration histogram carries no `status` label at all - a latency SLO that
    only counts successful requests is the standard definition, and dropping the
    label halves the number of bucket series.
"""

from __future__ import annotations

from typing import Final

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client.gc_collector import GCCollector
from prometheus_client.platform_collector import PlatformCollector
from prometheus_client.process_collector import ProcessCollector

# The 300 ms boundary exists because slo/sample-app.yml defines the latency
# objective at 300 ms. Bucket boundaries are the one part of a histogram that
# cannot be changed retroactively, so the SLO threshold has to be one of them.
DURATION_BUCKETS: Final = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.2,
    0.3,
    0.5,
    0.75,
    1.0,
    2.5,
    5.0,
    10.0,
)

REGISTRY: Final = CollectorRegistry(auto_describe=True)
ProcessCollector(registry=REGISTRY)
PlatformCollector(registry=REGISTRY)
GCCollector(registry=REGISTRY)

REQUESTS: Final = Counter(
    "http_requests",
    "HTTP requests handled, by route template and response status.",
    labelnames=("method", "path", "status"),
    registry=REGISTRY,
)

REQUEST_DURATION: Final = Histogram(
    "http_request_duration_seconds",
    "HTTP request handling latency in seconds, measured server-side.",
    labelnames=("method", "path"),
    buckets=DURATION_BUCKETS,
    registry=REGISTRY,
)

REQUESTS_IN_FLIGHT: Final = Gauge(
    "http_requests_in_flight",
    "HTTP requests currently being handled.",
    registry=REGISTRY,
)

ORDERS_CREATED: Final = Counter(
    "orders_created",
    "Orders accepted by the service.",
    registry=REGISTRY,
)


def observe_request(
    *,
    method: str,
    path: str,
    status: int,
    duration_seconds: float,
    trace_id: str | None,
) -> None:
    """Record one finished request.

    The trace id is attached to the histogram as an OpenMetrics exemplar, which is
    what turns a spike on the latency panel into a click-through to the exact
    slow trace in Tempo. Prometheus only stores exemplars when started with
    `--enable-feature=exemplar-storage`; without it the sample is simply ignored,
    so this is safe to emit unconditionally.
    """
    REQUESTS.labels(method=method, path=path, status=str(status)).inc()
    exemplar = {"trace_id": trace_id} if trace_id else None
    REQUEST_DURATION.labels(method=method, path=path).observe(
        duration_seconds, exemplar=exemplar
    )
