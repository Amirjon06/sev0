"""Scenario definitions: what to break, and what the truth about it is.

A scenario is deliberately split in two. Everything under `edits` and `commit`
describes the change the agent will have to find. Everything under
`ground_truth` is the answer key, and nothing that reads it may ever be exposed
to the agent.

Edits are find-and-replace rather than unified diffs. A diff carries line
numbers, so any unrelated change upstream in the file breaks it silently; an
exact-match replacement fails loudly instead, which is the behavior worth
having in a harness whose whole job is to be trustworthy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SCENARIO_DIR = Path(__file__).parent


class ScenarioError(RuntimeError):
    pass


@dataclass(frozen=True)
class Edit:
    file: str
    find: str
    replace: str

    def apply(self, root: Path) -> None:
        path = root / self.file
        if not path.exists():
            raise ScenarioError(f"edit targets a missing file: {self.file}")

        body = path.read_text()
        occurrences = body.count(self.find)
        if occurrences != 1:
            raise ScenarioError(
                f"edit anchor matched {occurrences} times in {self.file}, expected exactly 1"
            )
        path.write_text(body.replace(self.find, self.replace))


@dataclass(frozen=True)
class GroundTruth:
    """The answer key. Never show this to the agent."""

    service: str
    file: str
    symbol: str
    summary: str


@dataclass(frozen=True)
class Notes:
    """Why this scenario is solvable, and how.

    Written for whoever maintains the benchmark, not for the agent. A scenario
    nobody could solve from the available evidence is not measuring debugging
    ability, it is measuring luck, and the only way to know which one you have
    built is to write down the intended path and check it exists.

    This never leaves the sev0 repository. The target repository the agent
    investigates is built from the storefront source alone.
    """

    signal: str = ""
    path: str = ""
    reproduction: str = ""


@dataclass(frozen=True)
class Change:
    """One commit's worth of edits.

    A scenario is a sequence of these rather than a single change, because the
    interesting adversarial cases need a decoy: a later commit that looks far
    more suspicious than the one that actually broke production.
    """

    message: str
    author: str
    edits: tuple[Edit, ...]

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Change:
        return cls(
            message=raw["message"].strip(),
            author=raw["author"],
            edits=tuple(Edit(**e) for e in raw["edits"]),
        )


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    family: str
    summary: str
    alert: str
    changes: tuple[Change, ...]
    ground_truth: GroundTruth
    rebuild: tuple[str, ...] = ()
    failing_tests: tuple[str, ...] = field(default=())
    tags: tuple[str, ...] = field(default=())
    difficulty: str = "medium"
    notes: Notes = field(default_factory=Notes)

    @property
    def edits(self) -> tuple[Edit, ...]:
        """Every edit the scenario makes, across all of its commits."""
        return tuple(edit for change in self.changes for edit in change.edits)

    @property
    def commit_message(self) -> str:
        """The message of the change that actually planted the fault."""
        return self.changes[0].message if self.changes else ""

    @property
    def author(self) -> str:
        return self.changes[0].author if self.changes else ""

    @property
    def is_adversarial(self) -> bool:
        return "adversarial" in self.tags

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Scenario:
        try:
            changes = cls._changes_from(raw)
            if not changes:
                raise ScenarioError(f"scenario {raw.get('id', '?')} makes no changes")

            return cls(
                id=raw["id"],
                title=raw["title"],
                family=raw["family"],
                summary=raw["summary"].strip(),
                alert=raw["alert"],
                changes=changes,
                ground_truth=GroundTruth(**raw["ground_truth"]),
                rebuild=tuple(raw.get("rebuild", ())),
                failing_tests=tuple(raw.get("failing_tests", ())),
                tags=tuple(raw.get("tags", ())),
                difficulty=raw.get("difficulty", "medium"),
                notes=Notes(**raw.get("notes", {})),
            )
        except KeyError as exc:
            raise ScenarioError(f"scenario {raw.get('id', '?')} is missing {exc}") from exc

    @staticmethod
    def _changes_from(raw: dict[str, Any]) -> tuple[Change, ...]:
        """Accept either one commit or a sequence of them.

        The single-commit form is what most scenarios need and reads better for
        them; requiring every scenario to declare a list would be ceremony for
        the common case.
        """
        if "commits" in raw:
            return tuple(Change.from_dict(entry) for entry in raw["commits"])

        return (
            Change(
                message=raw["commit"]["message"].strip(),
                author=raw["commit"]["author"],
                edits=tuple(Edit(**e) for e in raw["edits"]),
            ),
        )


def load_all(directory: Path | None = None) -> dict[str, Scenario]:
    directory = directory or SCENARIO_DIR
    scenarios: dict[str, Scenario] = {}

    for path in sorted(directory.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text())
        scenario = Scenario.from_dict(raw)
        if scenario.id in scenarios:
            raise ScenarioError(f"duplicate scenario id: {scenario.id}")
        scenarios[scenario.id] = scenario

    return scenarios


def load(scenario_id: str, directory: Path | None = None) -> Scenario:
    scenarios = load_all(directory)
    if scenario_id not in scenarios:
        known = ", ".join(sorted(scenarios)) or "none"
        raise ScenarioError(f"unknown scenario {scenario_id!r}; known scenarios: {known}")
    return scenarios[scenario_id]
