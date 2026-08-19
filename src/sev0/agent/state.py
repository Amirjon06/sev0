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

# Tools that run code rather than read it. Counted separately because an
# investigation that never executed anything only ever formed opinions.
EXPERIMENT_TOOLS = frozenset({"run_tests", "run_snippet", "try_patch"})


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
class ProposedFix:
    """A candidate patch and what happened when it was actually run."""

    path: str
    find: str
    replace: str
    rationale: str
    verified: bool
    verification: str


@dataclass
class RootCause:
    service: str
    file: str
    symbol: str
    commit: str
    explanation: str
    confidence: Confidence = Confidence.LOW


@dataclass
class Usage:
    """What the run cost, as reported by the provider.

    Every field here comes from a response the API actually returned. Cost is
    the one derived number and it is only populated when a price for the model
    is known; a plausible-looking figure nobody can check is worse than a
    missing one, because it will end up in a table someone quotes.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    requests: int = 0
    cost_usd: float | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def add(self, raw: Any) -> None:
        """Fold one response's usage block in. Unknown shapes are ignored."""
        if raw is None:
            return

        self.requests += 1
        self.input_tokens += int(getattr(raw, "input_tokens", 0) or 0)
        self.output_tokens += int(getattr(raw, "output_tokens", 0) or 0)
        self.cache_read_tokens += int(getattr(raw, "cache_read_input_tokens", 0) or 0)
        self.cache_write_tokens += int(getattr(raw, "cache_creation_input_tokens", 0) or 0)


@dataclass
class RunState:
    incident: str
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))
    finished_at: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    hypotheses: list[Hypothesis] = field(default_factory=list)
    root_cause: RootCause | None = None
    proposed_fix: ProposedFix | None = None
    stopped_because: str | None = None

    # How this run was configured. A result nobody can attribute to a model,
    # a mode, and a revision of the code is not reproducible, and an
    # unreproducible number does not belong in a table.
    model: str = ""
    mode: str = "full"
    scenario: str = ""
    trial: int = 1
    sev0_commit: str = ""
    usage: Usage = field(default_factory=Usage)

    @property
    def call_count(self) -> int:
        return len(self.tool_calls)

    @property
    def experiments(self) -> int:
        """Tool calls that actually executed something, rather than read."""
        return sum(1 for call in self.tool_calls if call.name in EXPERIMENT_TOOLS)

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

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> RunState:
        """Rebuild a run from its saved trace, so old runs can be re-scored."""
        state = cls(
            incident=payload["incident"],
            run_id=payload.get("run_id", ""),
            started_at=payload.get("started_at", ""),
            finished_at=payload.get("finished_at"),
            stopped_because=payload.get("stopped_because"),
            model=payload.get("model", ""),
            mode=payload.get("mode", "full"),
            scenario=payload.get("scenario", ""),
            trial=payload.get("trial", 1),
            sev0_commit=payload.get("sev0_commit", ""),
            usage=Usage(**payload.get("usage", {})),
        )
        state.tool_calls = [ToolCall(**call) for call in payload.get("tool_calls", [])]
        state.hypotheses = [
            Hypothesis(
                statement=raw["statement"],
                verdict=Verdict(raw["verdict"]),
                reasoning=raw.get("reasoning", ""),
                raised_at=raw.get("raised_at", ""),
            )
            for raw in payload.get("hypotheses", [])
        ]
        if raw_cause := payload.get("root_cause"):
            state.root_cause = RootCause(
                **{**raw_cause, "confidence": Confidence(raw_cause["confidence"])}
            )
        if raw_fix := payload.get("proposed_fix"):
            state.proposed_fix = ProposedFix(**raw_fix)
        return state

    @classmethod
    def load(cls, trace: Path) -> RunState:
        return cls.from_dict(json.loads(trace.read_text()))

    def save(self, run_dir: Path) -> Path:
        directory = run_dir / self.run_id
        directory.mkdir(parents=True, exist_ok=True)

        trace = directory / "run.json"
        trace.write_text(json.dumps(self.to_dict(), indent=2) + "\n")
        return trace

    def summary(self) -> str:
        lines = [
            f"run {self.run_id}  incident={self.incident}",
            f"tool calls: {self.call_count} ({self.experiments} experiments)",
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

        if self.proposed_fix is not None:
            mark = "verified" if self.proposed_fix.verified else "NOT verified"
            lines.append(f"fix: {self.proposed_fix.path} ({mark})")

        return "\n".join(lines)
