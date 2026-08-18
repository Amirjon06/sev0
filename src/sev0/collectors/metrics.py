"""Metric collection from Prometheus, and onset detection.

Onset is the point of this module. "The site is broken" is not actionable; "the
error rate left its baseline at 21:47:30" is, because it turns an open-ended
investigation into a bounded question: what changed just before that.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from sev0.collectors.models import MetricPoint, Onset, Series

ERROR_RATE = (
    'sum by (service) (rate(http_requests_total{{status=~"5.."}}[{window}])) '
    "/ sum by (service) (rate(http_requests_total[{window}]))"
)

REQUEST_RATE = "sum by (service) (rate(http_requests_total[{window}]))"

LATENCY_P95 = (
    "histogram_quantile(0.95, sum by (service, le) "
    "(rate(http_request_duration_seconds_bucket[{window}])))"
)


class PrometheusError(RuntimeError):
    pass


class PrometheusCollector:
    def __init__(self, base_url: str, client: httpx.Client | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=15.0)

    def query_range(
        self,
        query: str,
        start: datetime,
        end: datetime,
        step_seconds: int = 15,
    ) -> list[Series]:
        response = self._client.get(
            f"{self.base_url}/api/v1/query_range",
            params={
                "query": query,
                "start": start.timestamp(),
                "end": end.timestamp(),
                "step": step_seconds,
            },
        )
        if response.status_code != 200:
            raise PrometheusError(
                f"prometheus returned {response.status_code}: {response.text[:200]}"
            )

        payload = response.json()
        if payload.get("status") != "success":
            raise PrometheusError(f"prometheus query failed: {payload}")

        return _parse_matrix(payload["data"]["result"])

    def error_rate(
        self,
        start: datetime,
        end: datetime,
        rate_window: str = "1m",
        step_seconds: int = 15,
    ) -> list[Series]:
        return self.query_range(ERROR_RATE.format(window=rate_window), start, end, step_seconds)

    def request_rate(
        self,
        start: datetime,
        end: datetime,
        rate_window: str = "1m",
        step_seconds: int = 15,
    ) -> list[Series]:
        return self.query_range(REQUEST_RATE.format(window=rate_window), start, end, step_seconds)

    def latency_p95(
        self,
        start: datetime,
        end: datetime,
        rate_window: str = "5m",
        step_seconds: int = 15,
    ) -> list[Series]:
        return self.query_range(LATENCY_P95.format(window=rate_window), start, end, step_seconds)


def find_onset(
    series: Series,
    threshold: float,
    sustain: int = 2,
) -> Onset | None:
    """First point above `threshold` that stays there for `sustain` points.

    A single spike is usually a deploy, a restart, or one unlucky request.
    Requiring the breach to persist is what separates an incident from noise,
    and it is the difference between the agent investigating a real regression
    and chasing a blip.
    """
    points = series.points
    if len(points) < sustain:
        return None

    for index, point in enumerate(points):
        if point.value <= threshold:
            continue

        window = points[index : index + sustain]
        if len(window) < sustain or any(p.value <= threshold for p in window):
            continue

        baseline = points[:index]
        return Onset(
            series=series.name,
            at=point.timestamp,
            baseline=sum(p.value for p in baseline) / len(baseline) if baseline else 0.0,
            peak=max(p.value for p in points[index:]),
        )

    return None


def first_onset(series: list[Series], threshold: float, sustain: int = 2) -> Onset | None:
    """The earliest onset across several series.

    With a fan-out architecture the caller sees the failure first, so the
    earliest onset is usually the alerting service rather than the broken one.
    That is a starting point, not an answer.
    """
    onsets = [o for o in (find_onset(s, threshold, sustain) for s in series) if o is not None]
    if not onsets:
        return None
    return min(onsets, key=lambda o: o.at)


def incident_window(
    onset: Onset,
    before: timedelta = timedelta(minutes=15),
    after: timedelta = timedelta(minutes=5),
) -> tuple[datetime, datetime]:
    """A search window around an onset, weighted towards what came before it."""
    return onset.at - before, onset.at + after


def _parse_matrix(result: list[dict[str, Any]]) -> list[Series]:
    series: list[Series] = []

    for entry in result:
        labels = {str(k): str(v) for k, v in entry.get("metric", {}).items()}
        points: list[MetricPoint] = []

        for raw_ts, raw_value in entry.get("values", []):
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                continue
            if math.isnan(value) or math.isinf(value):
                # Prometheus renders a ratio with an empty denominator as the
                # string "NaN", which float() happily accepts. Left in, it
                # would poison the baseline average in onset detection.
                continue
            points.append(
                MetricPoint(
                    timestamp=datetime.fromtimestamp(float(raw_ts), tz=UTC),
                    value=value,
                )
            )

        series.append(Series(labels=labels, points=tuple(points)))

    return series
