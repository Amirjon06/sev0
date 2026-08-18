"""The target repository: a working copy of the storefront with its own history.

Faults are committed here, never in the sev0 repository. Two reasons. Planting
bugs in your own history would push broken code to your remote, and the agent
needs a repository whose log contains a realistic haystack of commits rather
than one obviously-suspicious change on top of project scaffolding.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

BASELINE_TAG = "baseline"

SOURCE_ENTRIES = ("services", "loadgen", "tests", "requirements.txt", "Dockerfile")

# Committed in this order to build a plausible history. Each stage is a real
# diff, so `git log -p` and `git bisect` behave the way they would on a project
# that was actually written over time.
HISTORY: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "chore: Add service dependencies and container image",
        ("requirements.txt", "Dockerfile"),
    ),
    (
        "feat(common): Add structured logging and request metrics",
        ("services/__init__.py", "services/common.py"),
    ),
    (
        "feat(catalog): Serve the product listing",
        ("services/catalog",),
    ),
    (
        "feat(payments): Add charge authorization",
        ("services/payments",),
    ),
    (
        "feat(cart): Add line items, promotions, and totals",
        ("services/cart",),
    ),
    (
        "feat(gateway): Add the public checkout entry point",
        ("services/gateway",),
    ),
    (
        "test(loadgen): Drive baseline shopper traffic",
        ("loadgen",),
    ),
    (
        "test(cart): Cover promotions and order totals",
        ("tests",),
    ),
)


class TargetRepoError(RuntimeError):
    pass


@dataclass(frozen=True)
class Commit:
    sha: str
    subject: str


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise TargetRepoError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def exists(target: Path) -> bool:
    return (target / ".git").is_dir()


def materialize(source: Path, target: Path, *, force: bool = False) -> Path:
    """Create the target repository from the pristine storefront source."""
    if exists(target) and not force:
        return target

    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)

    for entry in SOURCE_ENTRIES:
        origin = source / entry
        if not origin.exists():
            raise TargetRepoError(f"missing source entry: {origin}")
        if origin.is_dir():
            shutil.copytree(origin, target / entry, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(origin, target / entry)

    _init_repo(target)
    return target


def _init_repo(target: Path) -> None:
    git(target, "init", "-q", "-b", "main")
    git(target, "config", "user.name", "Storefront Team")
    git(target, "config", "user.email", "team@storefront.example")

    staged: set[str] = set()
    for message, paths in HISTORY:
        for path in paths:
            if (target / path).exists():
                git(target, "add", path)
                staged.add(path)
        git(target, "commit", "-q", "-m", message)

    # Anything the staged history missed still needs to be in the tree.
    git(target, "add", "-A")
    if git(target, "status", "--porcelain"):
        git(target, "commit", "-q", "-m", "chore: Add remaining service files")

    git(target, "tag", "-f", BASELINE_TAG)


def reset_to_baseline(target: Path) -> None:
    """Discard every fault and return to the healthy tree."""
    if not exists(target):
        raise TargetRepoError(f"no target repository at {target}")
    git(target, "checkout", "-q", "main")
    git(target, "reset", "--hard", "-q", BASELINE_TAG)
    git(target, "clean", "-qfd")


def head(target: Path) -> Commit:
    sha = git(target, "rev-parse", "--short", "HEAD")
    subject = git(target, "log", "-1", "--pretty=%s")
    return Commit(sha=sha, subject=subject)


def is_dirty(target: Path) -> bool:
    return bool(git(target, "status", "--porcelain"))


def at_baseline(target: Path) -> bool:
    return git(target, "rev-parse", "HEAD") == git(target, "rev-parse", f"{BASELINE_TAG}^{{}}")
