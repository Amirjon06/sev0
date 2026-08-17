"""Tests for configuration loading and safety rail parsing."""

from __future__ import annotations

from sev0.config import Settings


def test_protected_paths_are_split_and_stripped() -> None:
    settings = Settings(protected_paths="migrations/, infra/ ,.github/")
    assert settings.protected_path_list == ["migrations/", "infra/", ".github/"]


def test_human_approval_defaults_to_required() -> None:
    assert Settings().require_human_approval is True
