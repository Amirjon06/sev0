"""Smoke tests for the sev0 command line interface."""

from __future__ import annotations

from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from sev0 import __version__, cli
from sev0.cli import app

runner = CliRunner()


def test_version_command_reports_package_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_doctor_command_renders_environment_table() -> None:
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "Sandbox runtime" in result.stdout


def test_investigate_refuses_without_a_target_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["investigate", "--incident", "checkout-5xx"])

    assert result.exit_code == 1
    assert "sev0-lab up" in result.stdout


def test_no_client_is_built_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # The SDK constructs a client without a key and only fails on the first
    # request, by which point a container is up and a fault is injected. This
    # check is the difference between a one-line refusal and a traceback
    # halfway through a run.
    monkeypatch.setattr(cli, "anthropic_api_key", lambda: None)

    with pytest.raises(typer.Exit):
        cli._build_client()
