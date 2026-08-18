"""Log collection from Loki.

The hard part is not fetching, it is deciding what not to return. A minute of
storefront traffic is a few thousand lines and almost all of them say the same
thing. Handing that to a model wastes context and buries the signal, so lines
are grouped by shape and only representatives survive.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from sev0.collectors.models import LogLine

# Anything that varies per request and carries no diagnostic weight on its own.
VOLATILE = re.compile(
    r"""
    (\b[0-9a-f]{8,}\b)          # ids and hashes
    | (user-\d+)                 # generated user ids
    | (sku-\d+)                  # product ids
    | (\b\d+\.\d+\b)             # durations
    | (\b\d{3,}\b)               # large numbers
    """,
    re.VERBOSE,
)


class LokiError(RuntimeError):
    pass


def _shape(line: LogLine) -> str:
    """Collapse a line to its template so near-identical lines group together."""
    return f"{line.service}|{line.level}|{VOLATILE.sub('*', line.message)}|{line.status}"


class LokiCollector:
    def __init__(self, base_url: str, client: httpx.Client | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=15.0)

    def query_range(
        self,
        query: str,
        start: datetime,
        end: datetime,
        limit: int = 1000,
    ) -> list[LogLine]:
        response = self._client.get(
            f"{self.base_url}/loki/api/v1/query_range",
            params={
                "query": query,
                "start": str(int(start.timestamp() * 1e9)),
                "end": str(int(end.timestamp() * 1e9)),
                "limit": limit,
                "direction": "backward",
            },
        )
        if response.status_code != 200:
            raise LokiError(f"loki returned {response.status_code}: {response.text[:200]}")

        payload = response.json()
        if payload.get("status") != "success":
            raise LokiError(f"loki query failed: {payload}")

        return _parse_streams(payload["data"]["result"])

    def failures(
        self,
        start: datetime,
        end: datetime,
        services: tuple[str, ...] = (),
        limit: int = 1000,
    ) -> list[LogLine]:
        """Every 5xx in the window, newest first."""
        selector = '{status=~"5.."}'
        if services:
            selector = f'{{status=~"5..", service=~"{"|".join(services)}"}}'
        return self.query_range(selector, start, end, limit=limit)

    def around(
        self,
        service: str,
        moment: datetime,
        window: timedelta = timedelta(minutes=2),
        limit: int = 500,
    ) -> list[LogLine]:
        """Everything one service said either side of a moment."""
        return self.query_range(
            f'{{service="{service}"}}',
            moment - window,
            moment + window,
            limit=limit,
        )


def summarise(lines: list[LogLine], per_shape: int = 2) -> list[LogLine]:
    """Keep a couple of examples of each distinct line shape.

    Returned in time order, oldest first, because the first occurrence of a
    failure is usually the most informative one.
    """
    seen: Counter[str] = Counter()
    kept: list[LogLine] = []

    for line in sorted(lines, key=lambda item: item.timestamp):
        shape = _shape(line)
        if seen[shape] < per_shape:
            kept.append(line)
        seen[shape] += 1

    return kept


def shape_counts(lines: list[LogLine]) -> list[tuple[str, int]]:
    """How many times each distinct line shape occurred, most frequent first."""
    counts: Counter[str] = Counter(_shape(line) for line in lines)
    return counts.most_common()


def _parse_streams(streams: list[dict[str, Any]]) -> list[LogLine]:
    lines: list[LogLine] = []

    for stream in streams:
        labels = {str(k): str(v) for k, v in stream.get("stream", {}).items()}
        for entry in stream.get("values", []):
            timestamp_ns, raw = entry
            line = _parse_line(labels, int(timestamp_ns), raw)
            if line is not None:
                lines.append(line)

    return lines


def _parse_line(labels: dict[str, str], timestamp_ns: int, raw: str) -> LogLine | None:
    when = datetime.fromtimestamp(timestamp_ns / 1e9, tz=UTC)

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        # Container logs that are not ours -- Postgres, Grafana, a stack trace
        # frame. Keep them; a traceback is often the most useful thing present.
        return LogLine(
            timestamp=when,
            service=labels.get("service", "unknown"),
            level=labels.get("level", "INFO"),
            message=raw.strip(),
        )

    if not isinstance(payload, dict):
        return None

    status = payload.get("status")
    return LogLine(
        timestamp=when,
        service=str(payload.get("service", labels.get("service", "unknown"))),
        level=str(payload.get("level", labels.get("level", "INFO"))),
        message=str(payload.get("message", "")),
        status=int(status) if isinstance(status, (int, str)) and str(status).isdigit() else None,
        path=payload.get("path"),
        fields={
            key: value
            for key, value in payload.items()
            if key not in {"ts", "level", "service", "message", "status", "path"}
        },
    )
