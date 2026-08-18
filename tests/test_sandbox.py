"""Tests for the sandbox, the patch limits, and verification.

Run against LocalSandbox rather than Docker so CI needs no daemon. What matters
here is the ordering and the refusals — that a limit stops a patch before any
code runs, and that a fix is only ever called verified when the failure was
demonstrably present first.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from sev0.sandbox.models import ExecResult, parse_pytest
from sev0.sandbox.patch import (
    Patch,
    PatchBuilder,
    PatchError,
    PatchLimits,
    apply,
    revert,
    validate,
)
from sev0.sandbox.runner import LocalSandbox, SandboxError
from sev0.sandbox.verify import run_tests, scratch_copy, verify_patch

CART = '''\
PROMOTIONS = {"SAVE10": 10}


def compute_total(subtotal, code):
    percent = PROMOTIONS.get(code) if code else 0
    return subtotal - subtotal * percent // 100
'''

FIXED = '''\
PROMOTIONS = {"SAVE10": 10}


def compute_total(subtotal, code):
    percent = PROMOTIONS.get(code) if code else 0
    if percent is None:
        percent = 0
    return subtotal - subtotal * percent // 100
'''

SUITE = '''\
from cart import compute_total


def test_known_code_discounts():
    assert compute_total(1000, "SAVE10") == 900


def test_unknown_code_is_ignored():
    assert compute_total(1000, "NOPE") == 1000
'''


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "cart.py").write_text(CART)
    (tmp_path / "test_cart.py").write_text(SUITE)
    (tmp_path / "migrations").mkdir()
    (tmp_path / "migrations" / "0001_init.py").write_text("# do not touch\n")
    return tmp_path


@pytest.fixture
def sandbox() -> LocalSandbox:
    return LocalSandbox()


def fix() -> Patch:
    return (
        PatchBuilder(rationale="guard the unknown-code path")
        .edit(
            "cart.py",
            "    percent = PROMOTIONS.get(code) if code else 0\n",
            (
                "    percent = PROMOTIONS.get(code) if code else 0\n"
                "    if percent is None:\n"
                "        percent = 0\n"
            ),
        )
        .build()
    )


class TestLocalSandbox:
    def test_a_successful_command_reports_output(self, sandbox: LocalSandbox, repo: Path) -> None:
        result = sandbox.run([sys.executable, "-c", "print('hello')"], workdir=repo)

        assert result.ok
        assert "hello" in result.stdout

    def test_a_failing_command_reports_its_exit_code(
        self, sandbox: LocalSandbox, repo: Path
    ) -> None:
        result = sandbox.run([sys.executable, "-c", "raise SystemExit(3)"], workdir=repo)

        assert not result.ok
        assert result.exit_code == 3

    def test_a_hanging_command_is_killed(self, sandbox: LocalSandbox, repo: Path) -> None:
        result = sandbox.run(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            workdir=repo,
            timeout_seconds=1,
        )

        assert result.timed_out
        assert not result.ok

    def test_a_missing_binary_is_reported_clearly(
        self, sandbox: LocalSandbox, repo: Path
    ) -> None:
        with pytest.raises(SandboxError, match="command not found"):
            sandbox.run(["definitely-not-a-real-binary"], workdir=repo)


class TestPytestParsing:
    def test_counts_and_failing_ids_are_both_read(self) -> None:
        output = (
            "FAILED test_cart.py::test_unknown_code_is_ignored - AssertionError\n"
            "1 failed, 1 passed in 0.05s\n"
        )
        run = parse_pytest(ExecResult(1, output, "", 0.1))

        assert run.passed == 1
        assert run.failed == 1
        assert run.failing_tests == ("test_cart.py::test_unknown_code_is_ignored",)
        assert not run.green

    def test_a_green_run_is_green(self) -> None:
        run = parse_pytest(ExecResult(0, "2 passed in 0.02s\n", "", 0.1))

        assert run.green
        assert run.total == 2

    def test_collection_errors_are_counted(self) -> None:
        output = "ERROR test_cart.py\n1 error in 0.01s\n"
        run = parse_pytest(ExecResult(2, output, "", 0.1))

        assert run.errors == 1
        assert not run.green

    def test_a_timeout_is_never_green(self) -> None:
        run = parse_pytest(ExecResult(124, "", "killed", 1.0, timed_out=True))
        assert not run.green


class TestPatchLimits:
    def test_a_valid_patch_has_no_violations(self, repo: Path) -> None:
        assert validate(fix(), repo, PatchLimits()) == ()

    def test_an_empty_patch_is_refused(self, repo: Path) -> None:
        violations = validate(Patch(edits=()), repo, PatchLimits())
        assert any(v.rule == "empty" for v in violations)

    def test_too_many_files_is_refused(self, repo: Path) -> None:
        for name in "abcdef":
            (repo / f"{name}.py").write_text("x = 1\n")
        builder = PatchBuilder()
        for name in "abcdef":
            builder.edit(f"{name}.py", "x = 1\n", "x = 2\n")

        violations = validate(builder.build(), repo, PatchLimits(max_files=5))
        assert any(v.rule == "too many files" for v in violations)

    def test_too_large_a_change_is_refused(self, repo: Path) -> None:
        violations = validate(fix(), repo, PatchLimits(max_lines=1))
        assert any(v.rule == "too large" for v in violations)

    def test_a_protected_path_is_refused(self, repo: Path) -> None:
        patch = PatchBuilder().edit(
            "migrations/0001_init.py", "# do not touch\n", "# touched\n"
        ).build()

        violations = validate(patch, repo, PatchLimits())
        assert any(v.rule == "protected path" for v in violations)

    def test_escaping_the_repository_is_refused(self, repo: Path) -> None:
        for path in ("../outside.py", "/etc/passwd"):
            patch = PatchBuilder().edit(path, "a", "b").build()
            violations = validate(patch, repo, PatchLimits())
            assert any(v.rule == "outside repository" for v in violations), path

    def test_an_anchor_that_matches_nothing_is_refused(self, repo: Path) -> None:
        patch = PatchBuilder().edit("cart.py", "not in the file", "x").build()
        violations = validate(patch, repo, PatchLimits())
        assert any(v.rule == "anchor not found" for v in violations)

    def test_an_ambiguous_anchor_is_refused(self, repo: Path) -> None:
        # An anchor matching twice means the edit is not specific enough to be
        # applied deliberately.
        (repo / "dupes.py").write_text("value = 1\nvalue = 1\n")
        patch = PatchBuilder().edit("dupes.py", "value = 1\n", "value = 2\n").build()

        violations = validate(patch, repo, PatchLimits())
        assert any(v.rule == "ambiguous anchor" for v in violations)

    def test_apply_refuses_a_patch_that_breaks_a_limit(self, repo: Path) -> None:
        patch = PatchBuilder().edit(
            "migrations/0001_init.py", "# do not touch\n", "# touched\n"
        ).build()

        with pytest.raises(PatchError, match="protected path"):
            apply(patch, repo, PatchLimits())

        assert (repo / "migrations" / "0001_init.py").read_text() == "# do not touch\n"


class TestApplyAndRevert:
    def test_applying_changes_the_file(self, repo: Path) -> None:
        apply(fix(), repo, PatchLimits())
        assert (repo / "cart.py").read_text() == FIXED

    def test_reverting_restores_the_original(self, repo: Path) -> None:
        previous = apply(fix(), repo, PatchLimits())
        revert(previous, repo)
        assert (repo / "cart.py").read_text() == CART


class TestVerification:
    def test_a_real_fix_is_verified(self, sandbox: LocalSandbox, repo: Path) -> None:
        result = verify_patch(sandbox, repo, fix(), PatchLimits())

        assert result.reproduced
        assert result.verified
        assert result.fixed == ("test_cart.py::test_unknown_code_is_ignored",)
        assert result.broke == ()

    def test_the_real_tree_is_never_touched(self, sandbox: LocalSandbox, repo: Path) -> None:
        verify_patch(sandbox, repo, fix(), PatchLimits())
        assert (repo / "cart.py").read_text() == CART

    def test_a_patch_that_breaks_another_test_is_not_verified(
        self, sandbox: LocalSandbox, repo: Path
    ) -> None:
        # Returns the subtotal always: the unknown-code test passes, and the
        # discount test starts failing. Exactly the shape of a plausible fix
        # that makes things worse.
        sloppy = (
            PatchBuilder()
            .edit(
                "cart.py",
                "    return subtotal - subtotal * percent // 100\n",
                "    return subtotal\n",
            )
            .build()
        )

        result = verify_patch(sandbox, repo, sloppy, PatchLimits())

        assert result.reproduced
        assert not result.verified
        assert "test_known_code_discounts" in result.broke[0]

    def test_a_healthy_tree_means_the_failure_did_not_reproduce(
        self, sandbox: LocalSandbox, repo: Path
    ) -> None:
        (repo / "cart.py").write_text(FIXED)

        harmless = (
            PatchBuilder()
            .edit(
                "cart.py",
                "def compute_total(subtotal, code):\n",
                "def compute_total(subtotal, code):\n    # no behaviour change\n",
            )
            .build()
        )
        result = verify_patch(sandbox, repo, harmless, PatchLimits())

        assert not result.reproduced
        assert not result.verified
        assert "already green" in result.note

    def test_a_limit_violation_runs_nothing_at_all(
        self, sandbox: LocalSandbox, repo: Path
    ) -> None:
        patch = PatchBuilder().edit(
            "migrations/0001_init.py", "# do not touch\n", "# touched\n"
        ).build()

        result = verify_patch(sandbox, repo, patch, PatchLimits())

        assert result.before is None
        assert result.after is None
        assert not result.verified
        assert "protected path" in result.render()

    def test_scratch_copies_leave_no_trace(self, repo: Path) -> None:
        with scratch_copy(repo) as scratch:
            assert (scratch / "cart.py").exists()
            location = scratch

        assert not location.exists()

    def test_run_tests_reports_the_failing_id(
        self, sandbox: LocalSandbox, repo: Path
    ) -> None:
        run = run_tests(sandbox, repo)

        assert not run.green
        assert any("test_unknown_code_is_ignored" in name for name in run.failing_tests)
