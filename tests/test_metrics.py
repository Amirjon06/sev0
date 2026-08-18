"""Metrics exposition tests.

The cardinality assertion matters more than it looks. If routes were labelled
with the concrete path, every shopper would create a new time series and
Prometheus would fall over well before the benchmark suite finished.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from services.catalog.main import app

client = TestClient(app)


def test_metrics_endpoint_exposes_prometheus_text() -> None:
    # A histogram emits no bucket series until something has been observed, so
    # drive one request before asking for the exposition.
    client.get("/products")

    response = client.get("/metrics")
    assert response.status_code == 200
    assert "http_requests_total{" in response.text
    assert "http_request_duration_seconds_bucket" in response.text


def test_routes_are_labelled_with_the_template_not_the_value() -> None:
    client.get("/products/sku-001")
    body = client.get("/metrics").text

    assert 'route="/products/{product_id}"' in body
    assert 'route="/products/sku-001"' not in body


def test_failed_lookups_are_counted_with_their_status() -> None:
    client.get("/products/does-not-exist")
    body = client.get("/metrics").text

    assert 'status="404"' in body


def test_health_endpoint_reports_the_service_name() -> None:
    payload = client.get("/healthz").json()
    assert payload == {"status": "ok", "service": "catalog"}
