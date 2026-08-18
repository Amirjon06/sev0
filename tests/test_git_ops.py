"""Tests for branching, committing, and the pull request body.

The refusals matter more than the happy path. An agent that can commit to main
or merge its own work is a different and much worse tool, so those are asserted
rather than assumed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from sev0.agent.state import (
    Confidence,
    ProposedFix,
    RootCause,
    RunState,
    Verdict,
)
from sev0.git_ops import pull_request as pr
from sev0.git_ops import repository as repo_ops
from sev0.sandbox.patch import PatchBuilder, PatchLimits

ORIGINAL = "def total(subtotal, percent):\n    return subtotal - percent\n"
REPLACEMENT = (
    "def total(subtotal, percent):\n"
    "    if percent is None:\n"
    "        percent = 0\n"
    "    return subtotal - percent\n"
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "cart.py").write_text(ORIGINAL)
    (tmp_path / "migrations").mkdir()
    (tmp_path / "migrations" / "0001.py").write_text("# untouchable\n")

    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.name", "Storefront Team"],
        ["config", "user.email", "team@storefront.example"],
        ["add", "-A"],
        ["commit", "-q", "-m", "feat(cart): Add totals"],
    ):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    return tmp_path


def fix_patch() -> object:
    return (
        PatchBuilder(rationale="guard the null percent")
        .edit("cart.py", ORIGINAL, REPLACEMENT)
        .build()
    )


def finished_state() -> RunState:
    state = RunState(incident="checkout-5xx")
    state.add_hypothesis("The connection pool is exhausted", Verdict.REJECTED, "peaked at 34%")
    state.add_hypothesis("An unknown promo code is unhandled", Verdict.CONFIRMED, "reproduced")
    state.record_call("run_snippet", {}, "TypeError", failed=False)
    state.record_call("search_code", {}, "1 match", failed=False)
    state.proposed_fix = ProposedFix(
        path="cart.py",
        find=ORIGINAL,
        replace=REPLACEMENT,
        rationale="An inactive code yields None, which the subtraction cannot take.",
        verified=True,
        verification="before: 6 passed, 1 failed\nafter:  7 passed\nverified",
    )
    state.conclude(
        RootCause(
            service="cart",
            file="cart.py",
            symbol="total",
            commit="a3f9c21",
            explanation="The guard was removed, so an unknown code reaches the arithmetic.",
            confidence=Confidence.HIGH,
        )
    )
    return state


class TestSubjects:
    def test_a_subject_is_conventional_and_short(self) -> None:
        subject = repo_ops.commit_subject("cart", "guard against an inactive promotion code")

        assert subject.startswith("fix(cart): ")
        assert len(subject) <= 50
        assert not subject.endswith(".")

    def test_a_long_summary_is_cut_at_a_word_boundary(self) -> None:
        subject = repo_ops.commit_subject("cart", "handle the unrecognised promotional code case")

        assert len(subject) <= 50
        assert not subject.endswith("-")
        assert " " in subject

    def test_an_empty_summary_still_produces_something_readable(self) -> None:
        assert repo_ops.commit_subject("cart", "  ") == "fix(cart): Repair the failing path"

    def test_branch_names_are_namespaced_and_unique(self) -> None:
        name = repo_ops.branch_name("checkout-5xx", "abc123def456")

        assert name.startswith("sev0/")
        assert "abc123de" in name


class TestCommitFix:
    def test_a_fix_lands_on_its_own_branch(self, repo: Path) -> None:
        commit = repo_ops.commit_fix(
            repo,
            fix_patch(),  # type: ignore[arg-type]
            branch="sev0/checkout-5xx-abc123",
            subject="fix(cart): Guard the null percent",
            body="Because it was None.",
        )

        assert commit.branch == "sev0/checkout-5xx-abc123"
        assert commit.files == ("cart.py",)
        assert (repo / "cart.py").read_text() == REPLACEMENT

    def test_main_is_left_untouched(self, repo: Path) -> None:
        repo_ops.commit_fix(
            repo,
            fix_patch(),  # type: ignore[arg-type]
            branch="sev0/checkout-5xx-abc123",
            subject="fix(cart): Guard the null percent",
            body="Because it was None.",
        )
        repo_ops.git(repo, "checkout", "-q", "main")

        assert (repo / "cart.py").read_text() == ORIGINAL

    def test_committing_to_the_base_branch_is_refused(self, repo: Path) -> None:
        with pytest.raises(repo_ops.GitOpsError, match="refusing to commit directly"):
            repo_ops.commit_fix(
                repo,
                fix_patch(),  # type: ignore[arg-type]
                branch="main",
                subject="fix: whatever",
                body="",
            )

    def test_an_unnamespaced_branch_is_refused(self, repo: Path) -> None:
        with pytest.raises(repo_ops.GitOpsError, match="must start with sev0/"):
            repo_ops.commit_fix(
                repo,
                fix_patch(),  # type: ignore[arg-type]
                branch="hotfix",
                subject="fix: whatever",
                body="",
            )

    def test_a_dirty_tree_is_refused(self, repo: Path) -> None:
        (repo / "cart.py").write_text("someone was mid-edit\n")

        with pytest.raises(repo_ops.GitOpsError, match="uncommitted changes"):
            repo_ops.commit_fix(
                repo,
                fix_patch(),  # type: ignore[arg-type]
                branch="sev0/x-1",
                subject="fix: whatever",
                body="",
            )

    def test_a_rejected_patch_leaves_no_branch_behind(self, repo: Path) -> None:
        forbidden = (
            PatchBuilder().edit("migrations/0001.py", "# untouchable\n", "# touched\n").build()
        )

        with pytest.raises(Exception, match="protected path"):
            repo_ops.commit_fix(
                repo,
                forbidden,
                branch="sev0/bad-1",
                subject="fix: nope",
                body="",
                limits=PatchLimits(),
            )

        assert "sev0/bad-1" not in repo_ops.git(repo, "branch", "--list")
        assert repo_ops.current_branch(repo) == "main"
        assert not repo_ops.is_dirty(repo)

    def test_the_diff_is_readable(self, repo: Path) -> None:
        repo_ops.commit_fix(
            repo,
            fix_patch(),  # type: ignore[arg-type]
            branch="sev0/x-1",
            subject="fix(cart): Guard the null percent",
            body="",
        )

        diff = repo_ops.diff_against(repo, "main")
        assert "+    if percent is None:" in diff


class TestPullRequestBody:
    def test_the_root_cause_is_stated_first(self) -> None:
        body = pr.render_body(finished_state())

        assert body.index("What broke") < body.index("What was ruled out")
        assert "`cart.py`" in body
        assert "a3f9c21" in body

    def test_rejected_hypotheses_are_included(self) -> None:
        # A pull request that shows only the answer looks more confident and is
        # worth less to the person reviewing it.
        body = pr.render_body(finished_state())

        assert "The connection pool is exhausted" in body
        assert "rejected" in body
        assert "peaked at 34%" in body

    def test_the_verification_output_is_quoted_verbatim(self) -> None:
        body = pr.render_body(finished_state())

        assert "before: 6 passed, 1 failed" in body
        assert "after:  7 passed" in body

    def test_experiment_count_is_reported(self) -> None:
        body = pr.render_body(finished_state())
        assert "**1 executed code**" in body

    def test_an_unverified_fix_is_flagged_loudly(self) -> None:
        state = finished_state()
        assert state.proposed_fix is not None
        state.proposed_fix.verified = False

        assert "**not verified**" in pr.render_body(state)

    def test_a_run_with_no_root_cause_says_so(self) -> None:
        state = RunState(incident="checkout-5xx")
        state.abandon("budget exhausted")

        body = pr.render_body(state)
        assert "No root cause was established" in body

    def test_the_diff_is_collapsed_not_omitted(self) -> None:
        body = pr.render_body(finished_state(), diff="--- a/cart.py\n+++ b/cart.py\n")

        assert "<details>" in body
        assert "```diff" in body

    def test_the_title_names_the_service_and_symbol(self) -> None:
        assert pr.render_title(finished_state()) == "fix(cart): Repair total for checkout-5xx"

    def test_a_failed_run_gets_an_honest_title(self) -> None:
        state = RunState(incident="checkout-5xx")
        assert "no root cause" in pr.render_title(state)


class TestOpeningOnGitHub:
    def test_a_missing_token_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        request = pr.build(finished_state(), branch="sev0/x-1", base="main")

        with pytest.raises(pr.PullRequestError, match="GITHUB_TOKEN"):
            pr.open_on_github(request, repository="owner/repo")

    def test_opening_from_the_base_branch_is_refused(self) -> None:
        request = pr.build(finished_state(), branch="main", base="main")

        with pytest.raises(pr.PullRequestError, match="refusing"):
            pr.open_on_github(request, repository="owner/repo", token="fake")
