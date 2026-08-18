"""Branching and committing a verified fix.

The rules here are deliberately unhelpful to anything trying to move fast. The
agent may create a branch and commit to it. It may not commit to the default
branch, it may not merge, and it may not touch a tree that has uncommitted work
in it. None of that is configurable from inside a run.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from sev0.sandbox.patch import Patch, PatchLimits, apply

BRANCH_PREFIX = "sev0"
SUBJECT_LIMIT = 50


class GitOpsError(RuntimeError):
    pass


@dataclass(frozen=True)
class Commit:
    sha: str
    branch: str
    subject: str
    files: tuple[str, ...]


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if result.returncode != 0:
        raise GitOpsError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def current_branch(repo: Path) -> str:
    return git(repo, "rev-parse", "--abbrev-ref", "HEAD")


def is_dirty(repo: Path) -> bool:
    return bool(git(repo, "status", "--porcelain"))


def slugify(text: str, limit: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:limit].rstrip("-") or "fix"


def branch_name(incident: str, run_id: str) -> str:
    return f"{BRANCH_PREFIX}/{slugify(incident)}-{run_id[:8]}"


def commit_subject(service: str, summary: str) -> str:
    """A Conventional Commit subject, trimmed to the conventional 50 characters."""
    summary = summary.strip().rstrip(".")
    if not summary:
        summary = "Repair the failing path"

    summary = summary[0].upper() + summary[1:]
    prefix = f"fix({service}): " if service else "fix: "

    room = SUBJECT_LIMIT - len(prefix)
    if len(summary) > room:
        # Cut at a word boundary rather than mid-word; a truncated subject that
        # reads as a phrase is better than one that reads as a typo.
        summary = summary[:room].rsplit(" ", 1)[0].rstrip(",;:")

    return f"{prefix}{summary}"


def commit_fix(
    repo: Path,
    patch: Patch,
    *,
    branch: str,
    subject: str,
    body: str,
    base_branch: str = "main",
    limits: PatchLimits | None = None,
) -> Commit:
    """Create a branch, apply the patch, and commit it.

    Refuses to work on a dirty tree. Committing on top of someone else's
    uncommitted changes would put work in the diff that nothing verified.
    """
    if branch == base_branch:
        raise GitOpsError(f"refusing to commit directly to {base_branch}")
    if not branch.startswith(f"{BRANCH_PREFIX}/"):
        raise GitOpsError(f"branch must start with {BRANCH_PREFIX}/, got {branch!r}")
    if is_dirty(repo):
        raise GitOpsError("the working tree has uncommitted changes")

    starting_point = current_branch(repo)
    git(repo, "checkout", "-q", "-b", branch)

    try:
        apply(patch, repo, limits or PatchLimits())
        git(repo, "add", *patch.files)
        git(repo, "commit", "-q", "-m", subject, "-m", body)
    except Exception:
        # Leave nothing behind on failure. A half-made branch is worse than
        # none, because the next run will trip over it.
        git(repo, "checkout", "-q", "--force", starting_point)
        git(repo, "branch", "-q", "-D", branch)
        raise

    return Commit(
        sha=git(repo, "rev-parse", "--short", "HEAD"),
        branch=branch,
        subject=subject,
        files=patch.files,
    )


def diff_against(repo: Path, base_branch: str = "main") -> str:
    return git(repo, "diff", f"{base_branch}...HEAD")


def abandon_branch(repo: Path, branch: str, base_branch: str = "main") -> None:
    """Delete a branch the reviewer rejected."""
    if branch == base_branch:
        raise GitOpsError(f"refusing to delete {base_branch}")
    git(repo, "checkout", "-q", base_branch)
    git(repo, "branch", "-q", "-D", branch)
