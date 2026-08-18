"""Tests for the fault injection harness.

The harness is the thing that decides whether the agent is any good, so a bug
in here is worse than a bug in the agent: it would produce confident numbers
that mean nothing.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from incident_lab import target as target_repo
from incident_lab.scenarios import model

APP_DIR = Path(__file__).resolve().parent.parent / "incident_lab" / "app"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return target_repo.materialize(APP_DIR, tmp_path / "target")


def load_compute_total(repo: Path):  # type: ignore[no-untyped-def]
    """Import compute_total freshly from a given working tree."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        f"cart_under_test_{repo.name}_{id(repo)}",
        repo / "services" / "cart" / "main.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.compute_total


def commit_fault(repo: Path, scenario: model.Scenario) -> None:
    for edit in scenario.edits:
        edit.apply(repo)
    target_repo.git(repo, "add", "-A")
    target_repo.git(repo, "commit", "-q", "-m", scenario.commit_message)


class TestPristineSource:
    def test_the_storefront_suite_is_green_before_any_fault(self) -> None:
        """The baseline must be healthy, or nothing downstream means anything.

        This replaces a copy of the pricing assertions that used to live here.
        Two suites asserting the same numbers drift, and the one in the sev0
        repo was the one that could drift silently — it never travels with the
        code it describes.
        """
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "tests"],
            cwd=APP_DIR,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(APP_DIR)},
        )

        assert result.returncode == 0, result.stdout + result.stderr


class TestScenarios:
    def test_every_scenario_loads(self) -> None:
        scenarios = model.load_all()
        assert scenarios, "no scenarios defined"

    def test_every_edit_anchor_matches_the_pristine_source_exactly(self) -> None:
        for scenario in model.load_all().values():
            for edit in scenario.edits:
                source = (APP_DIR / edit.file).read_text()
                assert source.count(edit.find) == 1, (
                    f"{scenario.id}: anchor for {edit.file} does not match exactly once. "
                    "The source drifted and this scenario would fail to inject."
                )

    def test_every_scenario_carries_ground_truth(self) -> None:
        for scenario in model.load_all().values():
            truth = scenario.ground_truth
            assert truth.service and truth.file and truth.symbol
            assert truth.summary.strip()

    def test_unknown_scenario_is_rejected_with_a_useful_message(self) -> None:
        with pytest.raises(model.ScenarioError, match="known scenarios"):
            model.load("no-such-scenario")

    def test_an_anchor_that_matches_nothing_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "sample.py"
        path.write_text("a = 1\n")
        edit = model.Edit(file="sample.py", find="b = 2\n", replace="b = 3\n")

        with pytest.raises(model.ScenarioError, match="matched 0 times"):
            edit.apply(tmp_path)


class TestScenarioDiversity:
    def test_scenarios_share_an_alert_but_not_a_cause(self) -> None:
        """Two bugs presenting identically is the point.

        Both scenarios surface as a partial 5xx rate on checkout. If the agent
        could tell them apart from the alert alone, the benchmark would be
        measuring pattern matching rather than diagnosis.
        """
        promo = model.load("checkout-promo-none")
        shipping = model.load("checkout-shipping-lookup")

        assert promo.alert == shipping.alert
        assert promo.ground_truth.symbol != shipping.ground_truth.symbol

    def test_every_scenario_names_a_test_it_should_break(self) -> None:
        # Without a failing assertion there is nothing for a fix to turn green,
        # so verification could never confirm anything.
        for scenario in model.load_all().values():
            assert scenario.failing_tests, f"{scenario.id} names no failing test"

    def test_the_shipping_fault_breaks_the_fallback(self, repo: Path) -> None:
        import importlib.util

        commit_fault(repo, model.load("checkout-shipping-lookup"))

        spec = importlib.util.spec_from_file_location(
            "cart_shipping_under_test", repo / "services" / "cart" / "main.py"
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        # Recognised speeds keep working; only the fallback path is gone.
        assert module.shipping_cents(1000, "standard") == 499
        with pytest.raises(KeyError):
            module.shipping_cents(1000, "Express")


class TestTargetRepo:
    def test_materialise_creates_a_repo_with_history(self, repo: Path) -> None:
        assert target_repo.exists(repo)
        log = target_repo.git(repo, "log", "--oneline").splitlines()
        assert len(log) >= len(target_repo.HISTORY)

    def test_a_fresh_target_is_clean_and_at_baseline(self, repo: Path) -> None:
        assert target_repo.at_baseline(repo)
        assert not target_repo.is_dirty(repo)

    def test_materialise_is_idempotent(self, repo: Path) -> None:
        before = target_repo.head(repo)
        target_repo.materialize(APP_DIR, repo)
        assert target_repo.head(repo) == before

    def test_the_sev0_repo_is_never_the_target(self) -> None:
        # A scenario writing into the project's own source would push planted
        # bugs to the remote. Worth an explicit guard.
        from incident_lab import cli

        assert cli.TARGET_DIR.is_relative_to(cli.RUN_DIR)
        assert not cli.TARGET_DIR.is_relative_to(cli.APP_DIR)


class TestInjectAndRestore:
    def test_injecting_breaks_the_unknown_promo_path(self, repo: Path) -> None:
        scenario = model.load("checkout-promo-none")
        items = [{"price_cents": 12900, "quantity": 1}]

        healthy = load_compute_total(repo)
        assert healthy(items, "NOT-A-CODE")["total_cents"] == 12900

        commit_fault(repo, scenario)

        faulted = load_compute_total(repo)
        with pytest.raises(TypeError):
            faulted(items, "NOT-A-CODE")

    def test_valid_codes_still_work_while_faulted(self, repo: Path) -> None:
        # A fault that broke everything would be trivially detectable. This one
        # has to stay partial to be worth diagnosing.
        commit_fault(repo, model.load("checkout-promo-none"))
        compute_total = load_compute_total(repo)

        items = [{"price_cents": 12900, "quantity": 1}]
        assert compute_total(items, "SAVE10")["total_cents"] == 11610
        assert compute_total(items, None)["total_cents"] == 12900

    def test_the_fault_lands_as_a_real_commit(self, repo: Path) -> None:
        scenario = model.load("checkout-promo-none")
        commit_fault(repo, scenario)

        assert not target_repo.at_baseline(repo)
        assert target_repo.head(repo).subject == scenario.commit_message.splitlines()[0]

    def test_restore_returns_the_tree_to_health(self, repo: Path) -> None:
        commit_fault(repo, model.load("checkout-promo-none"))
        target_repo.reset_to_baseline(repo)

        assert target_repo.at_baseline(repo)
        assert not target_repo.is_dirty(repo)

        compute_total = load_compute_total(repo)
        assert compute_total([{"price_cents": 12900, "quantity": 1}], "NOT-A-CODE")

    def test_restore_survives_a_dirty_tree(self, repo: Path) -> None:
        (repo / "services" / "cart" / "main.py").write_text("garbage\n")
        (repo / "stray.txt").write_text("left behind\n")

        target_repo.reset_to_baseline(repo)

        assert not (repo / "stray.txt").exists()
        assert target_repo.at_baseline(repo)

    def test_a_missing_target_repo_is_reported_clearly(self, repo: Path) -> None:
        shutil.rmtree(repo / ".git")
        with pytest.raises(target_repo.TargetRepoError, match="no target repository"):
            target_repo.reset_to_baseline(repo)
