"""The agent's experiment tools, run against a real planted fault.

These are the tests that matter most for the project's central claim. Anything
can assert that a function returns a string; what has to be true here is that a
hypothesis can be executed rather than assumed, and that a patch is only ever
called verified after the failure was seen to happen and then seen to stop.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from sev0.agent.state import RunState
from sev0.agent.tools import Toolbox
from sev0.sandbox.patch import PatchLimits
from sev0.sandbox.runner import LocalSandbox

CART = '''\
PROMOTIONS = {"SAVE10": 10, "WELCOME": 5}


def compute_total(subtotal, code):
    percent = PROMOTIONS.get(code) if code else 0
    return subtotal - subtotal * percent // 100
'''

HEALTHY = '''\
PROMOTIONS = {"SAVE10": 10, "WELCOME": 5}


def compute_total(subtotal, code):
    percent = PROMOTIONS.get(code) if code else 0
    if percent is None:
        percent = 0
    return subtotal - subtotal * percent // 100
'''

SUITE = '''\
from cart import compute_total


def test_active_code_discounts():
    assert compute_total(1000, "SAVE10") == 900


def test_expired_code_is_ignored():
    assert compute_total(1000, "SUMMER25") == 1000
'''

BROKEN_LINE = "    percent = PROMOTIONS.get(code) if code else 0\n"
GUARDED = BROKEN_LINE + "    if percent is None:\n        percent = 0\n"


@pytest.fixture
def faulted_repo(tmp_path: Path) -> Path:
    """A tree carrying the same shape of fault Incident Lab plants."""
    (tmp_path / "cart.py").write_text(CART)
    (tmp_path / "test_cart.py").write_text(SUITE)
    (tmp_path / "migrations").mkdir()
    (tmp_path / "migrations" / "0001_init.py").write_text("# untouchable\n")

    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    return tmp_path


@pytest.fixture
def state() -> RunState:
    return RunState(incident="checkout-5xx")


@pytest.fixture
def toolbox(state: RunState, faulted_repo: Path) -> Toolbox:
    return Toolbox(
        state=state,
        repo=faulted_repo,
        sandbox=LocalSandbox(),
        limits=PatchLimits(),
    )


class TestWithoutASandbox:
    def test_experiments_report_that_nothing_can_run(self, state: RunState, tmp_path: Path) -> None:
        toolbox = Toolbox(state=state, repo=tmp_path)
        result, failed = toolbox.invoke("run_snippet", {"code_to_run": "print(1)"})

        assert failed
        assert "No sandbox is configured" in result


class TestRunSnippet:
    def test_a_hypothesis_can_be_executed_rather_than_assumed(self, toolbox: Toolbox) -> None:
        # This is the whole point: the model suspects unknown codes fail, and
        # finds out instead of reasoning about it.
        result, failed = toolbox.invoke(
            "run_snippet",
            {"code_to_run": "from cart import compute_total\nprint(compute_total(1000, 'NOPE'))"},
        )

        assert not failed
        assert "TypeError" in result
        assert "exit code 1" in result

    def test_a_hypothesis_that_does_not_reproduce_says_so(self, toolbox: Toolbox) -> None:
        result, _ = toolbox.invoke(
            "run_snippet",
            {
                "code_to_run": (
                    "from cart import compute_total\nprint(compute_total(1000, 'SAVE10'))"
                )
            },
        )

        assert "exit code 0" in result
        assert "900" in result

    def test_a_snippet_that_hangs_is_killed(self, toolbox: Toolbox) -> None:
        result, _ = toolbox.invoke(
            "run_snippet",
            {"code_to_run": "import time; time.sleep(30)", "timeout_seconds": 1},
        )

        assert "did not finish" in result

    def test_a_snippet_cannot_modify_the_real_tree(
        self, toolbox: Toolbox, faulted_repo: Path
    ) -> None:
        toolbox.invoke(
            "run_snippet",
            {"code_to_run": "open('cart.py', 'w').write('destroyed')"},
        )

        assert (faulted_repo / "cart.py").read_text() == CART


class TestRunTests:
    def test_the_failing_assertion_is_named(self, toolbox: Toolbox) -> None:
        result, failed = toolbox.invoke("run_tests", {})

        assert not failed
        assert "test_expired_code_is_ignored" in result
        assert "1 failed" in result

    def test_selectors_narrow_the_run(self, toolbox: Toolbox) -> None:
        result, _ = toolbox.invoke(
            "run_tests", {"selectors": ["test_cart.py::test_active_code_discounts"]}
        )

        assert "1 passed" in result


class TestTryPatch:
    def test_a_real_fix_verifies(self, toolbox: Toolbox, state: RunState) -> None:
        result, failed = toolbox.invoke(
            "try_patch",
            {
                "path": "cart.py",
                "find": BROKEN_LINE,
                "replace": GUARDED,
                "rationale": "an unknown code yields None, which the multiplication cannot take",
            },
        )

        assert not failed
        assert "verified" in result
        assert state.proposed_fix is not None
        assert state.proposed_fix.verified

    def test_a_fix_that_breaks_another_test_is_refused(
        self, toolbox: Toolbox, state: RunState
    ) -> None:
        result, _ = toolbox.invoke(
            "try_patch",
            {
                "path": "cart.py",
                "find": "    return subtotal - subtotal * percent // 100\n",
                "replace": "    return subtotal\n",
                "rationale": "just return the subtotal",
            },
        )

        assert "BROKE" in result
        assert state.proposed_fix is not None
        assert not state.proposed_fix.verified

    def test_a_failed_attempt_is_still_recorded(self, toolbox: Toolbox, state: RunState) -> None:
        # A rejected patch is evidence about what was tried, not something to
        # quietly discard.
        toolbox.invoke(
            "try_patch",
            {
                "path": "migrations/0001_init.py",
                "find": "# untouchable\n",
                "replace": "# touched\n",
            },
        )

        assert state.proposed_fix is not None
        assert not state.proposed_fix.verified
        assert "protected path" in state.proposed_fix.verification

    def test_a_protected_path_never_runs_anything(self, toolbox: Toolbox) -> None:
        result, _ = toolbox.invoke(
            "try_patch",
            {
                "path": "migrations/0001_init.py",
                "find": "# untouchable\n",
                "replace": "# touched\n",
            },
        )

        assert "rejected before running" in result

    def test_the_real_tree_is_never_patched(self, toolbox: Toolbox, faulted_repo: Path) -> None:
        toolbox.invoke(
            "try_patch",
            {"path": "cart.py", "find": BROKEN_LINE, "replace": GUARDED},
        )

        assert (faulted_repo / "cart.py").read_text() == CART

    def test_a_patch_against_healthy_code_does_not_reproduce(
        self, toolbox: Toolbox, faulted_repo: Path
    ) -> None:
        (faulted_repo / "cart.py").write_text(HEALTHY)

        result, _ = toolbox.invoke(
            "try_patch",
            {
                "path": "cart.py",
                "find": "    if percent is None:\n        percent = 0\n",
                "replace": "    if percent is None:\n        percent = 0  # noqa\n",
            },
        )

        assert "did not reproduce" in result


class TestExperimentAccounting:
    def test_reading_tools_are_not_counted_as_experiments(
        self, toolbox: Toolbox, state: RunState
    ) -> None:
        toolbox.invoke("search_code", {"pattern": "PROMOTIONS"})
        toolbox.invoke("file_outline", {"path": "cart.py"})

        assert state.call_count == 2
        assert state.experiments == 0

    def test_executing_tools_are_counted(self, toolbox: Toolbox, state: RunState) -> None:
        toolbox.invoke("run_tests", {})
        toolbox.invoke("run_snippet", {"code_to_run": "print(1)"})

        assert state.experiments == 2

    def test_the_summary_reports_how_much_was_actually_run(
        self, toolbox: Toolbox, state: RunState
    ) -> None:
        # An investigation that executed nothing only ever formed opinions, and
        # the summary should make that visible.
        toolbox.invoke("run_tests", {})
        state.abandon("stopped")

        assert "(1 experiments)" in state.summary()
