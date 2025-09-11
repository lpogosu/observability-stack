"""A small HTTP service that is instrumented well enough to alert on.

The point of this app is not the domain model - it is that every request produces
a metric, a log line and a span that all reference the same trace id, and that
the two failure modes the SLOs care about can be turned on from the outside.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Final

from fastapi import Depends, FastAPI, HTTPException, Response
from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode, Tracer
from prometheus_client.exposition import choose_encoder
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request

from app.config import Settings
from app.faults import MAX_LATENCY_MS, MAX_TTL_SECONDS, FaultInjector, FaultState
from app.metrics import ORDERS_CREATED, REGISTRY, REQUESTS_IN_FLIGHT, observe_request
from app.telemetry import (
    annotate_error,
    configure_logging,
    configure_tracing,
    current_trace_id,
)

SETTINGS: Final = Settings.from_env()
FAULTS: Final = FaultInjector()

# Configured at import rather than in the lifespan hook: uvicorn writes its own
# startup banner before the hook runs, and two plain-text lines at the head of an
# otherwise JSON stream are exactly the thing that breaks a log parser.
configure_logging()

# Scrapes, health checks and the fault-control API are infrastructure traffic.
# Counting them would put a constant stream of guaranteed-successful requests
# into the SLI denominator, which quietly inflates availability and dampens every
# error ratio. /healthz is covered end-to-end by a blackbox probe instead.
UNINSTRUMENTED_PREFIXES: Final = ("/metrics", "/healthz", "/readyz", "/faults")

logger: Final = logging.getLogger("sample-app")
_rng: Final = random.Random()


class Item(BaseModel):
    id: int
    name: str
    price_cents: int


class Order(BaseModel):
    id: str
    item_id: int
    quantity: int
    total_cents: int


class OrderRequest(BaseModel):
    item_id: int = Field(ge=1)
    quantity: int = Field(ge=1, le=100)


class LatencyFaultRequest(BaseModel):
    delay_ms: int = Field(ge=0, le=MAX_LATENCY_MS)
    jitter_ms: int = Field(default=0, ge=0, le=MAX_LATENCY_MS)
    ttl_seconds: int = Field(default=300, gt=0, le=MAX_TTL_SECONDS)


class ErrorFaultRequest(BaseModel):
    ratio: float = Field(ge=0.0, le=1.0)
    status_code: int = Field(default=503, ge=500, le=599)
    ttl_seconds: int = Field(default=300, gt=0, le=MAX_TTL_SECONDS)


class FaultView(BaseModel):
    latency_delay_ms: int | None = None
    latency_jitter_ms: int | None = None
    error_ratio: float | None = None
    error_status_code: int | None = None

    @classmethod
    def of(cls, state: FaultState) -> FaultView:
        return cls(
            latency_delay_ms=state.latency.delay_ms if state.latency else None,
            latency_jitter_ms=state.latency.jitter_ms if state.latency else None,
            error_ratio=state.errors.ratio if state.errors else None,
            error_status_code=state.errors.status_code if state.errors else None,
        )


CATALOG: Final[dict[int, Item]] = {
    1: Item(id=1, name="ferrofluid display", price_cents=18_900),
    2: Item(id=2, name="nixie clock kit", price_cents=7_450),
    3: Item(id=3, name="thermal printer", price_cents=12_300),
    4: Item(id=4, name="rotary encoder pack", price_cents=1_990),
}


def _tracer() -> Tracer:
    return trace.get_tracer(SETTINGS.service_name, SETTINGS.service_version)


def _route_template(request: Request) -> str:
    """Route pattern (`/api/items/{item_id}`) rather than the concrete URL.

    Using the raw path would make `path` unbounded: one series per item id, per
    method, per status. Requests that match no route collapse into a single
    bucket for the same reason.
    """
    route: object = request.scope.get("route")
    path_format = getattr(route, "path_format", None)
    if isinstance(path_format, str):
        return path_format
    return "__unmatched__"


class ObservabilityMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if request.url.path.startswith(UNINSTRUMENTED_PREFIXES):
            return await call_next(request)

        started = time.perf_counter()
        status = 500
        REQUESTS_IN_FLIGHT.inc()
        with _tracer().start_as_current_span(
            f"{request.method} {request.url.path}", kind=SpanKind.SERVER
        ) as span:
            try:
                response = await call_next(request)
                status = response.status_code
                return response
            except Exception as exc:
                annotate_error(span, exc)
                raise
            finally:
                elapsed = time.perf_counter() - started
                REQUESTS_IN_FLIGHT.dec()
                route = _route_template(request)
                span.update_name(f"{request.method} {route}")
                span.set_attribute("http.request.method", request.method)
                span.set_attribute("http.route", route)
                span.set_attribute("http.response.status_code", status)
                if status >= 500:
                    span.set_status(Status(StatusCode.ERROR, f"HTTP {status}"))
                observe_request(
                    method=request.method,
                    path=route,
                    status=status,
                    duration_seconds=elapsed,
                    trace_id=current_trace_id(),
                )
                logger.info(
                    "request completed",
                    extra={
                        "http_method": request.method,
                        "http_route": route,
                        "http_status": status,
                        "duration_ms": round(elapsed * 1000, 2),
                    },
                )


async def apply_faults() -> None:
    """Dependency that turns the injected faults into real behaviour.

    Mounted on the /api router only, so the health endpoints and the fault
    controls themselves stay reachable while the service is "broken".
    """
    base = SETTINGS.base_latency_seconds * _rng.uniform(0.6, 1.4)
    injected = FAULTS.next_delay_seconds()
    if injected > 0:
        with _tracer().start_as_current_span("fault.latency") as span:
            span.set_attribute("fault.delay_ms", round(injected * 1000))
            await asyncio.sleep(base + injected)
    else:
        await asyncio.sleep(base)

    status = FAULTS.next_error_status()
    if status is not None:
        raise HTTPException(status_code=status, detail="fault injection: upstream unavailable")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    configure_tracing(SETTINGS)
    logger.info(
        "service started",
        extra={
            "service_version": SETTINGS.service_version,
            "otlp_endpoint": SETTINGS.otlp_endpoint or "disabled",
            "trace_sample_ratio": SETTINGS.trace_sample_ratio,
        },
    )
    yield
    FAULTS.clear()


app = FastAPI(
    title="sample-app",
    version=SETTINGS.service_version,
    summary="Instrumented service used to exercise the alerting rules.",
    lifespan=lifespan,
)
app.add_middleware(ObservabilityMiddleware)

Faulty = Annotated[None, Depends(apply_faults)]


@app.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> Response:
    """Exposition endpoint with content negotiation.

    Prometheus asks for `application/openmetrics-text`, and only that encoding
    carries exemplars - the trace ids attached to the latency histogram that turn
    a spike on a graph into a link to the trace behind it. Mounting a prebuilt
    ASGI app here would also work, but it answers /metrics with a 307 to
    /metrics/ on every single scrape.
    """
    encoder, content_type = choose_encoder(request.headers.get("accept", ""))
    return Response(content=encoder(REGISTRY), media_type=content_type)


@app.get("/healthz", tags=["ops"])
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz", tags=["ops"])
async def readyz() -> dict[str, str]:
    return {"status": "ready"}


@app.get("/api/items", tags=["catalog"])
async def list_items(_: Faulty, limit: int = 10) -> list[Item]:
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=422, detail="limit must be within [1, 100]")
    return list(CATALOG.values())[:limit]


@app.get("/api/items/{item_id}", tags=["catalog"])
async def get_item(item_id: int, _: Faulty) -> Item:
    item = CATALOG.get(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"no item {item_id}")
    return item


@app.post("/api/orders", status_code=201, tags=["orders"])
async def create_order(payload: OrderRequest, _: Faulty) -> Order:
    tracer = _tracer()
    with tracer.start_as_current_span("catalog.lookup") as span:
        span.set_attribute("item.id", payload.item_id)
        item = CATALOG.get(payload.item_id)
        if item is None:
            raise HTTPException(status_code=404, detail=f"no item {payload.item_id}")

    # A second span with its own latency: without at least one child span a trace
    # is just a timestamp, and the Tempo panel has nothing to show.
    with tracer.start_as_current_span("inventory.reserve") as span:
        span.set_attribute("item.id", item.id)
        span.set_attribute("order.quantity", payload.quantity)
        await asyncio.sleep(_rng.uniform(0.004, 0.02))

    ORDERS_CREATED.inc()
    return Order(
        id=uuid.uuid4().hex[:12],
        item_id=item.id,
        quantity=payload.quantity,
        total_cents=item.price_cents * payload.quantity,
    )


@app.get("/faults", tags=["faults"])
async def read_faults() -> FaultView:
    return FaultView.of(FAULTS.state())


@app.post("/faults/latency", tags=["faults"])
async def set_latency_fault(payload: LatencyFaultRequest) -> FaultView:
    try:
        FAULTS.inject_latency(
            delay_ms=payload.delay_ms,
            jitter_ms=payload.jitter_ms,
            ttl_seconds=payload.ttl_seconds,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    logger.warning(
        "latency fault armed",
        extra={"delay_ms": payload.delay_ms, "ttl_seconds": payload.ttl_seconds},
    )
    return FaultView.of(FAULTS.state())


@app.post("/faults/errors", tags=["faults"])
async def set_error_fault(payload: ErrorFaultRequest) -> FaultView:
    try:
        FAULTS.inject_errors(
            ratio=payload.ratio,
            status_code=payload.status_code,
            ttl_seconds=payload.ttl_seconds,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    logger.warning(
        "error fault armed",
        extra={
            "ratio": payload.ratio,
            "status_code": payload.status_code,
            "ttl_seconds": payload.ttl_seconds,
        },
    )
    return FaultView.of(FAULTS.state())


@app.delete("/faults", tags=["faults"])
async def clear_faults() -> FaultView:
    FAULTS.clear()
    logger.info("faults cleared")
    return FaultView.of(FAULTS.state())
