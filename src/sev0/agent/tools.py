"""The tools an investigation can call.

Two rules shape everything here. Every tool returns a string, because that is
what goes back into the conversation and a caller that has to guess at a nested
structure wastes turns. And a tool that fails returns the failure as text rather
than raising: a malformed regex should cost one turn and a correction, not the
whole run.

Nothing in this module writes to the repository under investigation.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sev0.agent.state import Confidence, RootCause, RunState, Verdict
from sev0.collectors import logs as log_collector
from sev0.collectors import metrics as metric_collector
from sev0.collectors.history import GitError, GitHistoryCollector
from sev0.retrieval import code

MAX_RESULT_CHARS = 6000


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict[str, Any]
    handler: Callable[..., str]

    def as_api_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.schema,
        }


def _obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
    }


def _truncate(text: str) -> str:
    if len(text) <= MAX_RESULT_CHARS:
        return text
    omitted = len(text) - MAX_RESULT_CHARS
    return text[:MAX_RESULT_CHARS] + f"\n... truncated, {omitted} chars omitted"


class Toolbox:
    """Binds the collectors to a single run."""

    def __init__(
        self,
        state: RunState,
        repo: Path,
        loki: log_collector.LokiCollector | None = None,
        prometheus: metric_collector.PrometheusCollector | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.state = state
        self.repo = Path(repo)
        self.loki = loki
        self.prometheus = prometheus
        self.now = now
        self._history: GitHistoryCollector | None = None
        self._tools = {tool.name: tool for tool in self._build()}

    @property
    def history(self) -> GitHistoryCollector:
        if self._history is None:
            self._history = GitHistoryCollector(self.repo)
        return self._history

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.as_api_schema() for tool in self._tools.values()]

    def invoke(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        """Run a tool. Returns (result, failed)."""
        tool = self._tools.get(name)
        if tool is None:
            known = ", ".join(sorted(self._tools))
            result = f"No tool named {name!r}. Available tools: {known}"
            self.state.record_call(name, arguments, result, failed=True)
            return result, True

        try:
            result = _truncate(tool.handler(**arguments))
            failed = False
        except TypeError as exc:
            result = f"Bad arguments for {name}: {exc}"
            failed = True
        except Exception as exc:  # noqa: BLE001 - surfaced to the model, not swallowed
            result = f"{type(exc).__name__}: {exc}"
            failed = True

        self.state.record_call(name, arguments, result, failed=failed)
        return result, failed

    # -- observability -----------------------------------------------------

    def _window(self, minutes: int) -> tuple[datetime, datetime]:
        end = self.now()
        return end - timedelta(minutes=minutes), end

    def _require_prometheus(self) -> metric_collector.PrometheusCollector:
        if self.prometheus is None:
            raise RuntimeError("Prometheus is not configured for this run")
        return self.prometheus

    def _require_loki(self) -> log_collector.LokiCollector:
        if self.loki is None:
            raise RuntimeError("Loki is not configured for this run")
        return self.loki

    def metrics_overview(self, minutes: int = 60) -> str:
        prometheus = self._require_prometheus()
        start, end = self._window(minutes)

        errors = prometheus.error_rate(start, end)
        traffic = prometheus.request_rate(start, end)
        latency = prometheus.latency_p95(start, end)

        by_service: dict[str, dict[str, float]] = {}
        for series in errors:
            by_service.setdefault(series.name, {})["error_rate_peak"] = series.max()
        for series in traffic:
            by_service.setdefault(series.name, {})["request_rate_peak"] = series.max()
        for series in latency:
            by_service.setdefault(series.name, {})["p95_seconds_peak"] = series.max()

        if not by_service:
            return f"No metric samples in the last {minutes} minutes."

        rows = [f"Metric peaks over the last {minutes} minutes:"]
        for service in sorted(by_service):
            values = by_service[service]
            rows.append(
                f"  {service:<10} "
                f"errors={values.get('error_rate_peak', 0.0):.3f} "
                f"rps={values.get('request_rate_peak', 0.0):.2f} "
                f"p95={values.get('p95_seconds_peak', 0.0):.3f}s"
            )
        return "\n".join(rows)

    def find_onset(self, threshold: float = 0.05, minutes: int = 60) -> str:
        prometheus = self._require_prometheus()
        start, end = self._window(minutes)

        series = prometheus.error_rate(start, end)
        onsets = [
            onset
            for onset in (metric_collector.find_onset(s, threshold) for s in series)
            if onset is not None
        ]
        if not onsets:
            return (
                f"No sustained error rate above {threshold:.0%} in the last {minutes} minutes. "
                "Either the incident is outside this window, or it does not show up as 5xx."
            )

        rows = ["Sustained error-rate onsets, earliest first:"]
        for onset in sorted(onsets, key=lambda o: o.at):
            rows.append(
                f"  {onset.series:<10} at {onset.at.isoformat(timespec='seconds')} "
                f"baseline={onset.baseline:.3f} peak={onset.peak:.3f}"
            )
        rows.append(
            "\nThe earliest onset is usually the service that reported the failure, "
            "which is not necessarily the service that caused it."
        )
        return "\n".join(rows)

    def failure_logs(self, minutes: int = 30, examples_per_shape: int = 2) -> str:
        loki = self._require_loki()
        start, end = self._window(minutes)

        lines = loki.failures(start, end)
        if not lines:
            return f"No 5xx responses logged in the last {minutes} minutes."

        counts = dict(log_collector.shape_counts(lines))
        kept = log_collector.summarise(lines, per_shape=examples_per_shape)

        rows = [f"{len(lines)} failing requests in the last {minutes} minutes."]
        rows.append(f"{len(counts)} distinct failure shapes. Examples:\n")
        rows.extend(f"  {item.render()}" for item in kept)
        return "\n".join(rows)

    def service_logs(self, service: str, minutes: int = 10, examples_per_shape: int = 2) -> str:
        loki = self._require_loki()
        start, end = self._window(minutes)

        lines = loki.query_range(f'{{service="{service}"}}', start, end)
        if not lines:
            return f"No logs from {service} in the last {minutes} minutes."

        kept = log_collector.summarise(lines, per_shape=examples_per_shape)
        rows = [f"{len(lines)} lines from {service}, deduplicated to {len(kept)} shapes:\n"]
        rows.extend(f"  {item.render()}" for item in kept)
        return "\n".join(rows)

    # -- history -----------------------------------------------------------

    def recent_commits(self, limit: int = 15) -> str:
        commits = self.history.recent(limit)
        if not commits:
            return "No commits found."
        return "\n".join(commit.render() for commit in commits)

    def commits_touching(self, path: str, limit: int = 10) -> str:
        commits = self.history.touching(path, limit)
        if not commits:
            return f"No commits touch {path}."
        return "\n".join(commit.render() for commit in commits)

    def show_commit(self, sha: str) -> str:
        commits = [c for c in self.history.recent(200) if c.sha.startswith(sha[:8])]
        header = commits[0].render() if commits else sha
        try:
            patch = self.history.diff(sha)
        except GitError as exc:
            return f"Could not read {sha}: {exc}"
        return f"{header}\n\n{patch}"

    def blame(self, path: str, start_line: int, end_line: int) -> str:
        rows = self.history.blame(path, start_line, end_line)
        return "\n".join(f"{sha}  {author:<20} {line}" for sha, author, line in rows)

    # -- code --------------------------------------------------------------

    def search_code(self, pattern: str, limit: int = 30) -> str:
        matches = code.search(self.repo, pattern, limit=limit)
        if not matches:
            return f"No matches for {pattern!r}."
        return "\n".join(
            f"{m.file}:{m.line_number}  [{m.symbol or 'module level'}]  {m.line}" for m in matches
        )

    def file_outline(self, path: str) -> str:
        rows = code.outline(self.repo / path, self.repo)
        if not rows:
            return f"{path} defines no functions or classes."
        return "\n".join(rows)

    def read_symbol(self, path: str, name: str) -> str:
        symbol = code.get_symbol(self.repo, path, name)
        return f"{symbol.location}\n\n{symbol.source}"

    # -- reasoning ---------------------------------------------------------

    def record_hypothesis(self, statement: str, verdict: str, reasoning: str = "") -> str:
        try:
            parsed = Verdict(verdict)
        except ValueError:
            allowed = ", ".join(v.value for v in Verdict)
            return f"Unknown verdict {verdict!r}. Use one of: {allowed}"

        self.state.add_hypothesis(statement, parsed, reasoning)
        return f"Recorded as {parsed.value}: {statement}"

    def conclude(
        self,
        service: str,
        file: str,
        symbol: str,
        commit: str,
        explanation: str,
        confidence: str = "low",
    ) -> str:
        try:
            parsed = Confidence(confidence)
        except ValueError:
            allowed = ", ".join(c.value for c in Confidence)
            return f"Unknown confidence {confidence!r}. Use one of: {allowed}"

        self.state.conclude(
            RootCause(
                service=service,
                file=file,
                symbol=symbol,
                commit=commit,
                explanation=explanation,
                confidence=parsed,
            )
        )
        return "Conclusion recorded. The investigation is complete."

    # -- registry ----------------------------------------------------------

    def _build(self) -> list[Tool]:
        minutes = {"type": "integer", "description": "How far back to look, in minutes."}

        return [
            Tool(
                "metrics_overview",
                "Peak error rate, request rate and p95 latency per service. Start here: it "
                "shows which services are affected and how badly.",
                _obj({"minutes": minutes}),
                self.metrics_overview,
            ),
            Tool(
                "find_onset",
                "Find when the error rate left its baseline and stayed there. Use the "
                "returned timestamp to bound which commits are worth reading.",
                _obj(
                    {
                        "threshold": {
                            "type": "number",
                            "description": "Error ratio that counts as broken, e.g. 0.05.",
                        },
                        "minutes": minutes,
                    }
                ),
                self.find_onset,
            ),
            Tool(
                "failure_logs",
                "Failing (5xx) log lines, deduplicated by shape, with the true count of each.",
                _obj(
                    {
                        "minutes": minutes,
                        "examples_per_shape": {
                            "type": "integer",
                            "description": "Examples to keep per distinct line shape.",
                        },
                    }
                ),
                self.failure_logs,
            ),
            Tool(
                "service_logs",
                "All log lines from one service, deduplicated by shape.",
                _obj(
                    {
                        "service": {
                            "type": "string",
                            "description": "Service name: gateway, cart, catalog or payments.",
                        },
                        "minutes": minutes,
                        "examples_per_shape": {"type": "integer"},
                    },
                    required=["service"],
                ),
                self.service_logs,
            ),
            Tool(
                "recent_commits",
                "Recent commits with author, subject and changed files.",
                _obj({"limit": {"type": "integer"}}),
                self.recent_commits,
            ),
            Tool(
                "commits_touching",
                "Commits that changed a specific file.",
                _obj(
                    {"path": {"type": "string"}, "limit": {"type": "integer"}},
                    required=["path"],
                ),
                self.commits_touching,
            ),
            Tool(
                "show_commit",
                "The full patch a commit introduced.",
                _obj({"sha": {"type": "string"}}, required=["sha"]),
                self.show_commit,
            ),
            Tool(
                "blame",
                "Which commit last touched each line in a range.",
                _obj(
                    {
                        "path": {"type": "string"},
                        "start_line": {"type": "integer"},
                        "end_line": {"type": "integer"},
                    },
                    required=["path", "start_line", "end_line"],
                ),
                self.blame,
            ),
            Tool(
                "search_code",
                "Regex search across the source, annotated with the enclosing function.",
                _obj(
                    {"pattern": {"type": "string"}, "limit": {"type": "integer"}},
                    required=["pattern"],
                ),
                self.search_code,
            ),
            Tool(
                "file_outline",
                "The functions and classes a file defines, with line numbers.",
                _obj({"path": {"type": "string"}}, required=["path"]),
                self.file_outline,
            ),
            Tool(
                "read_symbol",
                "The complete source of one function or class. Prefer this over reading a "
                "whole file.",
                _obj(
                    {"path": {"type": "string"}, "name": {"type": "string"}},
                    required=["path", "name"],
                ),
                self.read_symbol,
            ),
            Tool(
                "record_hypothesis",
                "Record a candidate cause, or update one you raised earlier. Record "
                "rejections too: a hypothesis you ruled out is evidence for the reviewer.",
                _obj(
                    {
                        "statement": {
                            "type": "string",
                            "description": "The claim, phrased so it could be shown false.",
                        },
                        "verdict": {
                            "type": "string",
                            "enum": [v.value for v in Verdict],
                        },
                        "reasoning": {
                            "type": "string",
                            "description": "The evidence behind this verdict.",
                        },
                    },
                    required=["statement", "verdict"],
                ),
                self.record_hypothesis,
            ),
            Tool(
                "conclude",
                "State the root cause and end the investigation. Only call this once the "
                "evidence identifies a specific symbol and the commit that changed it.",
                _obj(
                    {
                        "service": {"type": "string"},
                        "file": {"type": "string", "description": "Path relative to the repo."},
                        "symbol": {"type": "string", "description": "Function or class name."},
                        "commit": {"type": "string", "description": "The commit that caused it."},
                        "explanation": {
                            "type": "string",
                            "description": "Why this change produces the observed failure.",
                        },
                        "confidence": {
                            "type": "string",
                            "enum": [c.value for c in Confidence],
                        },
                    },
                    required=["service", "file", "symbol", "commit", "explanation"],
                ),
                self.conclude,
            ),
        ]


def render_schemas(toolbox: Toolbox) -> str:
    """Human-readable dump of the tool surface, for docs and debugging."""
    return json.dumps(toolbox.schemas(), indent=2)
