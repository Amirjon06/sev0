"""Runtime configuration, loaded from environment variables or a .env file.

Every knob the agent respects lives here so that safety limits are declared in
one auditable place rather than scattered through the call sites.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration for a single sev0 run."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="SEV0_",
        extra="ignore",
    )

    # Model provider
    model: str = "claude-sonnet-4-6"

    # Git hosting
    repo: str | None = None
    base_branch: str = "main"

    # Observability sources
    loki_url: str | None = None
    prometheus_url: str | None = None
    tempo_url: str | None = None

    # Sandbox
    sandbox_runtime: str = "docker"
    sandbox_timeout_seconds: int = 600
    sandbox_network: str = "none"

    # Safety rails
    max_files_changed: int = Field(default=5, ge=1)
    max_lines_changed: int = Field(default=120, ge=1)
    max_tool_calls: int = Field(default=60, ge=1)
    require_human_approval: bool = True
    protected_paths: str = "migrations/,infra/,.github/"

    # Output
    run_dir: Path = Path("./runs")
    log_level: str = "INFO"

    @property
    def protected_path_list(self) -> list[str]:
        """Paths the agent is forbidden from modifying."""
        return [p.strip() for p in self.protected_paths.split(",") if p.strip()]


def load_settings() -> Settings:
    """Load settings from the environment."""
    return Settings()
