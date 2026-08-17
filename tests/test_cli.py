"""Smoke tests for the sev0 command line interface."""

from __future__ import annotations

from typer.testing import CliRunner

from sev0 import __version__
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


def test_investigate_is_not_yet_implemented() -> None:
    result = runner.invoke(app, ["investigate", "--incident", "checkout-5xx"])
    assert result.exit_code == 1
