"""Runtime configuration, loaded from environment variables or a .env file.

Every knob the agent respects lives here so that safety limits are declared in
one auditable place rather than scattered through the call sites.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration for a single sev0 run."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="SEV0_",
        extra="ignore",
    )

    # Model provider. Sonnet is the default because the investigation loop is
    # long-horizon tool use; Haiku is roughly a tenth the cost and useful for
    # shaking out prompt and tool problems before spending on a real run.
    model: str = "claude-sonnet-5"

    # Git hosting
    repo: str | None = None
    base_branch: str = "main"

    # Observability sources
    loki_url: str | None = None
    prometheus_url: str | None = None
    tempo_url: str | None = None

    # Sandbox
    sandbox_runtime: str = "docker"
    # The storefront image already carries the app dependencies and pytest,
    # so verification runs in it rather than a bare python image.
    sandbox_image: str = "sev0-lab-cart:latest"
    sandbox_timeout_seconds: int = 600
    sandbox_network: str = "none"

    # Safety rails
    max_files_changed: int = Field(default=5, ge=1)
    max_lines_changed: int = Field(default=120, ge=1)
    max_tool_calls: int = Field(default=60, ge=1)
    require_human_approval: bool = True
    protected_paths: str = "migrations/,infra/,.github/"

    # The repository under investigation. Defaults to the Incident Lab target
    # rather than this project, so a misconfigured run reads a scratch copy
    # instead of real source.
    target_repo: Path = Path("./runs/target")

    # Output
    run_dir: Path = Path("./runs")
    log_level: str = "INFO"

    @property
    def protected_path_list(self) -> list[str]:
        """Paths the agent is forbidden from modifying."""
        return [p.strip() for p in self.protected_paths.split(",") if p.strip()]


def load_settings() -> Settings:
    """Load settings from the environment.

    The .env file is also loaded into the process environment. Settings only
    reads SEV0_-prefixed keys, so provider credentials that live in the same
    file -- ANTHROPIC_API_KEY, GITHUB_TOKEN -- would otherwise be parsed and
    discarded, and the SDK reading os.environ would find nothing.
    """
    # The path is explicit. load_dotenv() with no argument searches upward from
    # the file that called it, which for an installed package is site-packages
    # rather than the project the user is standing in.
    load_dotenv(dotenv_path=".env", override=False)
    return Settings()


def github_token() -> str | None:
    """The git hosting credential, or None if it was never set."""
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token or token.startswith("ghp_...") or token == "ghp_":
        return None
    return token


def anthropic_api_key() -> str | None:
    """The provider credential, or None if it was never set."""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    # The shipped .env.example carries a placeholder. Treating it as a real
    # key means the failure arrives as a 401 several seconds into a run
    # instead of before the first call.
    if not key or key.startswith("sk-ant-...") or key == "sk-ant-":
        return None
    return key
