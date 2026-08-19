"""A model-only baseline, for comparison against the full agent.

The question this exists to answer is uncomfortable and worth asking: does
sev0's investigation loop do anything a single well-briefed model call would
not? If handing the model a decent evidence package produces the same answers,
the loop is ceremony and the honest thing is to know that.

So the baseline is built to be strong, not to lose. It gets the same model, the
same incident, the same scoring, and an evidence package assembled by the same
collectors the agent uses: the metric summary, the onset, the failing service's
logs, the commits in the window, and the full source of every file those
commits touched. What it does not get is iteration. It cannot ask a follow-up
question, cannot retrieve anything it was not given, and cannot run code.

That gap is the thing being measured. Anything else that differed would
contaminate it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sev0.agent.state import Confidence, RootCause, RunState
from sev0.agent.tools import Toolbox

BASELINE_PROMPT = """\
You are an on-call software engineer diagnosing a production incident.

Below is an evidence package gathered for you: what the metrics show, when the
failure started, the logs around that time, the commits that landed just before
it, and the source of the files those commits touched.

You cannot gather more evidence and you cannot run anything. Read what is here
and name the single function responsible.

Answer with one call to `conclude`. If the evidence does not support a specific
symbol, say so in your explanation rather than guessing at one.
"""

MAX_SECTION_CHARS = 12_000


@dataclass
class EvidencePackage:
    """Everything the baseline is told, and where each part came from."""

    sections: dict[str, str]

    def render(self) -> str:
        parts = []
        for title, body in self.sections.items():
            text = body.strip() or "(nothing returned)"
            if len(text) > MAX_SECTION_CHARS:
                text = text[:MAX_SECTION_CHARS] + "\n... truncated ..."
            parts.append(f"## {title}\n\n{text}")
        return "\n\n".join(parts)


def gather(toolbox: Toolbox, incident: str, window_minutes: int = 60) -> EvidencePackage:
    """Assemble the package using the agent's own collectors.

    Deliberately generous. Everything the agent could have reached in its first
    few turns is here already, including the full text of any file a recent
    commit touched, which is more than a single retrieval call would return.
    """
    sections: dict[str, str] = {}

    sections["Alert"] = f"{incident}"
    sections["Metrics overview"] = _safe(toolbox, "metrics_overview", {})
    sections["Failure onset"] = _safe(toolbox, "find_onset", {})

    for service in ("gateway", "cart", "payments", "catalog"):
        logs = _safe(toolbox, "service_logs", {"service": service, "limit": 40})
        if logs and "no matching" not in logs.lower():
            sections[f"Logs: {service}"] = logs

    since = (datetime.now(UTC) - timedelta(minutes=window_minutes)).isoformat(timespec="seconds")
    commits = _safe(toolbox, "recent_commits", {"since": since, "limit": 25})
    sections["Recent commits"] = commits

    for path in _files_in(commits):
        source = _safe(toolbox, "file_outline", {"path": path})
        if source:
            sections[f"Outline: {path}"] = source

    return EvidencePackage(sections=sections)


def _safe(toolbox: Toolbox, tool: str, arguments: dict[str, Any]) -> str:
    """Call a collector, returning its message rather than raising.

    A baseline that dies because one collector was unreachable would score zero
    for a reason that has nothing to do with the comparison.
    """
    if tool not in toolbox._tools:  # noqa: SLF001 - same package, deliberate
        return ""
    result, _ = toolbox.invoke(tool, arguments)
    return result


def _files_in(commit_log: str) -> list[str]:
    """Source paths named anywhere in the commit listing."""
    seen: list[str] = []
    for line in commit_log.splitlines():
        candidate = line.strip()
        if candidate.endswith((".py", ".env", ".yml")) and candidate not in seen:
            seen.append(candidate)
    return seen[:6]


class StaticBaseline:
    """One model call against a fixed evidence package."""

    def __init__(
        self,
        client: Any,
        toolbox: Toolbox,
        state: RunState,
        model: str,
        max_tokens: int = 4096,
    ) -> None:
        self.client = client
        self.toolbox = toolbox
        self.state = state
        self.model = model
        self.max_tokens = max_tokens

    def run(self, incident: str) -> RunState:
        package = gather(self.toolbox, incident)

        # Evidence gathering is not part of what the baseline is being judged
        # on, so the collector calls it made are cleared before the model is
        # asked anything. Otherwise its tool-call count would read as effort
        # it did not spend.
        self.state.tool_calls.clear()

        conclude_schema = next(
            schema for schema in self.toolbox.schemas() if schema["name"] == "conclude"
        )

        response = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=BASELINE_PROMPT,
            tools=[conclude_schema],
            tool_choice={"type": "tool", "name": "conclude"},
            messages=[{"role": "user", "content": package.render()}],
        )
        self.state.usage.add(getattr(response, "usage", None))

        for block in response.content:
            if getattr(block, "type", None) == "tool_use":
                self.toolbox.invoke("conclude", dict(block.input))
                return self.state

        self.state.abandon("baseline returned no conclusion")
        return self.state


def package_for(repo: Path, toolbox: Toolbox, incident: str) -> str:
    """The rendered evidence package, for inspection without spending a call."""
    del repo
    return gather(toolbox, incident).render()


__all__ = [
    "BASELINE_PROMPT",
    "Confidence",
    "EvidencePackage",
    "RootCause",
    "StaticBaseline",
    "gather",
    "package_for",
]
