"""Tracing and structured logging.

Logs and traces are correlated by writing the active trace id into every log
line. Grafana's Loki datasource turns that field into a link to Tempo, which is
what makes the "logs and traces" dashboard a navigation tool rather than two
panels that happen to share a screen.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, Final

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import Span, Tracer

from app.config import Settings

_INVALID_TRACE_ID: Final = 0x00000000000000000000000000000000

# LogRecord attributes that are part of the logging machinery rather than the
# message; anything outside this set was passed by the caller via `extra=`.
# `color_message` is uvicorn's ANSI-escaped copy of its own message and has no
# business in a structured log line.
_RESERVED_LOG_FIELDS: Final = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"message", "asctime", "taskName", "color_message"}


def current_trace_id() -> str | None:
    """Hex trace id of the active span, or None when nothing is being traced."""
    context = trace.get_current_span().get_span_context()
    if context.trace_id == _INVALID_TRACE_ID:
        return None
    return format(context.trace_id, "032x")


def current_span_id() -> str | None:
    context = trace.get_current_span().get_span_context()
    if context.span_id == 0:
        return None
    return format(context.span_id, "016x")


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with trace context folded in.

    Promtail parses these into Loki labels; see promtail/promtail-config.yml.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        trace_id = current_trace_id()
        if trace_id is not None:
            payload["trace_id"] = trace_id
            span_id = current_span_id()
            if span_id is not None:
                payload["span_id"] = span_id
        for key, value in record.__dict__.items():
            if key not in _RESERVED_LOG_FIELDS:
                payload[key] = value
        if record.exc_info is not None:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    # uvicorn installs its own coloured handlers; propagating to ours instead
    # keeps every line in the same JSON shape for Promtail.
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True
    # The observability middleware already emits one structured line per request,
    # with the route template, the status and the trace id. uvicorn's access log
    # would duplicate every one of them in a shape Promtail cannot parse.
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.handlers = []
    access_logger.propagate = False


def configure_tracing(settings: Settings) -> Tracer:
    """Install a TracerProvider and return the application tracer.

    Sampling is head-based: the decision is made once, at the root span, and
    inherited by every child. That keeps the ratio honest across services, at the
    cost of not being able to keep "all traces that ended in an error" - that
    needs tail sampling in a collector, which is out of scope here and discussed
    in the README.
    """
    resource = Resource.create(
        {
            "service.name": settings.service_name,
            "service.version": settings.service_version,
            "deployment.environment": settings.environment,
        }
    )
    provider = TracerProvider(
        resource=resource,
        sampler=ParentBased(TraceIdRatioBased(settings.trace_sample_ratio)),
    )
    if settings.otlp_endpoint:
        exporter = OTLPSpanExporter(endpoint=f"{settings.otlp_endpoint}/v1/traces")
        provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return trace.get_tracer(settings.service_name, settings.service_version)


def annotate_error(span: Span, exc: BaseException) -> None:
    span.record_exception(exc)
    span.set_status(trace.Status(trace.StatusCode.ERROR, str(exc)))
