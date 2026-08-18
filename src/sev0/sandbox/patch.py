"""Patches, and the limits they are checked against before anything runs.

The limits are not advice to the model. They are enforced here, outside the
loop, and a patch that breaks one is rejected before a single line is written
to disk. A model that decides a migration really does need editing cannot talk
its way past this file.

Patches are expressed as exact-match replacements rather than unified diffs, for
the same reason the fault scenarios are: a diff carries line numbers, and line
numbers rot. An anchor that no longer matches fails loudly.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path

from sev0.sandbox.models import Violation


class PatchError(RuntimeError):
    pass


@dataclass(frozen=True)
class FileEdit:
    path: str
    find: str
    replace: str

    @property
    def changed_lines(self) -> int:
        return len(self.find.splitlines()) + len(self.replace.splitlines())


@dataclass(frozen=True)
class Patch:
    edits: tuple[FileEdit, ...]
    rationale: str = ""

    @property
    def files(self) -> tuple[str, ...]:
        seen: list[str] = []
        for edit in self.edits:
            if edit.path not in seen:
                seen.append(edit.path)
        return tuple(seen)

    @property
    def changed_lines(self) -> int:
        return sum(edit.changed_lines for edit in self.edits)


@dataclass(frozen=True)
class PatchLimits:
    max_files: int = 5
    max_lines: int = 120
    protected_paths: tuple[str, ...] = ("migrations/", "infra/", ".github/")


def _escapes_root(path: str) -> bool:
    if Path(path).is_absolute():
        return True
    return ".." in Path(path).parts


def validate(patch: Patch, root: Path, limits: PatchLimits) -> tuple[Violation, ...]:
    """Every reason this patch must not be applied. Empty means it may be."""
    violations: list[Violation] = []

    if not patch.edits:
        violations.append(Violation("empty", "the patch changes nothing"))

    if len(patch.files) > limits.max_files:
        violations.append(
            Violation(
                "too many files",
                f"{len(patch.files)} files changed, limit is {limits.max_files}",
            )
        )

    if patch.changed_lines > limits.max_lines:
        violations.append(
            Violation(
                "too large",
                f"{patch.changed_lines} lines changed, limit is {limits.max_lines}",
            )
        )

    for path in patch.files:
        if _escapes_root(path):
            violations.append(Violation("outside repository", path))
            continue

        for protected in limits.protected_paths:
            if path.startswith(protected):
                violations.append(Violation("protected path", f"{path} matches {protected}"))

        target = root / path
        if not target.exists():
            violations.append(Violation("missing file", path))

    for edit in patch.edits:
        target = root / edit.path
        if _escapes_root(edit.path) or not target.exists():
            continue

        occurrences = target.read_text().count(edit.find)
        if occurrences == 0:
            violations.append(
                Violation("anchor not found", f"{edit.path}: the text to replace is not present")
            )
        elif occurrences > 1:
            violations.append(
                Violation(
                    "ambiguous anchor",
                    f"{edit.path}: matches {occurrences} times, so the edit is not specific",
                )
            )

    return tuple(violations)


def apply(patch: Patch, root: Path, limits: PatchLimits | None = None) -> dict[str, str]:
    """Apply a patch, returning the previous contents of each file touched.

    Validation runs again here even when the caller has already validated.
    Between a check and a write the tree can move, and the whole point of this
    module is that the limits hold regardless of who called what in which order.
    """
    limits = limits or PatchLimits()

    violations = validate(patch, root, limits)
    if violations:
        raise PatchError("; ".join(str(v) for v in violations))

    previous: dict[str, str] = {}
    for edit in patch.edits:
        target = root / edit.path
        body = target.read_text()
        previous.setdefault(edit.path, body)
        target.write_text(body.replace(edit.find, edit.replace, 1))

    return previous


def revert(previous: dict[str, str], root: Path) -> None:
    for path, body in previous.items():
        (root / path).write_text(body)


def unified_diff(previous: dict[str, str], root: Path) -> str:
    """A readable diff of what a patch did, for the pull request body."""
    chunks: list[str] = []

    for path, before in sorted(previous.items()):
        after = (root / path).read_text()
        chunk = difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
        chunks.append("".join(chunk))

    return "".join(chunks)


@dataclass
class PatchBuilder:
    """Collects edits, so a caller can build a patch without tuple gymnastics."""

    edits: list[FileEdit] = field(default_factory=list)
    rationale: str = ""

    def edit(self, path: str, find: str, replace: str) -> PatchBuilder:
        self.edits.append(FileEdit(path=path, find=find, replace=replace))
        return self

    def build(self) -> Patch:
        return Patch(edits=tuple(self.edits), rationale=self.rationale)
