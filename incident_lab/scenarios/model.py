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
class Scenario:
    id: str
    title: str
    family: str
    summary: str
    alert: str
    commit_message: str
    author: str
    edits: tuple[Edit, ...]
    ground_truth: GroundTruth
    rebuild: tuple[str, ...] = ()
    failing_tests: tuple[str, ...] = field(default=())

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Scenario:
        try:
            return cls(
                id=raw["id"],
                title=raw["title"],
                family=raw["family"],
                summary=raw["summary"].strip(),
                alert=raw["alert"],
                commit_message=raw["commit"]["message"].strip(),
                author=raw["commit"]["author"],
                edits=tuple(Edit(**e) for e in raw["edits"]),
                ground_truth=GroundTruth(**raw["ground_truth"]),
                rebuild=tuple(raw.get("rebuild", ())),
                failing_tests=tuple(raw.get("failing_tests", ())),
            )
        except KeyError as exc:
            raise ScenarioError(f"scenario {raw.get('id', '?')} is missing {exc}") from exc


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
