"""Command line interface for sev0."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from sev0 import __version__
from sev0.agent.loop import InvestigationLoop, build_brief
from sev0.agent.state import RunState
from sev0.agent.tools import Toolbox
from sev0.collectors.logs import LokiCollector
from sev0.collectors.metrics import PrometheusCollector
from sev0.config import Settings, load_settings

app = typer.Typer(
    name="sev0",
    help="An autonomous AI software engineer that diagnoses, repairs, and proves.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


@app.command()
def version() -> None:
    """Print the installed sev0 version."""
    console.print(f"sev0 [bold cyan]{__version__}[/bold cyan]")


@app.command()
def doctor() -> None:
    """Check that the local environment is ready to run an investigation."""
    settings = load_settings()

    table = Table(title="sev0 environment check", show_lines=False)
    table.add_column("Component", style="bold")
    table.add_column("Value")

    table.add_row("Model", settings.model)
    table.add_row("Target repository", str(settings.target_repo))
    table.add_row("Repository", settings.repo or "[yellow]not configured[/yellow]")
    table.add_row("Loki", settings.loki_url or "[yellow]not configured[/yellow]")
    table.add_row("Prometheus", settings.prometheus_url or "[yellow]not configured[/yellow]")
    table.add_row("Sandbox runtime", settings.sandbox_runtime)
    table.add_row("Max tool calls", str(settings.max_tool_calls))
    table.add_row("Max files changed", str(settings.max_files_changed))
    table.add_row("Human approval required", str(settings.require_human_approval))

    console.print(table)


def _build_toolbox(state: RunState, settings: Settings) -> Toolbox:
    if not settings.target_repo.exists():
        console.print(
            f"[red]No repository at {settings.target_repo}.[/red] "
            "Run `sev0-lab up` to materialise the Incident Lab target."
        )
        raise typer.Exit(code=1)

    return Toolbox(
        state=state,
        repo=settings.target_repo,
        loki=LokiCollector(settings.loki_url) if settings.loki_url else None,
        prometheus=(
            PrometheusCollector(settings.prometheus_url) if settings.prometheus_url else None
        ),
    )


def _build_client() -> object:
    try:
        import anthropic
    except ImportError:  # pragma: no cover - dependency is declared
        console.print("[red]The anthropic package is not installed.[/red]")
        raise typer.Exit(code=1) from None

    try:
        return anthropic.Anthropic()
    except Exception as exc:  # noqa: BLE001 - the message is the useful part
        console.print(f"[red]Could not create an Anthropic client:[/red] {exc}")
        console.print("Set ANTHROPIC_API_KEY in your .env file.")
        raise typer.Exit(code=1) from None


@app.command()
def investigate(
    incident: str = typer.Option(..., "--incident", "-i", help="Incident identifier or alert."),
    alert: str = typer.Option("", "--alert", help="Alert text, if you have one."),
    dry_run: bool = typer.Option(
        True, "--dry-run/--no-dry-run", help="Investigate without opening a pull request."
    ),
) -> None:
    """Investigate an incident and identify its root cause."""
    settings = load_settings()
    state = RunState(incident=incident)
    toolbox = _build_toolbox(state, settings)
    client = _build_client()

    console.print(f"Investigating [bold]{incident}[/bold] against {settings.target_repo}\n")

    loop = InvestigationLoop(
        client=client,  # type: ignore[arg-type]
        toolbox=toolbox,
        state=state,
        model=settings.model,
        max_tool_calls=settings.max_tool_calls,
    )

    try:
        loop.run(build_brief(incident, alert or None))
    except KeyboardInterrupt:
        state.abandon("interrupted")

    trace = state.save(settings.run_dir)
    console.print(state.summary())
    console.print(f"\nTrace written to {trace}")

    if not dry_run:
        console.print("[yellow]Repair and pull requests arrive in Phase 3.[/yellow]")

    raise typer.Exit(code=0 if state.root_cause else 1)


if __name__ == "__main__":
    app()
