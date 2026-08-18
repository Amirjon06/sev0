"""sev0-lab: bring the storefront up, break it, put it back."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from incident_lab import target as target_repo
from incident_lab.scenarios import model

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = REPO_ROOT / "incident_lab" / "app"
SOURCE_DIR = APP_DIR
RUN_DIR = REPO_ROOT / "runs"
TARGET_DIR = RUN_DIR / "target"
STATE_FILE = RUN_DIR / "lab-state.json"

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

    for edit in scenario.edits:
        edit.apply(repo)

    target_repo.git(repo, "add", "-A")
    target_repo.git(
        repo,
        "-c",
        f"user.name={scenario.author.split(' <')[0]}",
        "-c",
        f"user.email={scenario.author.split(' <')[1].rstrip('>')}",
        "commit",
        "-q",
        "-m",
        scenario.commit_message,
    )

    head = target_repo.head(repo)
    write_state(
        {
            "scenario": scenario.id,
            "commit": head.sha,
            "injected_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
    )

    if rebuild and scenario.rebuild:
        console.print(f"Rebuilding {', '.join(scenario.rebuild)} ...")
        compose("up", "-d", "--build", *scenario.rebuild)

    console.print(f"[red]Injected[/red] {scenario.id} as {head.sha}")
    console.print(f"Alert: [bold]{scenario.alert}[/bold]")
    console.print("Ground truth is recorded but not shown. Use `sev0-lab reveal` to see it.")


@app.command()
def restore(
    rebuild: bool = typer.Option(True, "--rebuild/--no-rebuild", help="Rebuild affected services."),
) -> None:
    """Undo whatever is injected and return the storefront to health."""
    repo = ensure_target()
    state = read_state()

    target_repo.reset_to_baseline(repo)
    clear_state()

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
