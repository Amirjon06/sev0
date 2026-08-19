"""Invariants every benchmark scenario has to hold.

A scenario that does not break what it claims to break, or that leaks its own
answer, is worse than no scenario: it produces a number that looks like a
measurement and is not one. These run against the pristine storefront source
and a scratch copy of the target repository, so they need neither Docker nor a
model.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from incident_lab import target as target_repo
from incident_lab.scenarios import model

APP = Path(__file__).resolve().parent.parent / "incident_lab" / "app"
SCENARIOS = model.load_all()
SCENARIO_IDS = sorted(SCENARIOS)

# Words that would hand the agent the answer if they appeared somewhere it can
# read. Drawn from the ground truth of the scenarios themselves.
LEAKY_TERMS = ("root cause", "ground truth", "answer", "the bug is", "injected fault")


@pytest.fixture(scope="module")
def pristine(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A materialised target repository at baseline, shared across the module."""
    root = tmp_path_factory.mktemp("bench-target")
    return target_repo.materialize(APP, root / "target")


def run_storefront_tests(
    repo: Path, selectors: tuple[str, ...] = ()
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-header", *(selectors or ("tests",))],
        cwd=repo,
        capture_output=True,
        text=True,
    )


class TestSuiteShape:
    def test_the_suite_is_large_enough_to_mean_something(self) -> None:
        assert len(SCENARIOS) >= 20

    def test_every_fault_family_is_represented(self) -> None:
        families = {s.family for s in SCENARIOS.values()}
        assert {"code", "config", "infra"} <= families

    def test_adversarial_scenarios_exist(self) -> None:
        adversarial = [s for s in SCENARIOS.values() if s.is_adversarial]
        assert len(adversarial) >= 4

    def test_the_adversarial_set_covers_distinct_traps(self) -> None:
        # Five near-identical decoys would test one thing five times.
        traps = {tag for s in SCENARIOS.values() if s.is_adversarial for tag in s.tags}
        assert {"decoy-commit", "decoy-logs", "regression-trap", "protected-path"} <= traps

    def test_faults_are_spread_across_services(self) -> None:
        services = {s.ground_truth.service for s in SCENARIOS.values()}
        assert len(services) >= 4

    def test_faults_are_spread_across_symbols(self) -> None:
        # Twenty variations of one bug is one scenario written twenty times.
        symbols = {s.ground_truth.symbol for s in SCENARIOS.values()}
        assert len(symbols) >= 12

    def test_some_faults_produce_no_error_rate_at_all(self) -> None:
        # A benchmark made only of crashes measures reading tracebacks.
        silent = [s for s in SCENARIOS.values() if "silent" in s.tags]
        assert len(silent) >= 4

    def test_several_scenarios_share_an_alert(self) -> None:
        alerts = [s.alert for s in SCENARIOS.values()]
        assert any(alerts.count(alert) >= 3 for alert in set(alerts))


class TestGroundTruthIsolation:
    """The agent reads the target repository. None of this may reach it."""

    @pytest.mark.parametrize("scenario_id", SCENARIO_IDS)
    def test_the_id_does_not_name_the_faulty_symbol(self, scenario_id: str) -> None:
        symbol = SCENARIOS[scenario_id].ground_truth.symbol.lower()
        assert symbol not in scenario_id.lower().replace("-", "_")

    @pytest.mark.parametrize("scenario_id", SCENARIO_IDS)
    def test_no_commit_message_admits_to_planting_a_fault(self, scenario_id: str) -> None:
        for change in SCENARIOS[scenario_id].changes:
            lowered = change.message.lower()
            for term in LEAKY_TERMS:
                assert term not in lowered, f"{scenario_id}: {term!r} in a commit message"

    @pytest.mark.parametrize("scenario_id", SCENARIO_IDS)
    def test_no_commit_message_quotes_the_ground_truth_summary(self, scenario_id: str) -> None:
        scenario = SCENARIOS[scenario_id]
        first_line = scenario.ground_truth.summary.strip().splitlines()[0].lower()
        for change in scenario.changes:
            assert first_line not in change.message.lower()

    @pytest.mark.parametrize("scenario_id", SCENARIO_IDS)
    def test_the_alert_name_is_a_symptom_not_a_diagnosis(self, scenario_id: str) -> None:
        scenario = SCENARIOS[scenario_id]
        alert = scenario.alert.lower()
        assert scenario.ground_truth.symbol.lower() not in alert.replace("-", "_")
        assert scenario.ground_truth.service.lower() not in alert or alert.count("-") > 0

    def test_the_answer_key_never_reaches_the_target_repository(self, pristine: Path) -> None:
        # Scenario files live in the sev0 repository. The target is built from
        # storefront source alone, so there is nothing to read even in principle.
        assert not list(pristine.rglob("*.yaml")) or not any(
            "ground_truth" in path.read_text() for path in pristine.rglob("*.yaml")
        )

    def test_solvability_notes_stay_out_of_the_target(self, pristine: Path) -> None:
        notes = [s.notes.path for s in SCENARIOS.values() if s.notes.path]
        assert notes, "scenarios should document their intended diagnostic path"

        haystack = "\n".join(
            path.read_text(errors="ignore")
            for path in pristine.rglob("*")
            if path.is_file() and ".git" not in path.parts
        )
        for note in notes:
            first = note.strip().splitlines()[0]
            assert first not in haystack


class TestSolvabilityIsDocumented:
    @pytest.mark.parametrize("scenario_id", SCENARIO_IDS)
    def test_every_scenario_says_what_evidence_exposes_it(self, scenario_id: str) -> None:
        notes = SCENARIOS[scenario_id].notes
        assert notes.signal.strip(), f"{scenario_id} documents no observable signal"
        assert notes.path.strip(), f"{scenario_id} documents no diagnostic path"
        assert notes.reproduction.strip(), f"{scenario_id} documents no reproduction"


class TestBaselineHealth:
    def test_the_storefront_is_green_before_anything_is_injected(self, pristine: Path) -> None:
        result = run_storefront_tests(pristine)
        assert result.returncode == 0, result.stdout[-2000:]

    def test_the_target_starts_at_the_baseline_tag(self, pristine: Path) -> None:
        assert target_repo.at_baseline(pristine)


class TestEachScenarioBreaksWhatItClaims:
    """Inject into a scratch target and check the damage is the damage promised.

    This is the expensive part of the suite and the part that matters. A
    scenario whose ground truth points somewhere its edits never touched is a
    benchmark that scores the agent against fiction.
    """

    @pytest.fixture
    def repo(self, tmp_path: Path) -> Path:
        return target_repo.materialize(APP, tmp_path / "target")

    @pytest.mark.parametrize("scenario_id", SCENARIO_IDS)
    def test_the_ground_truth_file_is_one_the_scenario_edits(self, scenario_id: str) -> None:
        scenario = SCENARIOS[scenario_id]
        edited = {edit.file for edit in scenario.edits}
        assert scenario.ground_truth.file in edited

    @pytest.mark.parametrize("scenario_id", SCENARIO_IDS)
    def test_the_ground_truth_symbol_exists_in_the_pristine_source(
        self, scenario_id: str
    ) -> None:
        scenario = SCENARIOS[scenario_id]
        source = (APP / scenario.ground_truth.file).read_text()
        assert scenario.ground_truth.symbol in source

    @pytest.mark.parametrize("scenario_id", SCENARIO_IDS)
    def test_injection_applies_and_restore_undoes_it(
        self, scenario_id: str, repo: Path
    ) -> None:
        scenario = SCENARIOS[scenario_id]
        before = {edit.file: (repo / edit.file).read_text() for edit in scenario.edits}

        for change in scenario.changes:
            for edit in change.edits:
                edit.apply(repo)

        changed = [f for f, text in before.items() if (repo / f).read_text() != text]
        assert changed, f"{scenario_id} injected without changing anything"

        target_repo.reset_to_baseline(repo)
        for path, text in before.items():
            assert (repo / path).read_text() == text
        assert target_repo.at_baseline(repo)

    @pytest.mark.parametrize(
        "scenario_id", [s for s in SCENARIO_IDS if SCENARIOS[s].failing_tests]
    )
    def test_the_named_tests_fail_once_injected_and_pass_before(
        self, scenario_id: str, repo: Path
    ) -> None:
        scenario = SCENARIOS[scenario_id]

        healthy = run_storefront_tests(repo, scenario.failing_tests)
        assert healthy.returncode == 0, (
            f"{scenario_id} names tests that already fail at baseline:\n{healthy.stdout[-1500:]}"
        )

        for change in scenario.changes:
            for edit in change.edits:
                edit.apply(repo)

        broken = run_storefront_tests(repo, scenario.failing_tests)
        assert broken.returncode != 0, (
            f"{scenario_id} claims to break tests that still pass:\n{broken.stdout[-1500:]}"
        )

    @pytest.mark.parametrize(
        "scenario_id",
        [
            s
            for s in SCENARIO_IDS
            if SCENARIOS[s].family == "code"
            and SCENARIOS[s].failing_tests
            and "regression-trap" not in SCENARIOS[s].tags
        ],
    )
    def test_a_code_fault_leaves_the_rest_of_the_suite_alone(
        self, scenario_id: str, repo: Path
    ) -> None:
        # A fault that breaks half the suite is not a subtle regression, it is
        # a broken build, and nobody would need an agent to find it. Counted by
        # test function rather than by case: one behaviour asserted eight ways
        # through parametrize is still one behaviour.
        for change in SCENARIOS[scenario_id].changes:
            for edit in change.edits:
                edit.apply(repo)

        result = run_storefront_tests(repo)
        broken = {
            line.split("::")[-1].split("[")[0]
            for line in result.stdout.splitlines()
            if line.startswith("FAILED")
        }
        assert len(broken) <= 3, f"{scenario_id} broke {sorted(broken)}"

    @pytest.mark.parametrize(
        "scenario_id",
        [s for s in SCENARIO_IDS if "regression-trap" in SCENARIOS[s].tags],
    )
    def test_a_regression_trap_breaks_more_than_its_headline_case(
        self, scenario_id: str, repo: Path
    ) -> None:
        # The whole point of the trap: a patch aimed at one failing test has to
        # satisfy several, so a narrow repair cannot survive verification.
        for change in SCENARIOS[scenario_id].changes:
            for edit in change.edits:
                edit.apply(repo)

        result = run_storefront_tests(repo)
        broken = {
            line.split("::")[-1].split("[")[0]
            for line in result.stdout.splitlines()
            if line.startswith("FAILED")
        }
        assert len(broken) >= 3, f"{scenario_id} is not much of a trap: {sorted(broken)}"

    @pytest.mark.parametrize(
        "scenario_id",
        [s for s in SCENARIO_IDS if SCENARIOS[s].family == "config"],
    )
    def test_a_config_fault_leaves_the_application_suite_green(
        self, scenario_id: str, repo: Path
    ) -> None:
        # This is the defining property of the family: the code is correct and
        # the deployment is not, so the tests cannot see it.
        scenario = SCENARIOS[scenario_id]
        for change in scenario.changes:
            for edit in change.edits:
                edit.apply(repo)

        result = run_storefront_tests(repo)
        assert result.returncode == 0, result.stdout[-1500:]
