"""Running the benchmark: many scenarios, repeatedly, without cross-contamination.

Two properties matter more than anything else here.

The first is isolation. A scenario that fails to restore, or a run that dies
partway through, must not change what the next scenario measures. So every
trial restores before it injects as well as after, and a trial that raises is
recorded as a failure and stepped over rather than aborting the suite.

The second is that results are attributable. A row that says "80%" and cannot
say which model, which mode, which revision of sev0 and how many trials it came
from is not a measurement, it is a rumour. Every trial records all of it.
"""

from __future__ import annotations

import json
import statistics
import subprocess
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from incident_lab.scenarios.model import Scenario
from incident_lab.scoring import Score

SETTLE_SECONDS_DEFAULT = 180


@dataclass
class Trial:
    """One scenario, run once, in one mode."""

    scenario: str
    family: str
    mode: str
    trial: int
    run_id: str = ""
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))
    finished_at: str = ""
    error: str = ""
    score: Score | None = None

    @property
    def ok(self) -> bool:
        return not self.error and self.score is not None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "scenario": self.scenario,
            "family": self.family,
            "mode": self.mode,
            "trial": self.trial,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
        }
        payload["score"] = asdict(self.score) if self.score else None
        return payload


@dataclass
class BenchmarkRun:
    """A whole invocation: its configuration, and every trial it produced."""

    model: str
    modes: tuple[str, ...]
    runs_per_scenario: int
    sev0_commit: str
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))
    finished_at: str = ""
    trials: list[Trial] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "model": self.model,
            "modes": list(self.modes),
            "runs_per_scenario": self.runs_per_scenario,
            "sev0_commit": self.sev0_commit,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "trials": [trial.to_dict() for trial in self.trials],
        }

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2) + "\n")
        return path

    @classmethod
    def load(cls, path: Path) -> BenchmarkRun:
        raw = json.loads(path.read_text())
        run = cls(
            model=raw.get("model", ""),
            modes=tuple(raw.get("modes", ())),
            runs_per_scenario=raw.get("runs_per_scenario", 1),
            sev0_commit=raw.get("sev0_commit", ""),
            started_at=raw.get("started_at", ""),
            finished_at=raw.get("finished_at", ""),
        )
        for entry in raw.get("trials", []):
            score = entry.get("score")
            run.trials.append(
                Trial(
                    scenario=entry["scenario"],
                    family=entry.get("family", ""),
                    mode=entry.get("mode", "full"),
                    trial=entry.get("trial", 1),
                    run_id=entry.get("run_id", ""),
                    started_at=entry.get("started_at", ""),
                    finished_at=entry.get("finished_at", ""),
                    error=entry.get("error", ""),
                    score=Score(**score) if score else None,
                )
            )
        return run


def sev0_revision(repo: Path) -> str:
    """The commit the benchmark ran at, so a result can be reproduced."""
    result = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return "unknown"

    dirty = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
    )
    suffix = "-dirty" if dirty.stdout.strip() else ""
    return result.stdout.strip() + suffix


def plan(
    scenarios: Iterable[Scenario],
    modes: Iterable[str],
    runs_per_scenario: int,
) -> Iterator[tuple[Scenario, str, int]]:
    """Every (scenario, mode, trial) the invocation will attempt, in order.

    Scenario-major rather than trial-major: consecutive trials of the same
    scenario share one inject and one settle, which is most of the wall clock.
    """
    for scenario in scenarios:
        for mode in modes:
            for trial in range(1, runs_per_scenario + 1):
                yield scenario, mode, trial


def execute(
    scenarios: list[Scenario],
    modes: tuple[str, ...],
    runs_per_scenario: int,
    model: str,
    sev0_commit: str,
    investigate: Callable[[Scenario, str, int], Score],
    prepare: Callable[[Scenario], None],
    cleanup: Callable[[Scenario], None],
    on_event: Callable[[str], None] = lambda _: None,
) -> BenchmarkRun:
    """Walk the plan, isolating each trial from the next.

    The callbacks are injected rather than imported so the sequencing can be
    tested without Docker, a model, or a network. What is being tested here is
    the ordering and the failure handling, and neither of those needs any of
    that to be real.
    """
    run = BenchmarkRun(
        model=model,
        modes=modes,
        runs_per_scenario=runs_per_scenario,
        sev0_commit=sev0_commit,
    )

    for scenario in scenarios:
        prepared = False
        try:
            prepare(scenario)
            prepared = True
        except Exception as exc:  # noqa: BLE001 - one bad scenario must not end the suite
            on_event(f"{scenario.id}: could not inject: {exc}")

        for mode in modes:
            for trial_number in range(1, runs_per_scenario + 1):
                trial = Trial(
                    scenario=scenario.id,
                    family=scenario.family,
                    mode=mode,
                    trial=trial_number,
                )

                if not prepared:
                    trial.error = "scenario was not injected"
                else:
                    try:
                        trial.score = investigate(scenario, mode, trial_number)
                        trial.run_id = trial.score.run_id
                    except Exception as exc:  # noqa: BLE001 - recorded, not fatal
                        trial.error = f"{type(exc).__name__}: {exc}"
                        on_event(f"{scenario.id} [{mode} #{trial_number}]: {trial.error}")

                trial.finished_at = datetime.now(UTC).isoformat(timespec="seconds")
                run.trials.append(trial)

        # Always attempt to put the storefront back, including after a failure.
        # A scenario left injected would silently become part of the next one.
        try:
            cleanup(scenario)
        except Exception as exc:  # noqa: BLE001
            on_event(f"{scenario.id}: could not restore: {exc}")

    run.finished_at = datetime.now(UTC).isoformat(timespec="seconds")
    return run


# -- aggregation -----------------------------------------------------------


@dataclass(frozen=True)
class Summary:
    """Aggregate outcomes for one slice of the trials.

    Counts travel with every rate. A share quoted without its denominator
    invites someone to read three trials as a characterisation, and on a
    benchmark this size that is the mistake most worth designing against.
    """

    label: str
    trials: int
    attempted: int
    correct: int
    resolved: int
    unsafe_attempts: int
    silent: int
    median_seconds: float | None
    p95_seconds: float | None
    median_tool_calls: float | None
    median_experiments: float | None
    cost_usd: float | None

    @property
    def accuracy(self) -> float | None:
        return self.correct / self.attempted if self.attempted else None

    @property
    def resolution_rate(self) -> float | None:
        return self.resolved / self.attempted if self.attempted else None

    def rate(self, numerator: int) -> str:
        if not self.attempted:
            return "n/a"
        return f"{numerator}/{self.attempted} ({numerator / self.attempted:.0%})"


def summarise(trials: list[Trial], label: str) -> Summary:
    scored = [t.score for t in trials if t.score is not None]
    seconds = [s.seconds for s in scored if s.seconds is not None]
    costs = [s.cost_usd for s in scored if s.cost_usd is not None]

    return Summary(
        label=label,
        trials=len(trials),
        attempted=len(scored),
        correct=sum(1 for s in scored if s.correct),
        resolved=sum(1 for s in scored if s.resolved),
        unsafe_attempts=sum(s.unsafe_attempts for s in scored),
        silent=sum(1 for s in scored if s.experiments == 0),
        median_seconds=statistics.median(seconds) if seconds else None,
        p95_seconds=_percentile(seconds, 0.95),
        median_tool_calls=statistics.median([s.tool_calls for s in scored]) if scored else None,
        median_experiments=statistics.median([s.experiments for s in scored]) if scored else None,
        cost_usd=round(sum(costs), 4) if costs else None,
    )


def _percentile(values: list[float], fraction: float) -> float | None:
    """A p95 that refuses to exist below a sample size where it would lie.

    With eight samples the ninety-fifth percentile is the largest one, which is
    a maximum wearing a percentile's name.
    """
    if len(values) < 20:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
    return ordered[index]


def group_by(trials: list[Trial], key: Callable[[Trial], str]) -> dict[str, list[Trial]]:
    grouped: dict[str, list[Trial]] = {}
    for trial in trials:
        grouped.setdefault(key(trial), []).append(trial)
    return dict(sorted(grouped.items()))


def report(run: BenchmarkRun) -> str:
    """A human-readable report. Every rate carries its counts."""
    if not run.trials:
        return "No trials recorded."

    scenarios = {t.scenario for t in run.trials}
    overall = summarise(run.trials, "overall")
    failed = [t for t in run.trials if t.error]

    lines = [
        "# sev0 benchmark",
        "",
        f"- Model: `{run.model}`",
        f"- sev0 revision: `{run.sev0_commit}`",
        f"- Modes: {', '.join(f'`{m}`' for m in run.modes)}",
        f"- Scenarios: **{len(scenarios)}**",
        f"- Trials: **{len(run.trials)}** ({run.runs_per_scenario} per scenario per mode)",
        f"- Started: {run.started_at}",
        "",
        "## Overall",
        "",
        f"- Root-cause accuracy: **{overall.rate(overall.correct)}**",
        f"- Verified resolution rate: **{overall.rate(overall.resolved)}**",
        f"- Median time to diagnosis: **{_seconds(overall.median_seconds)}**",
        f"- p95 time to diagnosis: **{_seconds(overall.p95_seconds)}**",
        f"- Unsafe attempts: **{overall.unsafe_attempts}**",
        f"- Trials that executed nothing: **{overall.rate(overall.silent)}**",
        f"- Median tool calls: **{_number(overall.median_tool_calls)}**",
        f"- Median experiments: **{_number(overall.median_experiments)}**",
        f"- Estimated cost: **{_cost(overall.cost_usd)}**",
    ]

    if failed:
        lines += ["", f"- Trials that did not complete: **{len(failed)}**"]

    lines += _table("By mode", run.trials, lambda t: t.mode)
    lines += _table("By fault family", run.trials, lambda t: t.family)
    lines += _table("By scenario", run.trials, lambda t: t.scenario)

    if failed:
        lines += ["", "## Trials that did not complete", ""]
        lines += [f"- `{t.scenario}` [{t.mode} #{t.trial}]: {t.error}" for t in failed]

    return "\n".join([*lines, ""])


def _table(title: str, trials: list[Trial], key: Callable[[Trial], str]) -> list[str]:
    rows = [
        "",
        f"## {title}",
        "",
        "| Group | Trials | Root cause | Resolved | Median s | Unsafe | Silent |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for label, group in group_by(trials, key).items():
        s = summarise(group, label)
        rows.append(
            f"| {label} | {s.attempted}/{s.trials} | {s.rate(s.correct)} "
            f"| {s.rate(s.resolved)} | {_seconds(s.median_seconds)} "
            f"| {s.unsafe_attempts} | {s.silent} |"
        )
    return rows


def _seconds(value: float | None) -> str:
    return f"{value:.0f}s" if value is not None else "n/a"


def _number(value: float | None) -> str:
    return f"{value:.0f}" if value is not None else "n/a"


def _cost(value: float | None) -> str:
    return f"${value:.2f}" if value is not None else "not priced"
