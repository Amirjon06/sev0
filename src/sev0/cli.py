"""Command line interface for sev0."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from sev0 import __version__
from sev0.config import load_settings

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
    table.add_row("Repository", settings.repo or "[yellow]not configured[/yellow]")
    table.add_row("Loki", settings.loki_url or "[yellow]not configured[/yellow]")
    table.add_row("Prometheus", settings.prometheus_url or "[yellow]not configured[/yellow]")
    table.add_row("Sandbox runtime", settings.sandbox_runtime)
    table.add_row("Max files changed", str(settings.max_files_changed))
    table.add_row("Human approval required", str(settings.require_human_approval))

    console.print(table)


@app.command()
def investigate(
    incident: str = typer.Option(..., "--incident", "-i", help="Incident identifier or alert name."),
    dry_run: bool = typer.Option(
        True, "--dry-run/--no-dry-run", help="Investigate without opening a pull request."
    ),
) -> None:
    """Investigate an incident and propose a fix.

    Not yet implemented. See docs/ROADMAP.md for the delivery plan.
    """
    console.print(f"[bold]Investigating[/bold] {incident} (dry_run={dry_run})")
    raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
