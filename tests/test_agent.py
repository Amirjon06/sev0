"""Tests for run state, the toolbox, and the investigation loop.

The loop is exercised against a scripted client rather than a live model. What
is worth testing here is not whether the model is clever, but whether the
harness around it holds: budgets enforced, failures surfaced rather than
swallowed, and nothing concluded without a record of how.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from sev0.agent.loop import InvestigationLoop, build_brief
from sev0.agent.state import Confidence, RootCause, RunState, Verdict
from sev0.agent.tools import Toolbox


class Block:
    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


def text(body: str) -> Block:
    return Block(type="text", text=body)


def call(name: str, **arguments: Any) -> Block:
    return Block(type="tool_use", id=f"tu_{name}", name=name, input=arguments)


class Response:
    def __init__(self, *blocks: Block) -> None:
        self.content = list(blocks)
        self.stop_reason = "tool_use"


class ScriptedMessages:
    def __init__(self, script: list[Response]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Response:
        self.calls.append(kwargs)
        if not self.script:
            return Response(text("nothing left to say"))
        return self.script.pop(0)


class ScriptedClient:
    def __init__(self, *responses: Response) -> None:
        self.messages = ScriptedMessages(list(responses))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    (tmp_path / "cart.py").write_text(
        "PROMOS = {'SAVE10': 10}\n\n\ndef total(subtotal, code):\n    return subtotal\n"
    )
    git("init", "-q", "-b", "main")
    git("config", "user.name", "Dana Whitfield")
    git("config", "user.email", "dana@storefront.example")
    git("add", "-A")
    git("commit", "-q", "-m", "feat(cart): Add totals")
    return tmp_path


@pytest.fixture
def state() -> RunState:
    return RunState(incident="checkout-5xx")


@pytest.fixture
def toolbox(state: RunState, repo: Path) -> Toolbox:
    return Toolbox(state=state, repo=repo)


class TestRunState:
    def test_a_repeated_hypothesis_is_updated_not_duplicated(self, state: RunState) -> None:
        state.add_hypothesis("pool exhaustion", Verdict.PROPOSED, "")
        state.add_hypothesis("Pool Exhaustion", Verdict.REJECTED, "utilisation peaked at 34%")

        assert len(state.hypotheses) == 1
        assert state.hypotheses[0].verdict is Verdict.REJECTED
        assert "34%" in state.hypotheses[0].reasoning

    def test_rejections_are_retrievable_as_evidence(self, state: RunState) -> None:
        state.add_hypothesis("a", Verdict.REJECTED, "ruled out")
        state.add_hypothesis("b", Verdict.CONFIRMED, "held up")

        assert [h.statement for h in state.rejected] == ["a"]

    def test_concluding_records_a_finish_time(self, state: RunState) -> None:
        assert state.finished_at is None
        state.conclude(RootCause("cart", "cart.py", "total", "abc1234", "because"))
        assert state.finished_at is not None

    def test_the_trace_round_trips_through_json(self, state: RunState, tmp_path: Path) -> None:
        state.add_hypothesis("something", Verdict.REJECTED, "no")
        state.record_call("search_code", {"pattern": "x"}, "no matches", failed=False)
        state.conclude(
            RootCause("cart", "cart.py", "total", "abc1234", "because", Confidence.HIGH)
        )

        written = json.loads(state.save(tmp_path).read_text())

        assert written["hypotheses"][0]["verdict"] == "rejected"
        assert written["root_cause"]["confidence"] == "high"
        assert written["tool_calls"][0]["name"] == "search_code"

    def test_the_summary_says_when_nothing_was_found(self, state: RunState) -> None:
        state.abandon("tool call budget exhausted")
        assert "no root cause" in state.summary()


class TestToolbox:
    def test_an_unknown_tool_lists_the_real_ones(self, toolbox: Toolbox) -> None:
        result, failed = toolbox.invoke("teleport", {})

        assert failed
        assert "search_code" in result

    def test_a_failing_tool_returns_text_rather_than_raising(self, toolbox: Toolbox) -> None:
        result, failed = toolbox.invoke("search_code", {"pattern": "(unclosed"})

        assert failed
        assert "bad pattern" in result

    def test_bad_arguments_are_explained(self, toolbox: Toolbox) -> None:
        result, failed = toolbox.invoke("read_symbol", {"path": "cart.py"})

        assert failed
        assert "Bad arguments" in result

    def test_every_call_is_recorded_including_failures(
        self, toolbox: Toolbox, state: RunState
    ) -> None:
        toolbox.invoke("recent_commits", {})
        toolbox.invoke("teleport", {})

        assert state.call_count == 2
        assert [c.failed for c in state.tool_calls] == [False, True]

    def test_missing_observability_is_reported_not_crashed(self, toolbox: Toolbox) -> None:
        result, failed = toolbox.invoke("metrics_overview", {})

        assert failed
        assert "Prometheus is not configured" in result

    def test_search_annotates_matches_with_their_function(self, toolbox: Toolbox) -> None:
        result, failed = toolbox.invoke("search_code", {"pattern": "subtotal"})

        assert not failed
        assert "[total]" in result

    def test_read_symbol_returns_the_whole_function(self, toolbox: Toolbox) -> None:
        result, _ = toolbox.invoke("read_symbol", {"path": "cart.py", "name": "total"})
        assert "def total(subtotal, code):" in result

    def test_long_results_are_truncated(self, toolbox: Toolbox, repo: Path) -> None:
        (repo / "big.py").write_text("# padding\n" * 5000)
        result, _ = toolbox.invoke("search_code", {"pattern": "padding", "limit": 5000})
        assert "truncated" in result

    def test_an_invalid_verdict_is_rejected_with_the_allowed_values(
        self, toolbox: Toolbox
    ) -> None:
        result, _ = toolbox.invoke(
            "record_hypothesis", {"statement": "x", "verdict": "maybe-ish"}
        )
        assert "proposed" in result and "rejected" in result

    def test_concluding_populates_the_run_state(self, toolbox: Toolbox, state: RunState) -> None:
        toolbox.invoke(
            "conclude",
            {
                "service": "cart",
                "file": "cart.py",
                "symbol": "total",
                "commit": "abc1234",
                "explanation": "unguarded None",
                "confidence": "high",
            },
        )

        assert state.root_cause is not None
        assert state.root_cause.confidence is Confidence.HIGH

    def test_the_schemas_are_well_formed(self, toolbox: Toolbox) -> None:
        for schema in toolbox.schemas():
            assert schema["name"] and schema["description"]
            assert schema["input_schema"]["type"] == "object"


class TestInvestigationLoop:
    def test_a_conclusion_ends_the_run(self, toolbox: Toolbox, state: RunState) -> None:
        client = ScriptedClient(
            Response(text("Looking at history."), call("recent_commits")),
            Response(
                call(
                    "conclude",
                    service="cart",
                    file="cart.py",
                    symbol="total",
                    commit="abc1234",
                    explanation="unguarded None",
                    confidence="high",
                )
            ),
            Response(call("recent_commits")),  # must never be reached
        )

        loop = InvestigationLoop(client, toolbox, state)
        loop.run(build_brief("checkout-5xx"))

        assert state.root_cause is not None
        assert len(client.messages.calls) == 2

    def test_the_tool_call_budget_is_enforced(self, toolbox: Toolbox, state: RunState) -> None:
        client = ScriptedClient(*[Response(call("recent_commits")) for _ in range(20)])

        loop = InvestigationLoop(client, toolbox, state, max_tool_calls=3)
        loop.run(build_brief("checkout-5xx"))

        assert state.root_cause is None
        assert state.stopped_because is not None
        assert "budget exhausted" in state.stopped_because
        assert state.call_count <= 4

    def test_a_model_that_stops_calling_tools_ends_the_run(
        self, toolbox: Toolbox, state: RunState
    ) -> None:
        client = ScriptedClient(Response(text("I am not sure where to look.")))

        loop = InvestigationLoop(client, toolbox, state)
        loop.run(build_brief("checkout-5xx"))

        assert state.root_cause is None
        assert state.stopped_because == "model stopped without calling conclude"

    def test_the_turn_limit_is_enforced(self, toolbox: Toolbox, state: RunState) -> None:
        client = ScriptedClient(*[Response(call("recent_commits")) for _ in range(50)])

        loop = InvestigationLoop(client, toolbox, state, max_turns=4, max_tool_calls=999)
        loop.run(build_brief("checkout-5xx"))

        assert state.stopped_because is not None
        assert "turn limit" in state.stopped_because

    def test_tool_failures_are_fed_back_to_the_model(
        self, toolbox: Toolbox, state: RunState
    ) -> None:
        client = ScriptedClient(
            Response(call("search_code", pattern="(unclosed")),
            Response(
                call(
                    "conclude",
                    service="cart",
                    file="cart.py",
                    symbol="total",
                    commit="abc1234",
                    explanation="recovered after a bad pattern",
                )
            ),
        )

        loop = InvestigationLoop(client, toolbox, state)
        loop.run(build_brief("checkout-5xx"))

        second_turn = client.messages.calls[1]["messages"]
        results = second_turn[-1]["content"]
        assert results[0]["is_error"] is True
        assert state.root_cause is not None

    def test_the_brief_contains_no_hint_about_the_cause(self) -> None:
        brief = build_brief("checkout-5xx", alert="error rate above 5%")

        for giveaway in ("cart", "promo", "discount", "None", "TypeError"):
            assert giveaway not in brief

    def test_the_system_prompt_is_sent_with_the_tools(
        self, toolbox: Toolbox, state: RunState
    ) -> None:
        client = ScriptedClient(Response(text("done")))

        InvestigationLoop(client, toolbox, state).run(build_brief("checkout-5xx"))

        sent = client.messages.calls[0]
        assert "on-call software engineer" in sent["system"]
        assert {tool["name"] for tool in sent["tools"]} >= {"search_code", "conclude"}
