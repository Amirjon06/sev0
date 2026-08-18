"""Shapes the collectors return.

Everything here is deliberately plain data. The agent loop should never touch a
Loki response body or a git porcelain string; it gets these instead, so the
prompt has a stable contract and the collectors can be tested without a model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class LogLine:
    timestamp: datetime
    service: str
    level: str
    message: str
    status: int | None = None
    path: str | None = None
    fields: dict[str, Any] = field(default_factory=dict)

    def render(self) -> str:
        parts = [
            self.timestamp.strftime("%H:%M:%S"),
            self.service,
            self.level,
            self.message,
        ]
        if self.status is not None:
            parts.append(f"status={self.status}")
        if self.path:
            parts.append(self.path)
        return "  ".join(parts)


@dataclass(frozen=True)
class MetricPoint:
    timestamp: datetime
    value: float


@dataclass(frozen=True)
class Series:
    labels: dict[str, str]
    points: tuple[MetricPoint, ...]

    @property
    def name(self) -> str:
        return self.labels.get("service") or self.labels.get("__name__") or "series"

    def max(self) -> float:
        return max((p.value for p in self.points), default=0.0)


@dataclass(frozen=True)
class Onset:
    """When a metric first crossed a threshold and stayed there."""

    series: str
    at: datetime
    baseline: float
    peak: float


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    author: str
    email: str
    committed_at: datetime
    subject: str
    body: str
    files: tuple[str, ...]

    def render(self) -> str:
        when = self.committed_at.strftime("%Y-%m-%d %H:%M")
        files = ", ".join(self.files[:5])
        if len(self.files) > 5:
            files += f" (+{len(self.files) - 5} more)"
        return f"{self.sha}  {when}  {self.author}  {self.subject}  [{files}]"
