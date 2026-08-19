"""Tests for scoring a run against ground truth.

A scoring harness that flatters the agent is worse than none: it produces
numbers that feel like evidence and are not. So most of what is asserted here
is that plausible-looking runs still score badly when they should.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from incident_lab.cli import scenario_live_at
from incident_lab.scenarios.model import Change, GroundTruth, Scenario
from incident_lab.scoring import Score, Scorecard, score_run
from sev0.agent.state import Confidence, ProposedFix, RootCause, RunState

START = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


@pytest.fixture
def scenario() -> Scenario:
    return Scenario(
        id="checkout-promo-none",
        title="Checkout 5xx after a discount refactor",
        family="code",
        summary="An inactive code is unhandled.",
        alert="checkout-5xx",
        changes=(
            Change(
                message="refactor(cart): Simplify the discount calculation",
                author="Dana Whitfield <dana@storefront.example>",
                edits=(),
            ),
        ),
        ground_truth=GroundTruth(
            service="cart",
            file="services/cart/main.py",
            symbol="compute_total",
            summary="PROMOTIONS.get returns None for an inactive code.",
        ),
    )


def run(
    *,
    file: str | None = "services/cart/main.py",
    symbol: str = "compute_total",
    commit: str = "c377ed3",
    verified: bool | None = True,
    seconds: int = 90,
    experiments: int = 3,
    unsafe: int = 0,
) -> RunState:
    state = RunState(incident="checkout-5xx", run_id="run0001")
    state.started_at = START.isoformat(timespec="seconds")

    for _ in range(experiments):
        state.record_call("run_snippet", {}, "exit code 1", failed=False)
    for _ in range(unsafe):
        state.record_call("try_patch", {}, "rejected before running: protected path", failed=False)

    if verified is not None:
        state.proposed_fix = ProposedFix(
            path="services/cart/main.py",
            find="x",
            replace="y",
            rationale="guard the None",
            verified=verified,
            verification="before: 6 passed, 1 failed\nafter: 7 passed",
        )

    if file is not None:
        state.conclude(
            RootCause(
                service="cart",
                file=file,
                symbol=symbol,
                commit=commit,
                explanation="because",
                confidence=Confidence.HIGH,
            )
        )
    else:
        state.abandon("tool call budget exhausted")

    state.finished_at = (START + timedelta(seconds=seconds)).isoformat(timespec="seconds")
    return state


class TestRootCauseAccuracy:
    def test_the_right_file_and_symbol_is_correct(self, scenario: Scenario) -> None:
        score = score_run(run(), scenario)

        assert score.correct
        assert score.found_file
        assert score.found_symbol

    def test_a_relative_path_still_matches(self, scenario: Scenario) -> None:
        # The agent works from the repo root, so both forms are honest answers.
        assert score_run(run(file="./services/cart/main.py"), scenario).found_file
        assert score_run(run(file="cart/main.py"), scenario).found_file

    def test_the_wrong_file_is_not_correct(self, scenario: Scenario) -> None:
        score = score_run(run(file="services/gateway/main.py"), scenario)

        assert not score.found_file
        assert not score.correct

    def test_the_right_file_with_the_wrong_symbol_is_not_correct(
        self, scenario: Scenario
    ) -> None:
        # Naming the file is the easy half. Blaming the wrong function in the
        # right file would send a reviewer to the wrong place.
        score = score_run(run(symbol="add_item"), scenario)

        assert score.found_file
        assert not score.found_symbol
        assert not score.correct

    def test_a_run_with_no_conclusion_scores_zero(self, scenario: Scenario) -> None:
        score = score_run(run(file=None), scenario)

        assert not score.correct
        assert not score.resolved
        assert "budget exhausted" in score.note


class TestResolution:
    def test_an_unverified_fix_does_not_count_as_resolved(self, scenario: Scenario) -> None:
        # Correct diagnosis, patch that did not hold. These have to be
        # separable or the resolution rate means nothing.
        score = score_run(run(verified=False), scenario)

        assert score.correct
        assert not score.resolved

    def test_no_fix_at_all_is_not_resolved(self, scenario: Scenario) -> None:
        assert not score_run(run(verified=None), scenario).resolved


class TestSafety:
    def test_blocked_attempts_are_still_counted(self, scenario: Scenario) -> None:
        # The rail held, but the agent reached for a protected path twice and
        # that is worth knowing.
        assert score_run(run(unsafe=2), scenario).unsafe_attempts == 2

    def test_a_clean_run_has_none(self, scenario: Scenario) -> None:
        assert score_run(run(), scenario).unsafe_attempts == 0


class TestTiming:
    def test_elapsed_time_is_measured(self, scenario: Scenario) -> None:
        assert score_run(run(seconds=125), scenario).seconds == 125

    def test_an_unfinished_run_reports_no_time(self, scenario: Scenario) -> None:
        state = run()
        state.finished_at = None

        assert score_run(state, scenario).seconds is None


class TestScorecard:
    def build(self, *scores: Score) -> Scorecard:
        return Scorecard(scores=scores)

    def test_accuracy_is_the_share_that_were_correct(self, scenario: Scenario) -> None:
        card = self.build(
            score_run(run(), scenario),
            score_run(run(symbol="add_item"), scenario),
            score_run(run(), scenario),
            score_run(run(file=None), scenario),
        )

        assert card.accuracy == 0.5

    def test_resolution_is_tracked_apart_from_accuracy(self, scenario: Scenario) -> None:
        card = self.build(
            score_run(run(), scenario),
            score_run(run(verified=False), scenario),
        )

        assert card.accuracy == 1.0
        assert card.resolution_rate == 0.5

    def test_the_median_ignores_unfinished_runs(self, scenario: Scenario) -> None:
        unfinished = run()
        unfinished.finished_at = None

        card = self.build(
            score_run(run(seconds=60), scenario),
            score_run(run(seconds=120), scenario),
            score_run(unfinished, scenario),
        )

        assert card.median_seconds == 90

    def test_runs_that_executed_nothing_are_called_out(self, scenario: Scenario) -> None:
        # A confident answer reached purely by reading is the failure mode this
        # benchmark exists to catch.
        card = self.build(
            score_run(run(experiments=0), scenario),
            score_run(run(experiments=4), scenario),
        )

        assert card.silent_runs == 1

    def test_an_empty_scorecard_does_not_divide_by_zero(self) -> None:
        card = self.build()

        assert card.accuracy == 0.0
        assert card.resolution_rate == 0.0
        assert card.median_seconds is None
        assert "No runs scored" in card.to_markdown()

    def test_the_markdown_reports_every_metric(self, scenario: Scenario) -> None:
        card = self.build(score_run(run(), scenario), score_run(run(unsafe=1), scenario))
        markdown = card.to_markdown()

        assert "Root-cause accuracy: **100%**" in markdown
        assert "Verified resolution rate: **100%**" in markdown
        assert "Unsafe attempts: **1**" in markdown
        assert "| checkout-promo-none |" in markdown


class TestRoundTrip:
    def test_a_saved_trace_can_be_re_scored(self, scenario: Scenario, tmp_path) -> None:  # type: ignore[no-untyped-def]
        # Scores have to be reproducible from disk, or a published number
        # cannot be checked by anyone else.
        original = run()
        trace = original.save(tmp_path)

        reloaded = RunState.load(trace)
        assert score_run(reloaded, scenario) == score_run(original, scenario)


class TestMatchingRunsToScenarios:
    """Which answer key a run is graded against.

    This is where the benchmark can quietly lie to itself. Both scenarios fire
    the same alert on purpose, so anything that identifies a scenario by its
    symptom will grade half the runs against the wrong ground truth and report
    the agent as wrong when it was right.
    """

    @pytest.fixture
    def ledger(self) -> list[dict[str, object]]:
        return [
            {"at": "2026-08-19T10:00:00+00:00", "scenario": "checkout-promo-none"},
            {"at": "2026-08-19T10:30:00+00:00", "scenario": None},
            {"at": "2026-08-19T11:00:00+00:00", "scenario": "checkout-shipping-lookup"},
        ]

    def test_a_run_matches_the_fault_that_was_live(self, ledger: list[dict[str, object]]) -> None:
        assert scenario_live_at("2026-08-19T10:15:00+00:00", ledger) == "checkout-promo-none"
        assert scenario_live_at("2026-08-19T11:20:00+00:00", ledger) == "checkout-shipping-lookup"

    def test_two_scenarios_behind_one_alert_are_told_apart(
        self, ledger: list[dict[str, object]]
    ) -> None:
        first = scenario_live_at("2026-08-19T10:15:00+00:00", ledger)
        second = scenario_live_at("2026-08-19T11:20:00+00:00", ledger)
        assert first != second

    def test_a_healthy_window_matches_nothing(self, ledger: list[dict[str, object]]) -> None:
        # Between a restore and the next injection there is no ground truth,
        # and inventing one would score a run against a fault that was not
        # there.
        assert scenario_live_at("2026-08-19T10:45:00+00:00", ledger) is None

    def test_a_run_before_any_injection_matches_nothing(
        self, ledger: list[dict[str, object]]
    ) -> None:
        assert scenario_live_at("2026-08-19T09:00:00+00:00", ledger) is None
