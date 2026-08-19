"""Shared helpers for the demo storefront services.

Kept deliberately small. Every service emits the same structured log shape so
that a log collector can filter by service, level, and request without needing
per-service parsing rules.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

REQUESTS = Counter(
    "http_requests_total",
    "Total HTTP requests handled",
    ["service", "method", "route", "status"],
)

LATENCY = Histogram(
    "http_request_duration_seconds",
    "Request duration in seconds",
    ["service", "method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

IN_FLIGHT = Gauge(
    "http_requests_in_flight",
    "Requests currently being handled",
    ["service"],
)

# Business metrics. A fault that produces a wrong number rather than an
# exception leaves the HTTP metrics untouched, and without these there is no
# signal at all for an entire class of real bug.
ORDERS = Counter(
    "storefront_orders_total",
    "Orders priced, labelled by whether they qualified for free shipping",
    ["free_shipping"],
)

ORDER_CENTS = Counter(
    "storefront_order_cents_total",
    "Money moved through pricing, by component",
    ["component"],
)

CHARGES = Counter(
    "storefront_charges_total",
    "Charge attempts by outcome",
    ["outcome"],
)


def env_int(name: str, default: int) -> int:
    """Read an integer setting, falling back rather than crashing on nonsense.

    A service that refuses to boot because someone typed a stray character into
    a config value fails in a way nothing downstream can diagnose.
    """
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "service": os.getenv("SERVICE_NAME", "unknown"),
            "message": record.getMessage(),
        }
        if extra := getattr(record, "extra", None):
            payload.update(extra)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def get_logger(name: str) -> logging.Logger:
    """Return a logger writing JSON lines to stdout."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))
    return logger


def service_url(name: str) -> str:
    """Resolve a sibling service's base URL from the environment."""
    return os.environ[f"{name.upper()}_URL"]


def route_of(request: Request) -> str:
    # The templated path, not the concrete one. /carts/{user_id} rather than
    # /carts/user-0042, or the metric explodes into one series per shopper.
    route = request.scope.get("route")
    return getattr(route, "path", "unmatched")


def instrument(app: FastAPI, service_name: str) -> None:
    """Attach request logging, metrics, and a health endpoint to a service."""
    log = get_logger(service_name)

    @app.middleware("http")
    async def log_requests(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("x-request-id", str(uuid.uuid4())[:8])
        started = time.perf_counter()
        IN_FLIGHT.labels(service_name).inc()

        try:
            response = await call_next(request)
        except Exception:
            duration = time.perf_counter() - started
            route = route_of(request)
            REQUESTS.labels(service_name, request.method, route, "500").inc()
            LATENCY.labels(service_name, request.method, route).observe(duration)
            log.exception(
                "request failed",
                extra={
                    "extra": {
                        "request_id": request_id,
                        "path": request.url.path,
                        "method": request.method,
                        "status": 500,
                        "duration_ms": round(duration * 1000, 2),
                    }
                },
            )
            raise
        finally:
            IN_FLIGHT.labels(service_name).dec()

        duration = time.perf_counter() - started
        route = route_of(request)
        REQUESTS.labels(service_name, request.method, route, str(response.status_code)).inc()
        LATENCY.labels(service_name, request.method, route).observe(duration)

        level = log.warning if response.status_code >= 500 else log.info
        level(
            "request completed",
            extra={
                "extra": {
                    "request_id": request_id,
                    "path": request.url.path,
                    "method": request.method,
                    "status": response.status_code,
                    "duration_ms": round(duration * 1000, 2),
                }
            },
        )
        response.headers["x-request-id"] = request_id
        return response

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": service_name}

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
