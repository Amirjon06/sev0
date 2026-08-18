"""Proving a fix, rather than believing one.

The order matters and is not negotiable. Reproduce the failure first: if the
suite is already green, whatever the agent found is not what broke production,
and applying a patch would be changing working code for a story. Only once the
failure is demonstrably present does the patch go on, and only then does a
second run mean anything.

Everything happens in a throwaway copy. The repository under investigation is
never modified, so a run that dies halfway leaves nothing behind.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from sev0.sandbox.models import TestRun, VerificationResult, parse_pytest
from sev0.sandbox.patch import Patch, PatchLimits, apply, unified_diff, validate
from sev0.sandbox.runner import Sandbox

IGNORE = shutil.ignore_patterns(
    ".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv"
)


@contextmanager
def scratch_copy(repo: Path) -> Iterator[Path]:
    """A disposable copy of the tree, without its history or caches."""
    with tempfile.TemporaryDirectory(prefix="sev0-verify-") as temporary:
        destination = Path(temporary) / repo.name
        shutil.copytree(repo, destination, ignore=IGNORE)
        yield destination


def run_tests(
    sandbox: Sandbox,
    repo: Path,
    selectors: Sequence[str] = (),
    timeout_seconds: int = 600,
) -> TestRun:
    command = ["python", "-m", "pytest", "-q", "--tb=short", "-rf", *selectors]
    return parse_pytest(sandbox.run(command, workdir=repo, timeout_seconds=timeout_seconds))


def verify_patch(
    sandbox: Sandbox,
    repo: Path,
    patch: Patch,
    limits: PatchLimits | None = None,
    selectors: Sequence[str] = (),
    timeout_seconds: int = 600,
) -> VerificationResult:
    """Reproduce, patch, re-run, and report what actually changed."""
    limits = limits or PatchLimits()

    violations = validate(patch, repo, limits)
    if violations:
        # Nothing is executed. A patch that breaks a limit does not get to run
        # code just to find out whether it would have worked.
        return VerificationResult(reproduced=False, before=None, after=None, violations=violations)

    with scratch_copy(repo) as scratch:
        before = run_tests(sandbox, scratch, selectors, timeout_seconds)

        if before.timed_out:
            return VerificationResult(
                reproduced=False,
                before=before,
                after=None,
                note="the suite timed out before the patch was applied",
            )

        if before.green:
            return VerificationResult(
                reproduced=False,
                before=before,
                after=None,
                note="the suite is already green, so this failure is not the one in production",
            )

        apply(patch, scratch, limits)
        after = run_tests(sandbox, scratch, selectors, timeout_seconds)

        failing_before = set(before.failing_tests)
        failing_after = set(after.failing_tests)

        return VerificationResult(
            reproduced=True,
            before=before,
            after=after,
            fixed=tuple(sorted(failing_before - failing_after)),
            broke=tuple(sorted(failing_after - failing_before)),
        )


def diff_for(patch: Patch, repo: Path, limits: PatchLimits | None = None) -> str:
    """The patch rendered as a unified diff, without touching the real tree."""
    with scratch_copy(repo) as scratch:
        previous = apply(patch, scratch, limits or PatchLimits())
        return unified_diff(previous, scratch)
