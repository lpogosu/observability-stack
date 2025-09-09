"""Process configuration, read once at import time from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be within [{minimum}, {maximum}], got {value}")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    service_name: str
    service_version: str
    environment: str
    otlp_endpoint: str
    trace_sample_ratio: float
    base_latency_seconds: float

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            service_name=_env_str("OTEL_SERVICE_NAME", "sample-app"),
            service_version=_env_str("SERVICE_VERSION", "0.1.0"),
            environment=_env_str("DEPLOY_ENVIRONMENT", "demo"),
            # Empty endpoint disables the exporter entirely, which is what the
            # unit tests and `ruff`/`mypy` runs want: no background gRPC threads,
            # no connection retries polluting the log.
            otlp_endpoint=os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip(),
            trace_sample_ratio=_env_float(
                "OTEL_TRACES_SAMPLER_ARG", 1.0, minimum=0.0, maximum=1.0
            ),
            # A floor under handler latency so the histogram has a realistic shape
            # instead of every request landing in the first bucket.
            base_latency_seconds=_env_float(
                "BASE_LATENCY_SECONDS", 0.02, minimum=0.0, maximum=5.0
            ),
        )
