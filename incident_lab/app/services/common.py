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


def instrument(app: FastAPI, service_name: str) -> None:
    """Attach request logging and a health endpoint to a service."""
    log = get_logger(service_name)

    @app.middleware("http")
    async def log_requests(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("x-request-id", str(uuid.uuid4())[:8])
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            log.exception(
                "request failed",
                extra={
                    "extra": {
                        "request_id": request_id,
                        "path": request.url.path,
                        "method": request.method,
                        "status": 500,
                        "duration_ms": duration_ms,
                    }
                },
            )
            raise

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        level = log.warning if response.status_code >= 500 else log.info
        level(
            "request completed",
            extra={
                "extra": {
                    "request_id": request_id,
                    "path": request.url.path,
                    "method": request.method,
                    "status": response.status_code,
                    "duration_ms": duration_ms,
                }
            },
        )
        response.headers["x-request-id"] = request_id
        return response

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": service_name}
