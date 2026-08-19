"""The investigation loop.

Deliberately small. The interesting behaviour lives in the tools and in the
system prompt; a loop that tries to be clever about what the model should do
next just fights it. What this does own is the things a model must not be
trusted with: a hard call budget, a stop condition, and a record of everything
that happened.
"""

from __future__ import annotations

from typing import Any, Protocol

from sev0.agent.state import RunState
from sev0.agent.tools import Toolbox

SYSTEM_PROMPT = """\
You are an on-call software engineer diagnosing a production incident.

You are given an alert and nothing else. Investigate using the tools until you
can name the specific function that is failing and the commit that changed it.

How to work:

- Start with metrics to see the shape and size of the failure, then find when it
  started. That timestamp bounds which commits are worth reading.
- The service that reports errors is often not the service that caused them. A
  gateway returning 500 may be faithfully relaying a failure from behind it.
- Read logs for the failing service around the onset. Tracebacks and error
  messages usually name the file and line directly.
- Correlate against commits landing shortly before the onset. Read the actual
  patch, not just the subject line: commit messages describe intent, and the
  bug is where intent and behaviour diverged.
- Prefer reading a single function over a whole file.

How to test what you think:

You can run code. Use it. A hypothesis you have not executed is a guess, however
well it reads.

- Record a hypothesis, then test it. `run_snippet` executes Python against a
  throwaway copy of the source: call the suspect function with the suspect input
  and see whether it actually raises. `run_tests` shows which assertions the
  failure breaks.
- If the experiment does not reproduce the failure, reject the hypothesis and
  say what the result was. That is progress, not a dead end.
- Once you believe you have the cause, `try_patch` proves it. The failure has to
  reproduce first, then your change is applied and the suite re-run. A patch that
  fixes one test and breaks another is not a fix.
- A patch that fails verification is worth more than no patch at all. It tells
  the reviewer what the cause is not, and it is recorded either way. Try the
  smallest change that would make the observed failure impossible, and let the
  suite say whether you were right.

How to reason:

- Record each hypothesis before testing it, phrased so it could be shown false.
- Record rejections too, with the evidence that ruled them out. A reviewer needs
  to know what you considered and discarded, not only what you settled on.
- Do not conclude on a plausible story. Conclude when you have run something that
  demonstrates the failure, can point at the line responsible, and can explain
  why the observed behaviour follows from it.
- If the evidence does not support a specific symbol, say so rather than
  guessing. An honest "I could not determine this" is more useful than a
  confident wrong answer, because a wrong answer costs the reviewer their time
  and their trust.

How to finish:

Naming the cause is half the job. The person reading this is on call, and a
diagnosis they still have to write the fix for has saved them the hard thinking
and none of the work.

So before you conclude: attempt the fix with `try_patch` and let verification
judge it. Conclude only after that has run, whether it passed or failed, and say
in your reasoning what the verification showed.

The one case where concluding without a patch is right is when you cannot
identify a specific change that would fix it. Then say so plainly. An honest "I
found the cause but not the fix" is useful. Stopping at the diagnosis because it
felt like the end is not.

Call `conclude` exactly once, and last.
"""


class ModelResponse(Protocol):
    content: list[Any]
    stop_reason: str | None


class Messages(Protocol):
    def create(self, **kwargs: Any) -> ModelResponse: ...


class ModelClient(Protocol):
    messages: Messages


def _block_to_dict(block: Any) -> dict[str, Any]:
    """Turn a response block into something that can be sent back.

    Serialised whole rather than field by field. Thinking blocks carry a
    signature the API verifies when they are returned, and any block type
    added later will carry something similar; a hand-written converter drops
    what it does not know about and the next turn is rejected.
    """
    dump = getattr(block, "model_dump", None)
    if callable(dump):
        return dict(dump(exclude_none=True))

    kind = getattr(block, "type", None)
    if kind == "text":
        return {"type": "text", "text": block.text}
    if kind == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    return {"type": "text", "text": str(block)}


class InvestigationLoop:
    def __init__(
        self,
        client: ModelClient,
        toolbox: Toolbox,
        state: RunState,
        model: str = "claude-sonnet-5",
        max_tool_calls: int = 60,
        max_turns: int = 40,
        max_tokens: int = 4096,
    ) -> None:
        self.client = client
        self.toolbox = toolbox
        self.state = state
        self.model = model
        self.max_tool_calls = max_tool_calls
        self.max_turns = max_turns
        self.max_tokens = max_tokens

    def run(self, brief: str) -> RunState:
        messages: list[dict[str, Any]] = [{"role": "user", "content": brief}]

        for _ in range(self.max_turns):
            if self.state.call_count >= self.max_tool_calls:
                self.state.abandon(
                    f"tool call budget exhausted after {self.state.call_count} calls"
                )
                return self.state

            response = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=SYSTEM_PROMPT,
                tools=self.toolbox.schemas(),
                # A copy, not the list itself. Passing the live list means
                # anything holding onto a request -- a recorder, a replay
                # fixture, a debugger -- sees the conversation's final state
                # rather than what was actually sent on that turn.
                messages=list(messages),
            )

            blocks = [_block_to_dict(block) for block in response.content]
            messages.append({"role": "assistant", "content": blocks})

            tool_uses = [block for block in blocks if block["type"] == "tool_use"]
            if not tool_uses:
                # The model stopped calling tools without concluding. One nudge,
                # then stop: a second nudge has never once produced an answer,
                # it just burns budget.
                if self.state.stopped_because is not None:
                    return self.state
                self.state.abandon("model stopped without calling conclude")
                return self.state

            results: list[dict[str, Any]] = []
            for call in tool_uses:
                result, failed = self.toolbox.invoke(call["name"], call["input"])
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": call["id"],
                        "content": result,
                        "is_error": failed,
                    }
                )

            if self.state.root_cause is not None:
                return self.state

            messages.append({"role": "user", "content": results})

        self.state.abandon(f"turn limit reached after {self.max_turns} turns")
        return self.state


def build_brief(incident: str, alert: str | None = None) -> str:
    """What the agent is told. Deliberately thin — no hints about the cause."""
    lines = [
        f"Incident: {incident}",
        "",
        "An alert fired against the storefront. Investigate and identify the root cause.",
    ]
    if alert:
        lines.insert(1, f"Alert: {alert}")
    return "\n".join(lines)
