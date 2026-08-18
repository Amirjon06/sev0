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
from sev0.git_ops import pull_request as pr
from sev0.git_ops import repository as repo_ops
from sev0.sandbox.patch import PatchBuilder, PatchLimits
from sev0.sandbox.runner import DockerSandbox, LocalSandbox, Sandbox

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
    table.add_row("Sandbox image", settings.sandbox_image)
    table.add_row("Max tool calls", str(settings.max_tool_calls))
    table.add_row("Max files changed", str(settings.max_files_changed))
    table.add_row("Human approval required", str(settings.require_human_approval))

    console.print(table)


def _build_sandbox(settings: Settings, allow_local: bool) -> Sandbox | None:
    docker = DockerSandbox(
        image=settings.sandbox_image,
        network=settings.sandbox_network,
    )
    if docker.available():
        return docker

    if not allow_local:
        console.print(
            "[yellow]Docker is unavailable, so the agent cannot run experiments.[/yellow]\n"
            "Start Docker Desktop, or pass --local-sandbox to execute on this machine."
        )
        return None

    console.print(
        "[red]Running experiments directly on this machine.[/red] "
        "Generated code will execute outside any isolation."
    )
    return LocalSandbox()


def _limits(settings: Settings) -> PatchLimits:
    return PatchLimits(
        max_files=settings.max_files_changed,
        max_lines=settings.max_lines_changed,
        protected_paths=tuple(settings.protected_path_list),
    )


def _build_toolbox(state: RunState, settings: Settings, sandbox: Sandbox | None) -> Toolbox:
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
        sandbox=sandbox,
        limits=_limits(settings),
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


def _raise_pull_request(state: RunState, settings: Settings) -> None:
    """Turn a verified fix into a branch, a commit, and a reviewable body."""
    fix = state.proposed_fix
    if fix is None or not fix.verified:
        console.print(
            "[yellow]No verified fix, so nothing is being proposed.[/yellow] "
            "sev0 does not open pull requests for changes it could not prove."
        )
        return

    patch = PatchBuilder(rationale=fix.rationale).edit(fix.path, fix.find, fix.replace).build()
    branch = repo_ops.branch_name(state.incident, state.run_id)
    service = state.root_cause.service if state.root_cause else ""

    try:
        commit = repo_ops.commit_fix(
            settings.target_repo,
            patch,
            branch=branch,
            subject=repo_ops.commit_subject(service, fix.rationale),
            body=f"Investigated by sev0 as run {state.run_id}.",
            base_branch=settings.base_branch,
            limits=_limits(settings),
        )
        diff = repo_ops.diff_against(settings.target_repo, settings.base_branch)
    except repo_ops.GitOpsError as exc:
        console.print(f"[red]Could not commit the fix:[/red] {exc}")
        return

    request = pr.build(
        state,
        branch=commit.branch,
        base=settings.base_branch,
        diff=diff,
        trace_path=str(settings.run_dir / state.run_id / "run.json"),
    )

    destination = settings.run_dir / state.run_id / "pull_request.md"
    destination.write_text(f"# {request.title}\n\n{request.body}\n")

    console.print(f"\nCommitted [bold]{commit.sha}[/bold] on {commit.branch}")
    console.print(f"Pull request body written to {destination}")

    if not settings.repo:
        console.print("SEV0_REPO is not set, so nothing was opened on GitHub.")
        return

    try:
        opened = pr.open_on_github(request, repository=settings.repo, draft=True)
    except pr.PullRequestError as exc:
        console.print(f"[yellow]Not opened on GitHub:[/yellow] {exc}")
        return

    console.print(f"Opened as a draft: {opened.url}")


@app.command()
def investigate(
    incident: str = typer.Option(..., "--incident", "-i", help="Incident identifier or alert."),
    alert: str = typer.Option("", "--alert", help="Alert text, if you have one."),
    dry_run: bool = typer.Option(
        True, "--dry-run/--no-dry-run", help="Investigate without opening a pull request."
    ),
    local_sandbox: bool = typer.Option(
        False,
        "--local-sandbox",
        help="Run experiments on this machine when Docker is unavailable. Not isolated.",
    ),
) -> None:
    """Investigate an incident and identify its root cause."""
    settings = load_settings()
    state = RunState(incident=incident)
    sandbox = _build_sandbox(settings, allow_local=local_sandbox)
    toolbox = _build_toolbox(state, settings, sandbox)
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

    if dry_run:
        if state.proposed_fix is not None and state.proposed_fix.verified:
            console.print("\n[green]A verified fix is ready.[/green] Re-run with --no-dry-run.")
    else:
        _raise_pull_request(state, settings)

    raise typer.Exit(code=0 if state.root_cause else 1)


if __name__ == "__main__":
    app()
