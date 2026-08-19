"""Tests for configuration loading and safety rail parsing."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sev0.config import Settings, anthropic_api_key, load_settings


def test_protected_paths_are_split_and_stripped() -> None:
    settings = Settings(protected_paths="migrations/, infra/ ,.github/")
    assert settings.protected_path_list == ["migrations/", "infra/", ".github/"]


def test_human_approval_defaults_to_required() -> None:
    assert Settings().require_human_approval is True


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # load_settings() loads .env without overriding, which is the right
    # precedence for a real shell but makes these tests depend on whatever the
    # developer happens to have exported -- or on an earlier test in the same
    # session having loaded a .env of its own. Everything sev0 reads comes out
    # first so the fixture file under test is the only source.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    for name in [key for key in os.environ if key.startswith("SEV0_")]:
        monkeypatch.delenv(name, raising=False)


def test_unprefixed_credentials_reach_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
) -> None:
    # Settings only reads SEV0_-prefixed keys. Without an explicit dotenv load
    # the provider credential is parsed out of .env and thrown away, and the
    # run dies inside the SDK on its first request.
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-test-1234\nSEV0_MODEL=x\n")
    monkeypatch.chdir(tmp_path)

    settings = load_settings()

    assert settings.model == "x"
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-test-1234"


def test_the_shipped_placeholder_does_not_count_as_a_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-...")
    assert anthropic_api_key() is None


def test_a_real_key_is_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "  sk-ant-api03-real  ")
    assert anthropic_api_key() == "sk-ant-api03-real"


def test_a_missing_key_is_none(clean_env: None) -> None:
    assert anthropic_api_key() is None
