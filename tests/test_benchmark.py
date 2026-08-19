"""Tests for benchmark sequencing, aggregation, and reporting.

The runner's callbacks are injected, so everything here runs without Docker, a
network, or a model. That is deliberate: what needs testing is the ordering and
the failure handling, and neither of those becomes more true for having spent
money to check it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from incident_lab import benchmark as bench
from incident_lab.scenarios.model import Change, GroundTruth, Scenario
from incident_lab.scoring import Score


def scenario(name: str, family: str = "code") -> Scenario:
    return Scenario(
        id=name,
        title=name,
        family=family,
        summary="s",
        alert="checkout-5xx",
        changes=(Change(message="m", author="A <a@example.com>", edits=()),),
        ground_truth=GroundTruth(service="cart", file="f.py", symbol="g", summary="t"),
    )


def score(
    name: str,
    *,
    correct: bool = True,
    resolved: bool = True,
    seconds: float = 30.0,
    unsafe: int = 0,
    experiments: int = 2,
    mode: str = "full",
    cost: float | None = 0.01,
) -> Score:
    return Score(
        scenario=name,
        run_id=f"run-{name}-{mode}",
        found_file=correct,
        found_symbol=correct,
        found_commit=correct,
        seconds=seconds,
        resolved=resolved,
        unsafe_attempts=unsafe,
        experiments=experiments,
        tool_calls=10,
        mode=mode,
        cost_usd=cost,
    )


class Harness:
    """Records the order of every prepare, investigate, and cleanup."""

    def __init__(self, failing: set[str] | None = None, unpreparable: set[str] | None = None):
        self.events: list[str] = []
        self.failing = failing or set()
        self.unpreparable = unpreparable or set()

    def prepare(self, s: Scenario) -> None:
        self.events.append(f"prepare:{s.id}")
        if s.id in self.unpreparable:
            raise RuntimeError("inject failed")

    def cleanup(self, s: Scenario) -> None:
        self.events.append(f"cleanup:{s.id}")

    def investigate(self, s: Scenario, mode: str, trial: int) -> Score:
        self.events.append(f"run:{s.id}:{mode}:{trial}")
        if s.id in self.failing:
            raise RuntimeError("the model exploded")
        return score(s.id, mode=mode)


def execute(harness: Harness, scenarios: list[Scenario], modes: tuple[str, ...], runs: int = 1):
    return bench.execute(
        scenarios=scenarios,
        modes=modes,
        runs_per_scenario=runs,
        model="test-model",
        sev0_commit="abc1234",
        investigate=harness.investigate,
        prepare=harness.prepare,
        cleanup=harness.cleanup,
    )


class TestSequencing:
    def test_every_scenario_is_injected_and_restored(self) -> None:
        harness = Harness()
        execute(harness, [scenario("a"), scenario("b")], ("full",))

        assert harness.events == [
            "prepare:a",
            "run:a:full:1",
            "cleanup:a",
            "prepare:b",
            "run:b:full:1",
            "cleanup:b",
        ]

    def test_repeated_trials_share_one_injection(self) -> None:
        # Injecting and settling is most of the wall clock. Repeating a trial
        # against the same injected fault is the point of --runs.
        harness = Harness()
        execute(harness, [scenario("a")], ("full",), runs=3)

        assert harness.events.count("prepare:a") == 1
        assert [e for e in harness.events if e.startswith("run:")] == [
            "run:a:full:1",
            "run:a:full:2",
            "run:a:full:3",
        ]

    def test_every_mode_sees_the_same_injected_fault(self) -> None:
        harness = Harness()
        execute(harness, [scenario("a")], ("full", "no-execution"))

        assert harness.events == [
            "prepare:a",
            "run:a:full:1",
            "run:a:no-execution:1",
            "cleanup:a",
        ]

    def test_the_trial_count_is_scenarios_times_modes_times_runs(self) -> None:
        run = execute(
            Harness(), [scenario("a"), scenario("b")], ("full", "no-history"), runs=2
        )
        assert len(run.trials) == 8


class TestFailureIsolation:
    def test_a_failing_trial_does_not_stop_the_suite(self) -> None:
        harness = Harness(failing={"a"})
        run = execute(harness, [scenario("a"), scenario("b")], ("full",))

        assert [t.scenario for t in run.trials] == ["a", "b"]
        assert run.trials[0].error
        assert run.trials[1].ok

    def test_a_failing_trial_is_still_restored(self) -> None:
        # A scenario left injected silently becomes part of the next one.
        harness = Harness(failing={"a"})
        execute(harness, [scenario("a"), scenario("b")], ("full",))

        assert "cleanup:a" in harness.events
        assert harness.events.index("cleanup:a") < harness.events.index("prepare:b")

    def test_a_scenario_that_cannot_be_injected_is_skipped_not_run(self) -> None:
        harness = Harness(unpreparable={"a"})
        run = execute(harness, [scenario("a"), scenario("b")], ("full",))

        assert "run:a:full:1" not in harness.events
        assert run.trials[0].error == "scenario was not injected"
        assert run.trials[1].ok

    def test_a_scenario_that_cannot_be_injected_is_still_cleaned_up(self) -> None:
        harness = Harness(unpreparable={"a"})
        execute(harness, [scenario("a")], ("full",))

        assert "cleanup:a" in harness.events

    def test_later_trials_of_a_failing_scenario_still_run(self) -> None:
        harness = Harness(failing={"a"})
        run = execute(harness, [scenario("a")], ("full",), runs=3)

        assert len(run.trials) == 3
        assert all(t.error for t in run.trials)


class TestSummary:
    def test_counts_travel_with_every_rate(self) -> None:
        trials = [
            bench.Trial("a", "code", "full", 1, score=score("a", correct=True)),
            bench.Trial("b", "code", "full", 1, score=score("b", correct=False)),
        ]
        summary = bench.summarise(trials, "x")

        assert summary.rate(summary.correct) == "1/2 (50%)"

    def test_a_failed_trial_is_not_counted_as_a_wrong_answer(self) -> None:
        # A trial that crashed is missing data, not a miss. Folding it into the
        # denominator would make an infrastructure problem look like a model
        # problem.
        trials = [
            bench.Trial("a", "code", "full", 1, score=score("a")),
            bench.Trial("b", "code", "full", 1, error="boom"),
        ]
        summary = bench.summarise(trials, "x")

        assert summary.trials == 2
        assert summary.attempted == 1
        assert summary.accuracy == 1.0

    def test_p95_is_withheld_below_a_sample_size_where_it_would_lie(self) -> None:
        few = [bench.Trial("a", "code", "full", i, score=score("a")) for i in range(8)]
        assert bench.summarise(few, "x").p95_seconds is None

        many = [bench.Trial("a", "code", "full", i, score=score("a")) for i in range(25)]
        assert bench.summarise(many, "x").p95_seconds is not None

    def test_diagnosis_and_repair_stay_separate(self) -> None:
        trials = [
            bench.Trial("a", "code", "full", 1, score=score("a", correct=True, resolved=False))
        ]
        summary = bench.summarise(trials, "x")

        assert summary.correct == 1
        assert summary.resolved == 0

    def test_cost_is_omitted_rather_than_guessed(self) -> None:
        trials = [bench.Trial("a", "code", "full", 1, score=score("a", cost=None))]
        assert bench.summarise(trials, "x").cost_usd is None

    def test_an_empty_slice_reports_no_rate_rather_than_zero(self) -> None:
        summary = bench.summarise([], "x")
        assert summary.accuracy is None
        assert summary.rate(0) == "n/a"


class TestReport:
    @pytest.fixture
    def run(self) -> bench.BenchmarkRun:
        run = bench.BenchmarkRun(
            model="claude-test",
            modes=("full", "no-execution"),
            runs_per_scenario=2,
            sev0_commit="abc1234",
        )
        run.trials = [
            bench.Trial("a", "code", "full", 1, score=score("a", mode="full")),
            bench.Trial(
                "a",
                "code",
                "no-execution",
                1,
                score=score("a", correct=False, mode="no-execution"),
            ),
            bench.Trial("b", "config", "full", 1, score=score("b", mode="full")),
            bench.Trial("c", "infra", "full", 1, error="timed out"),
        ]
        return run

    def test_the_report_names_the_model_and_the_revision(self, run: bench.BenchmarkRun) -> None:
        text = bench.report(run)
        assert "claude-test" in text
        assert "abc1234" in text

    def test_the_report_states_scenario_and_trial_counts_separately(
        self, run: bench.BenchmarkRun
    ) -> None:
        text = bench.report(run)
        assert "Scenarios: **3**" in text
        assert "Trials: **4**" in text

    def test_rates_carry_their_denominators(self, run: bench.BenchmarkRun) -> None:
        assert "2/3 (67%)" in bench.report(run)

    def test_results_are_broken_out_by_mode_and_family(self, run: bench.BenchmarkRun) -> None:
        text = bench.report(run)
        assert "By mode" in text and "no-execution" in text
        assert "By fault family" in text and "config" in text

    def test_incomplete_trials_are_listed_rather_than_hidden(
        self, run: bench.BenchmarkRun
    ) -> None:
        text = bench.report(run)
        assert "did not complete" in text
        assert "timed out" in text

    def test_an_empty_run_says_so(self) -> None:
        empty = bench.BenchmarkRun(model="m", modes=(), runs_per_scenario=1, sev0_commit="x")
        assert "No trials" in bench.report(empty)


class TestPersistence:
    def test_a_run_round_trips_through_json(self, tmp_path: Path) -> None:
        # Results have to be re-readable, or a published number cannot be
        # rechecked by anyone.
        run = bench.BenchmarkRun(
            model="m", modes=("full",), runs_per_scenario=1, sev0_commit="abc"
        )
        run.trials = [bench.Trial("a", "code", "full", 1, score=score("a"))]
        run.finished_at = "2026-08-19T00:00:00+00:00"

        path = run.save(tmp_path / "bench.json")
        reloaded = bench.BenchmarkRun.load(path)

        assert reloaded.model == run.model
        assert reloaded.sev0_commit == run.sev0_commit
        assert len(reloaded.trials) == 1
        assert reloaded.trials[0].score == run.trials[0].score

    def test_a_failed_trial_round_trips_without_a_score(self, tmp_path: Path) -> None:
        run = bench.BenchmarkRun(
            model="m", modes=("full",), runs_per_scenario=1, sev0_commit="abc"
        )
        run.trials = [bench.Trial("a", "code", "full", 1, error="boom")]

        reloaded = bench.BenchmarkRun.load(run.save(tmp_path / "b.json"))
        assert reloaded.trials[0].score is None
        assert reloaded.trials[0].error == "boom"


class TestPlan:
    def test_the_plan_covers_every_combination(self) -> None:
        combinations = list(
            bench.plan([scenario("a"), scenario("b")], ["full", "no-history"], 2)
        )
        assert len(combinations) == 8

    def test_the_plan_keeps_a_scenario_together(self) -> None:
        order = [s.id for s, _, _ in bench.plan([scenario("a"), scenario("b")], ["full"], 2)]
        assert order == ["a", "a", "b", "b"]
