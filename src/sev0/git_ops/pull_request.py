"""The pull request, which is the actual deliverable.

A diff on its own asks the reviewer to trust the author. The point of this file
is that the reviewer never has to: the body carries what was observed, what was
considered and discarded, what was executed, and what the test suite did before
and after. Someone can disagree with the conclusion and still audit how it was
reached.

Rejected hypotheses are included on purpose. A pull request that shows only the
answer looks more confident and is worth less.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from sev0.agent.state import RunState, Verdict

HEADER = "## What broke"
FOOTER = (
    "---\n\n"
    "Opened by [sev0](https://github.com/Amirjon06/sev0). "
    "Every claim above is reproducible from the run trace."
)


class PullRequestError(RuntimeError):
    pass


@dataclass(frozen=True)
class PullRequest:
    title: str
    body: str
    branch: str
    base: str
    url: str | None = None


def _bullet(text: str) -> str:
    return "\n".join(f"  {line}" if line else "" for line in text.strip().splitlines())


def render_body(state: RunState, diff: str = "", trace_path: str = "") -> str:
    sections: list[str] = []

    root = state.root_cause
    if root is not None:
        sections.append(
            f"{HEADER}\n\n"
            f"`{root.file}` in the **{root.service}** service, in `{root.symbol}`.\n\n"
            f"{root.explanation.strip()}\n\n"
            f"Introduced by `{root.commit}`. Confidence: **{root.confidence.value}**."
        )
    else:
        sections.append(f"{HEADER}\n\nNo root cause was established.")

    considered = [h for h in state.hypotheses if h.verdict is not Verdict.CONFIRMED]
    if considered:
        lines = ["## What was ruled out\n"]
        for hypothesis in considered:
            mark = "rejected" if hypothesis.verdict is Verdict.REJECTED else "left open"
            lines.append(f"- **{hypothesis.statement}** — {mark}")
            if hypothesis.reasoning:
                lines.append(_bullet(hypothesis.reasoning))
        sections.append("\n".join(lines))

    fix = state.proposed_fix
    if fix is not None:
        verdict = "verified" if fix.verified else "**not verified**"
        body = [f"## The fix\n\n{fix.rationale.strip() or 'No rationale recorded.'}\n"]
        body.append(f"Verification: {verdict}\n")
        body.append("```\n" + fix.verification.strip() + "\n```")
        sections.append("\n".join(body))

    evidence = [
        "## How this was reached\n",
        f"- {state.call_count} tool calls, of which **{state.experiments} executed code**",
    ]
    if state.started_at and state.finished_at:
        evidence.append(f"- Started {state.started_at}, finished {state.finished_at}")
    if trace_path:
        evidence.append(f"- Full reasoning trace: `{trace_path}`")
    sections.append("\n".join(evidence))

    if diff.strip():
        sections.append(
            "<details>\n<summary>Diff</summary>\n\n"
            "```diff\n" + diff.strip() + "\n```\n\n</details>"
        )

    sections.append(FOOTER)
    return "\n\n".join(sections)


def render_title(state: RunState) -> str:
    root = state.root_cause
    if root is None:
        return f"sev0: investigation of {state.incident} (no root cause)"
    return f"fix({root.service}): Repair {root.symbol} for {state.incident}"


def build(
    state: RunState,
    branch: str,
    base: str,
    diff: str = "",
    trace_path: str = "",
) -> PullRequest:
    return PullRequest(
        title=render_title(state),
        body=render_body(state, diff=diff, trace_path=trace_path),
        branch=branch,
        base=base,
    )


def open_on_github(
    request: PullRequest,
    repository: str,
    token: str | None = None,
    draft: bool = True,
) -> PullRequest:
    """Open the pull request. Never merges it, and never pushes to the base.

    Draft by default. A machine-authored change arriving as ready-to-merge
    invites the reviewer to skim it, which is the opposite of the point.
    """
    token = token or os.getenv("GITHUB_TOKEN")
    if not token:
        raise PullRequestError("GITHUB_TOKEN is not set")
    if request.branch == request.base:
        raise PullRequestError("refusing to open a pull request from the base branch")

    try:
        from github import Auth, Github
    except ImportError as exc:  # pragma: no cover - declared dependency
        raise PullRequestError("PyGithub is not installed") from exc

    client = Github(auth=Auth.Token(token))
    try:
        remote: Any = client.get_repo(repository)
        created = remote.create_pull(
            title=request.title,
            body=request.body,
            head=request.branch,
            base=request.base,
            draft=draft,
        )
    except Exception as exc:  # noqa: BLE001 - the API message is what helps
        raise PullRequestError(f"could not open the pull request: {exc}") from exc
    finally:
        client.close()

    return PullRequest(
        title=request.title,
        body=request.body,
        branch=request.branch,
        base=request.base,
        url=created.html_url,
    )
