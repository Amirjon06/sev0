"""sev0-lab: bring the storefront up, break it, put it back."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from incident_lab import benchmark as bench
from incident_lab import target as target_repo
from incident_lab.scenarios import model
from incident_lab.scoring import Score, Scorecard, score_run
from sev0.agent.state import RunState
from sev0.config import github_token, load_settings
from sev0.git_ops import repository as repo_ops

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = REPO_ROOT / "incident_lab" / "app"
SOURCE_DIR = APP_DIR
RUN_DIR = REPO_ROOT / "runs"
TARGET_DIR = RUN_DIR / "target"
STATE_FILE = RUN_DIR / "lab-state.json"
LEDGER_FILE = RUN_DIR / "injections.jsonl"
DEFAULT_SCORECARD = RUN_DIR / "scorecard.md"
BENCHMARK_DIR = RUN_DIR / "benchmarks"

app = typer.Typer(
    name="sev0-lab",
    help="Incident Lab: run the storefront, inject hidden faults, score the agent.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


def compose(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["docker", "compose", *args],
        cwd=APP_DIR,
        text=True,
        capture_output=True,
    )
    if check and result.returncode != 0:
        console.print(f"[red]docker compose {' '.join(args)} failed[/red]")
        console.print(result.stderr.strip())
        raise typer.Exit(code=result.returncode)
    return result


def read_state() -> dict[str, object]:
    if not STATE_FILE.exists():
        return {}
    state: dict[str, object] = json.loads(STATE_FILE.read_text())
    return state


def write_state(state: dict[str, object]) -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


def clear_state() -> None:
    STATE_FILE.unlink(missing_ok=True)


def append_ledger(entry: dict[str, object]) -> None:
    """Record an injection or a restore, permanently.

    lab-state.json is deleted on restore, so it says what is broken now and
    nothing about what was broken an hour ago. Scoring a run needs the second
    thing: which fault was live when that run started.
    """
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    with LEDGER_FILE.open("a") as handle:
        handle.write(json.dumps(entry) + "\n")


def read_ledger() -> list[dict[str, object]]:
    if not LEDGER_FILE.exists():
        return []
    entries = []
    for line in LEDGER_FILE.read_text().splitlines():
        if line.strip():
            entries.append(json.loads(line))
    return entries


def scenario_live_at(moment: str, ledger: list[dict[str, object]]) -> str | None:
    """Which scenario was injected at the given instant, if any.

    The ledger is append-only and chronological, so the last entry at or before
    the moment describes the state the run was looking at. A restore entry
    carries no scenario, which is how a run against a healthy system reads as
    unscoreable rather than as a wrong answer.
    """
    live: str | None = None
    for entry in ledger:
        at = str(entry.get("at", ""))
        if not at or at > moment:
            break
        scenario = entry.get("scenario")
        live = str(scenario) if scenario else None
    return live


def commit_change(repo: Path, change: model.Change) -> None:
    """Commit the working tree as the change's author.

    Authorship is per-commit rather than global so that `git blame` and
    `git log --author` behave the way they would on a real project, which is
    part of what makes history a usable source of evidence at all.
    """
    name, _, email = change.author.partition(" <")
    target_repo.git(repo, "add", "-A")
    target_repo.git(
        repo,
        "-c",
        f"user.name={name.strip()}",
        "-c",
        f"user.email={email.rstrip('>').strip()}",
        "commit",
        "-q",
        "-m",
        change.message,
    )


def ensure_target() -> Path:
    if not target_repo.exists(TARGET_DIR):
        console.print("Materialising target repository ...")
        target_repo.materialize(SOURCE_DIR, TARGET_DIR)
    return TARGET_DIR


@app.command()
def up(
    rebuild: bool = typer.Option(True, "--build/--no-build", help="Rebuild service images."),
    fresh: bool = typer.Option(
        False, "--fresh", help="Discard and recreate the target repository first."
    ),
) -> None:
    """Start the storefront and its observability stack."""
    if fresh:
        console.print("Recreating target repository ...")
        target_repo.materialize(SOURCE_DIR, TARGET_DIR, force=True)
        clear_state()
    ensure_target()
    args = ["up", "-d"]
    if rebuild:
        args.append("--build")
    compose(*args)
    console.print("[green]Lab is up.[/green] Grafana: http://localhost:3000")


@app.command()
def down(
    volumes: bool = typer.Option(False, "--volumes", help="Also delete the database volume."),
) -> None:
    """Stop the lab."""
    compose("down", *(["-v"] if volumes else []))
    console.print("Lab stopped.")


@app.command(name="list")
def list_scenarios() -> None:
    """List the available fault scenarios."""
    scenarios = model.load_all()
    if not scenarios:
        console.print("[yellow]No scenarios defined.[/yellow]")
        return

    table = Table(title="Scenarios")
    table.add_column("ID", style="bold")
    table.add_column("Family")
    table.add_column("Alert")
    table.add_column("Title")

    for scenario in scenarios.values():
        table.add_row(scenario.id, scenario.family, scenario.alert, scenario.title)

    console.print(table)


@app.command()
def status() -> None:
    """Show what is running and whether a fault is currently injected."""
    state = read_state()

    table = Table(show_header=False)
    table.add_column("", style="bold")
    table.add_column("")

    if not target_repo.exists(TARGET_DIR):
        table.add_row("Target repo", "[yellow]not materialised[/yellow]")
    else:
        head = target_repo.head(TARGET_DIR)
        healthy = target_repo.at_baseline(TARGET_DIR)
        table.add_row("Target repo", f"{head.sha}  {head.subject}")
        table.add_row("Tree", "[green]baseline[/green]" if healthy else "[red]faulted[/red]")

    if state:
        table.add_row("Injected", f"[red]{state.get('scenario')}[/red]")
        table.add_row("Since", str(state.get("injected_at")))
    else:
        table.add_row("Injected", "[green]nothing[/green]")

    console.print(table)

    running = compose("ps", "--format", "{{.Service}}\t{{.State}}", check=False)
    if running.returncode == 0 and running.stdout.strip():
        console.print("\n[bold]Containers[/bold]")
        console.print(running.stdout.strip())


@app.command()
def inject(
    scenario_id: str = typer.Option(..., "--scenario", "-s", help="Scenario id to inject."),
    rebuild: bool = typer.Option(True, "--rebuild/--no-rebuild", help="Rebuild affected services."),
) -> None:
    """Break the storefront, without saying how."""
    scenario = model.load(scenario_id)
    repo = ensure_target()

    if read_state():
        console.print("[red]A fault is already injected.[/red] Run `sev0-lab restore` first.")
        raise typer.Exit(code=1)

    if target_repo.is_dirty(repo):
        console.print("[red]Target repository has uncommitted changes.[/red]")
        raise typer.Exit(code=1)

    # Each change lands as its own commit, in order. A scenario with a decoy
    # needs the decoy to sit on top of the real fault in the log, which is only
    # true if they are separate commits.
    faulting_sha = ""
    for index, change in enumerate(scenario.changes):
        for edit in change.edits:
            edit.apply(repo)
        commit_change(repo, change)
        if index == 0:
            faulting_sha = target_repo.head(repo).sha

    head = target_repo.head(repo)
    now = datetime.now(UTC).isoformat(timespec="seconds")
    write_state({"scenario": scenario.id, "commit": faulting_sha, "injected_at": now})
    append_ledger({"at": now, "scenario": scenario.id, "commit": faulting_sha})

    if rebuild and scenario.rebuild:
        console.print(f"Rebuilding {', '.join(scenario.rebuild)} ...")
        compose("up", "-d", "--build", *scenario.rebuild)

    console.print(f"[red]Injected[/red] {scenario.id} as {head.sha}")
    console.print(f"Alert: [bold]{scenario.alert}[/bold]")
    console.print("Ground truth is recorded but not shown. Use `sev0-lab reveal` to see it.")


@app.command()
def publish(
    repository: str = typer.Option(
        "", "--repo", "-R", help="Destination as owner/name. Defaults to SEV0_REPO."
    ),
) -> None:
    """Push the target repository to GitHub so pull requests have somewhere to go.

    The target is scratch: `up --fresh` rebuilds it and its history is
    synthetic. Publishing it is what lets a verified fix become a branch a
    reviewer can actually open, and it force-pushes for the same reason --
    the local tree is the truth and the remote is a mirror of it.
    """
    settings = load_settings()
    destination = repository or settings.repo or ""
    token = github_token()

    if not destination or "/" not in destination:
        console.print("[red]No destination.[/red] Set SEV0_REPO or pass --repo owner/name.")
        raise typer.Exit(code=1)
    if token is None:
        console.print("[red]GITHUB_TOKEN is not set.[/red]")
        raise typer.Exit(code=1)

    repo = ensure_target()
    url = repo_ops.remote_url(destination, token)
    branch = target_repo.git(repo, "rev-parse", "--abbrev-ref", "HEAD")

    result = subprocess.run(
        ["git", "push", "--force", url, f"{branch}:{settings.base_branch}"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # The URL carries a token, so git's own message is not safe to print.
        console.print(f"[red]Push failed.[/red] {result.stderr.replace(url, '<remote>').strip()}")
        raise typer.Exit(code=1)

    console.print(f"Published {branch} to [bold]{destination}[/bold] as {settings.base_branch}.")


@app.command()
def restore(
    rebuild: bool = typer.Option(True, "--rebuild/--no-rebuild", help="Rebuild affected services."),
) -> None:
    """Undo whatever is injected and return the storefront to health."""
    repo = ensure_target()
    state = read_state()

    target_repo.reset_to_baseline(repo)
    clear_state()
    if state.get("scenario"):
        append_ledger({"at": datetime.now(UTC).isoformat(timespec="seconds"), "scenario": None})

    services: tuple[str, ...] = ()
    if state.get("scenario"):
        try:
            services = model.load(str(state["scenario"])).rebuild
        except model.ScenarioError:
            services = ()

    if rebuild:
        console.print("Rebuilding ...")
        compose("up", "-d", "--build", *services)

    console.print("[green]Restored to baseline.[/green]")


def _load_scores(run_dir: Path, scenario_id: str | None) -> list[Score]:
    scenarios = model.load_all()
    scores: list[Score] = []

    for trace in sorted(run_dir.glob("*/run.json")):
        state = RunState.load(trace)
        chosen = scenario_id or _scenario_for(state, scenarios)
        if chosen is None or chosen not in scenarios:
            continue
        scores.append(score_run(state, scenarios[chosen]))

    return scores


def _scenario_for(state: RunState, scenarios: dict[str, model.Scenario]) -> str | None:
    """Match a run to the fault that was live when it started.

    Matching by alert was wrong, and wrong in the way the benchmark was built
    to expose: both scenarios deliberately fire checkout-5xx, so the lookup
    always returned whichever one was defined first and every second-scenario
    run scored against the wrong answer key. The ledger knows what was
    actually injected; the alert never did.
    """
    if state.started_at:
        live = scenario_live_at(state.started_at, read_ledger())
        if live is not None:
            return live if live in scenarios else None

    # Runs recorded before the ledger existed, and runs named after a scenario
    # directly. Deliberately not falling back to the alert.
    if state.incident in scenarios:
        return state.incident
    return None


@app.command()
def benchmark(
    scenario_ids: str = typer.Option(
        "", "--scenario", "-s", help="Comma-separated scenario ids. Default: all."
    ),
    family: str = typer.Option("", "--family", help="Restrict to one fault family."),
    modes: str = typer.Option("full", "--mode", help="Comma-separated evaluation modes."),
    runs: int = typer.Option(1, "--runs", "-n", min=1, help="Trials per scenario per mode."),
    settle: int = typer.Option(
        bench.SETTLE_SECONDS_DEFAULT,
        "--settle",
        help="Seconds to let telemetry establish after injecting.",
    ),
    output: str = typer.Option("", "--output", "-o", help="Where to write the results JSON."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the plan and its cost exposure, then stop."
    ),
) -> None:
    """Evaluate sev0 across the scenario suite, reproducibly.

    Each trial restores the storefront before it injects and again after,
    so a scenario that fails to clean up cannot silently become part of the
    next one's measurement.
    """
    settings = load_settings()
    selected = _select_scenarios(scenario_ids, family)
    mode_list = tuple(m.strip() for m in modes.split(",") if m.strip())
    total = len(selected) * len(mode_list) * runs

    console.print(
        f"[bold]{len(selected)}[/bold] scenarios x [bold]{len(mode_list)}[/bold] modes "
        f"x [bold]{runs}[/bold] runs = [bold]{total}[/bold] trials, "
        f"model {settings.model}"
    )

    if dry_run:
        table = Table(title="Benchmark plan")
        table.add_column("Scenario", style="bold")
        table.add_column("Family")
        table.add_column("Alert")
        table.add_column("Modes")
        for scenario in selected:
            table.add_row(scenario.id, scenario.family, scenario.alert, ", ".join(mode_list))
        console.print(table)
        console.print(
            "\n[yellow]Dry run.[/yellow] Nothing was injected and no model was called."
        )
        return

    def prepare(scenario: model.Scenario) -> None:
        _restore_quietly()
        _inject_quietly(scenario)
        if settle:
            console.print(f"  settling {settle}s ...")
            time.sleep(settle)

    def cleanup(_: model.Scenario) -> None:
        _restore_quietly()

    def investigate(scenario: model.Scenario, run_mode: str, trial: int) -> Score:
        console.print(f"  {scenario.id} [{run_mode} #{trial}] ...")
        trace = _run_investigation(scenario, run_mode, trial, settings)
        state = RunState.load(trace)
        return score_run(state, scenario)

    result = bench.execute(
        scenarios=selected,
        modes=mode_list,
        runs_per_scenario=runs,
        model=settings.model,
        sev0_commit=bench.sev0_revision(REPO_ROOT),
        investigate=investigate,
        prepare=prepare,
        cleanup=cleanup,
        on_event=lambda message: console.print(f"  [yellow]{message}[/yellow]"),
    )

    stamp = result.started_at.replace(":", "").replace("-", "")
    destination = Path(output) if output else BENCHMARK_DIR / f"{stamp}.json"
    result.save(destination)

    report_path = destination.with_suffix(".md")
    report_path.write_text(bench.report(result))

    console.print(bench.report(result))
    console.print(f"Results written to {destination}")
    console.print(f"Report written to {report_path}")


def _select_scenarios(scenario_ids: str, family: str) -> list[model.Scenario]:
    scenarios = model.load_all()
    wanted = [s.strip() for s in scenario_ids.split(",") if s.strip()]

    if wanted:
        unknown = [s for s in wanted if s not in scenarios]
        if unknown:
            console.print(f"[red]Unknown scenarios:[/red] {', '.join(unknown)}")
            raise typer.Exit(code=1)
        selected = [scenarios[s] for s in wanted]
    else:
        selected = list(scenarios.values())

    if family:
        selected = [s for s in selected if s.family == family]
        if not selected:
            console.print(f"[red]No scenarios in family {family!r}.[/red]")
            raise typer.Exit(code=1)

    return selected


def _restore_quietly() -> None:
    repo = ensure_target()
    state = read_state()
    target_repo.reset_to_baseline(repo)
    clear_state()
    if state.get("scenario"):
        append_ledger(
            {"at": datetime.now(UTC).isoformat(timespec="seconds"), "scenario": None}
        )
        try:
            services = model.load(str(state["scenario"])).rebuild
        except model.ScenarioError:
            services = ()
        compose("up", "-d", "--build", *services)


def _inject_quietly(scenario: model.Scenario) -> None:
    repo = ensure_target()
    for change in scenario.changes:
        for edit in change.edits:
            edit.apply(repo)
        commit_change(repo, change)

    head = target_repo.head(repo)
    now = datetime.now(UTC).isoformat(timespec="seconds")
    write_state({"scenario": scenario.id, "commit": head.sha, "injected_at": now})
    append_ledger({"at": now, "scenario": scenario.id, "commit": head.sha})

    if scenario.rebuild:
        compose("up", "-d", "--build", *scenario.rebuild)


def _run_investigation(
    scenario: model.Scenario, run_mode: str, trial: int, settings: object
) -> Path:
    """Shell out to `sev0 investigate` so the benchmark exercises the real path.

    Calling the library directly would let the benchmark drift away from
    what a person running the command actually gets, which is the one thing
    a harness must not do.
    """
    before = {path.name for path in RUN_DIR.glob("*") if path.is_dir()}

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "sev0.cli",
            "investigate",
            "--incident",
            scenario.alert,
            "--mode",
            run_mode,
            "--scenario",
            scenario.id,
            "--trial",
            str(trial),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )

    after = {path.name for path in RUN_DIR.glob("*") if path.is_dir()}
    fresh = sorted(after - before)
    if not fresh:
        raise RuntimeError(
            f"investigate produced no run: {result.stderr.strip()[-400:] or 'no output'}"
        )

    return RUN_DIR / fresh[-1] / "run.json"


@app.command()
def score(
    run: str = typer.Option(..., "--run", "-r", help="Run id under runs/."),
    scenario_id: str = typer.Option(
        "", "--scenario", "-s", help="Scenario to score against. Inferred from the alert if unset."
    ),
) -> None:
    """Score one run against ground truth."""
    trace = RUN_DIR / run / "run.json"
    if not trace.exists():
        console.print(f"[red]No trace at {trace}[/red]")
        raise typer.Exit(code=1)

    state = RunState.load(trace)
    scenarios = model.load_all()
    chosen = scenario_id or _scenario_for(state, scenarios)

    if chosen is None or chosen not in scenarios:
        console.print(
            f"[red]Could not tell which scenario {run} belongs to.[/red] Pass --scenario."
        )
        raise typer.Exit(code=1)

    console.print(score_run(state, scenarios[chosen]).render())


@app.command()
def report(
    scenario_id: str = typer.Option("", "--scenario", "-s", help="Restrict to one scenario."),
    output: str = typer.Option("", "--output", "-o", help="Where to write the scorecard."),
) -> None:
    """Aggregate every run under runs/ into a scorecard."""
    destination = Path(output) if output else DEFAULT_SCORECARD
    scores = _load_scores(RUN_DIR, scenario_id or None)
    if not scores:
        console.print("[yellow]No scoreable runs found under runs/.[/yellow]")
        raise typer.Exit(code=1)

    card = Scorecard(scores=tuple(scores))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(card.to_markdown())

    table = Table(title=f"Scorecard over {card.runs} runs")
    table.add_column("Metric", style="bold")
    table.add_column("Value")
    table.add_row("Root-cause accuracy", f"{card.accuracy:.0%}")
    table.add_row("Verified resolution rate", f"{card.resolution_rate:.0%}")
    median = card.median_seconds
    table.add_row("Median time to diagnosis", f"{median:.0f}s" if median else "n/a")
    table.add_row("Unsafe attempts", str(card.unsafe_attempts))
    table.add_row("Runs that executed nothing", str(card.silent_runs))

    console.print(table)
    console.print(f"\nWritten to {destination}")


@app.command()
def reveal(
    scenario_id: str = typer.Option(..., "--scenario", "-s", help="Scenario id."),
) -> None:
    """Print the answer key. Never call this from anything the agent can read."""
    scenario = model.load(scenario_id)
    truth = scenario.ground_truth

    console.print(f"[bold]{scenario.id}[/bold] — {scenario.title}\n")
    console.print(f"Service : {truth.service}")
    console.print(f"File    : {truth.file}")
    console.print(f"Symbol  : {truth.symbol}")
    console.print(f"\n{truth.summary.strip()}")


if __name__ == "__main__":
    app()
