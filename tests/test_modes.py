"""Ablation and baseline modes.

Two properties matter. An ablation has to remove exactly one thing, or the
result cannot be attributed to that thing. And no mode may weaken the safety
rails, because a comparison against a more dangerous system is not a comparison
of the system that ships.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sev0.agent import baseline as baseline_module
from sev0.agent.capabilities import (
    EXECUTION_TOOLS,
    HISTORY_TOOLS,
    MODES,
    OBSERVABILITY_TOOLS,
    REASONING_TOOLS,
    RETRIEVAL_TOOLS,
    Capabilities,
    mode,
)
from sev0.agent.loop import (
    EXECUTION_SECTION,
    HISTORY_GUIDANCE,
    RETRIEVAL_GUIDANCE,
    system_prompt,
)
from sev0.agent.state import RunState, Usage
from sev0.agent.tools import Toolbox
from sev0.pricing import estimate_cost
from sev0.sandbox.patch import PatchLimits


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "services").mkdir()
    (tmp_path / "services" / "cart.py").write_text("def total(items):\n    return sum(items)\n")
    return tmp_path


def toolbox_for(repo: Path, name: str) -> Toolbox:
    return Toolbox(
        state=RunState(incident="checkout-5xx"),
        repo=repo,
        capabilities=mode(name),
    )


class TestCapabilityGating:
    def test_full_mode_exposes_every_tool(self, repo: Path) -> None:
        names = {s["name"] for s in toolbox_for(repo, "full").schemas()}
        assert names >= EXECUTION_TOOLS
        assert names >= HISTORY_TOOLS
        assert names >= RETRIEVAL_TOOLS
        assert names >= OBSERVABILITY_TOOLS

    @pytest.mark.parametrize(
        ("name", "removed"),
        [
            ("no-execution", EXECUTION_TOOLS),
            ("no-history", HISTORY_TOOLS),
            ("no-retrieval", RETRIEVAL_TOOLS),
        ],
    )
    def test_an_ablation_removes_its_group_entirely(
        self, repo: Path, name: str, removed: frozenset[str]
    ) -> None:
        names = {s["name"] for s in toolbox_for(repo, name).schemas()}
        assert not (removed & names)

    @pytest.mark.parametrize("name", sorted(MODES))
    def test_an_ablation_removes_only_its_own_group(self, repo: Path, name: str) -> None:
        full = {s["name"] for s in toolbox_for(repo, "full").schemas()}
        ablated = {s["name"] for s in toolbox_for(repo, name).schemas()}
        missing = full - ablated

        groups = [EXECUTION_TOOLS, HISTORY_TOOLS, RETRIEVAL_TOOLS, OBSERVABILITY_TOOLS]
        assert any(missing == group for group in groups) or not missing

    @pytest.mark.parametrize("name", sorted(MODES))
    def test_every_mode_can_still_state_an_answer(self, repo: Path, name: str) -> None:
        # Without conclude, a mode scores zero for a reason that has nothing to
        # do with the component under test.
        names = {s["name"] for s in toolbox_for(repo, name).schemas()}
        assert names >= REASONING_TOOLS

    def test_a_removed_tool_is_absent_rather_than_refusing(self, repo: Path) -> None:
        box = toolbox_for(repo, "no-execution")
        result, failed = box.invoke("run_tests", {})

        assert failed
        assert "No tool named" in result

    def test_an_unknown_mode_is_rejected(self) -> None:
        with pytest.raises(KeyError, match="unknown mode"):
            mode("no-thinking")


class TestSafetyIsNeverAblated:
    @pytest.mark.parametrize("name", sorted(MODES))
    def test_patch_limits_are_identical_in_every_mode(self, repo: Path, name: str) -> None:
        limits = PatchLimits(max_files=2, max_lines=10, protected_paths=("infra/",))
        box = Toolbox(
            state=RunState(incident="x"),
            repo=repo,
            limits=limits,
            capabilities=mode(name),
        )
        assert box.limits == limits

    @pytest.mark.parametrize("name", sorted(MODES))
    def test_no_mode_grants_a_capability_full_does_not_have(
        self, repo: Path, name: str
    ) -> None:
        full = {s["name"] for s in toolbox_for(repo, "full").schemas()}
        ablated = {s["name"] for s in toolbox_for(repo, name).schemas()}
        assert ablated <= full

    def test_capabilities_describe_what_is_missing(self) -> None:
        assert Capabilities(execution=False).missing == ("execution",)
        assert Capabilities().missing == ()


class TestPromptMatchesCapabilities:
    def test_the_full_prompt_asks_for_experiments(self) -> None:
        assert "run_snippet" in system_prompt(mode("full"))

    def test_removing_execution_removes_the_instruction_to_execute(self) -> None:
        # Telling a model to run experiments it has no tools for measures how
        # many turns it wastes finding that out.
        prompt = system_prompt(mode("no-execution"))
        assert EXECUTION_SECTION not in prompt
        assert "run_snippet" not in prompt
        assert "cannot" in prompt

    def test_removing_history_removes_the_instruction_to_correlate(self) -> None:
        assert HISTORY_GUIDANCE not in system_prompt(mode("no-history"))

    def test_removing_retrieval_removes_the_instruction_to_read_symbols(self) -> None:
        assert RETRIEVAL_GUIDANCE not in system_prompt(mode("no-retrieval"))

    def test_no_mode_produces_an_empty_prompt(self) -> None:
        for name in MODES:
            assert len(system_prompt(mode(name))) > 500


class FakeUsage:
    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_input_tokens = 0
        self.cache_creation_input_tokens = 0


class FakeBlock:
    type = "tool_use"

    def __init__(self, payload: dict[str, Any]) -> None:
        self.input = payload


class FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.content = [FakeBlock(payload)]
        self.usage = FakeUsage(1200, 300)


class FakeMessages:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(kwargs)
        return FakeResponse(self.payload)


class FakeClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.messages = FakeMessages(payload)


CONCLUSION = {
    "service": "cart",
    "file": "services/cart.py",
    "symbol": "total",
    "commit": "abc1234",
    "explanation": "sum over a None",
    "confidence": "medium",
}


class TestStaticBaseline:
    def test_it_reaches_a_conclusion_in_one_call(self, repo: Path) -> None:
        state = RunState(incident="checkout-5xx")
        box = Toolbox(state=state, repo=repo)
        client = FakeClient(CONCLUSION)

        baseline_module.StaticBaseline(client, box, state, "test-model").run("checkout-5xx")

        assert len(client.messages.calls) == 1
        assert state.root_cause is not None
        assert state.root_cause.symbol == "total"

    def test_it_is_offered_only_the_conclude_tool(self, repo: Path) -> None:
        state = RunState(incident="checkout-5xx")
        box = Toolbox(state=state, repo=repo)
        client = FakeClient(CONCLUSION)

        baseline_module.StaticBaseline(client, box, state, "test-model").run("checkout-5xx")

        sent = client.messages.calls[0]
        assert [tool["name"] for tool in sent["tools"]] == ["conclude"]
        assert sent["tool_choice"] == {"type": "tool", "name": "conclude"}

    def test_the_evidence_it_gathered_is_not_counted_as_effort(self, repo: Path) -> None:
        # Otherwise the baseline's tool-call count would read as investigation
        # it never did, and the comparison would flatter it.
        state = RunState(incident="checkout-5xx")
        box = Toolbox(state=state, repo=repo)

        baseline_module.StaticBaseline(FakeClient(CONCLUSION), box, state, "m").run("x")

        assert state.experiments == 0
        assert state.call_count == 1  # the conclude call itself

    def test_a_baseline_that_says_nothing_is_recorded_as_abandoned(self, repo: Path) -> None:
        class Silent:
            class messages:  # noqa: N801 - mirrors the SDK shape
                @staticmethod
                def create(**_: Any) -> Any:
                    class Empty:
                        content: list[Any] = []
                        usage = None

                    return Empty()

        state = RunState(incident="x")
        box = Toolbox(state=state, repo=repo)
        baseline_module.StaticBaseline(Silent(), box, state, "m").run("x")

        assert state.root_cause is None
        assert state.stopped_because == "baseline returned no conclusion"

    def test_the_package_is_rendered_from_real_collector_output(self, repo: Path) -> None:
        state = RunState(incident="checkout-5xx")
        box = Toolbox(state=state, repo=repo)

        package = baseline_module.gather(box, "checkout-5xx")

        assert "Alert" in package.sections
        assert "checkout-5xx" in package.render()


class TestUsageAndCost:
    def test_usage_accumulates_across_turns(self) -> None:
        usage = Usage()
        usage.add(FakeUsage(100, 20))
        usage.add(FakeUsage(150, 30))

        assert usage.requests == 2
        assert usage.input_tokens == 250
        assert usage.total_tokens == 300

    def test_a_missing_usage_block_is_ignored(self) -> None:
        usage = Usage()
        usage.add(None)
        assert usage.requests == 0

    def test_a_priced_model_produces_a_cost(self) -> None:
        usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
        assert estimate_cost(usage, "claude-sonnet-5") == pytest.approx(18.0)

    def test_an_unpriced_model_produces_no_cost_rather_than_a_guess(self) -> None:
        # A plausible-looking figure nobody can check is worse than a blank,
        # because a blank is obviously missing.
        usage = Usage(input_tokens=1000, output_tokens=1000)
        assert estimate_cost(usage, "some-other-model") is None

    def test_cache_tokens_are_priced_at_their_own_rate(self) -> None:
        cached = Usage(cache_read_tokens=1_000_000)
        plain = Usage(input_tokens=1_000_000)

        assert estimate_cost(cached, "claude-sonnet-5") < estimate_cost(plain, "claude-sonnet-5")
