"""Run state: what the agent did, and why it believed what it concluded.

Every investigation writes its full trace to disk. That is not logging for its
own sake — the trace is the deliverable. A root cause with no evidence behind it
is an assertion, and an assertion from a model is worth less than nothing to the
engineer who has to decide whether to merge the fix.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any


class Verdict(StrEnum):
    PROPOSED = "proposed"
    REJECTED = "rejected"
    CONFIRMED = "confirmed"


class Confidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass
class Hypothesis:
    statement: str
    verdict: Verdict = Verdict.PROPOSED
    reasoning: str = ""
    raised_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]
    result: str
    failed: bool = False
    at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))


@dataclass
class RootCause:
    service: str
    file: str
    symbol: str
    commit: str
    explanation: str
    confidence: Confidence = Confidence.LOW


@dataclass
class RunState:
    incident: str
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))
    finished_at: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    hypotheses: list[Hypothesis] = field(default_factory=list)
    root_cause: RootCause | None = None
    stopped_because: str | None = None

    @property
    def call_count(self) -> int:
        return len(self.tool_calls)

    @property
    def rejected(self) -> list[Hypothesis]:
        return [h for h in self.hypotheses if h.verdict is Verdict.REJECTED]

    def record_call(self, name: str, arguments: dict[str, Any], result: str, failed: bool) -> None:
        self.tool_calls.append(
            ToolCall(name=name, arguments=arguments, result=result, failed=failed)
        )

    def add_hypothesis(self, statement: str, verdict: Verdict, reasoning: str) -> Hypothesis:
        """Record a hypothesis, or update one already raised.

        Matching on the statement is what lets the model come back and reject
        an idea it proposed twenty calls earlier without creating a duplicate.
        """
        for existing in self.hypotheses:
            if existing.statement.strip().lower() == statement.strip().lower():
                existing.verdict = verdict
                existing.reasoning = reasoning or existing.reasoning
                return existing

        raised = Hypothesis(statement=statement, verdict=verdict, reasoning=reasoning)
        self.hypotheses.append(raised)
        return raised

    def conclude(self, root_cause: RootCause, reason: str = "concluded") -> None:
        self.root_cause = root_cause
        self.stopped_because = reason
        self.finished_at = datetime.now(UTC).isoformat(timespec="seconds")

    def abandon(self, reason: str) -> None:
        self.stopped_because = reason
        self.finished_at = datetime.now(UTC).isoformat(timespec="seconds")

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if self.root_cause is not None:
            payload["root_cause"]["confidence"] = self.root_cause.confidence.value
        for raw, hypothesis in zip(payload["hypotheses"], self.hypotheses, strict=True):
            raw["verdict"] = hypothesis.verdict.value
        return payload

    def save(self, run_dir: Path) -> Path:
        directory = run_dir / self.run_id
        directory.mkdir(parents=True, exist_ok=True)

        trace = directory / "run.json"
        trace.write_text(json.dumps(self.to_dict(), indent=2) + "\n")
        return trace

    def summary(self) -> str:
        lines = [
            f"run {self.run_id}  incident={self.incident}",
            f"tool calls: {self.call_count}",
        ]

        for hypothesis in self.hypotheses:
            mark = {
                Verdict.CONFIRMED: "confirmed",
                Verdict.REJECTED: "rejected",
                Verdict.PROPOSED: "open",
            }[hypothesis.verdict]
            lines.append(f"  [{mark}] {hypothesis.statement}")

        if self.root_cause is not None:
            rc = self.root_cause
            lines.append(
                f"root cause: {rc.file}::{rc.symbol} in {rc.service} "
                f"({rc.commit}, confidence {rc.confidence.value})"
            )
        else:
            lines.append(f"no root cause: {self.stopped_because}")

        return "\n".join(lines)
