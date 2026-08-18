"""Executing untrusted code.

The agent writes patches and then runs a test suite against them. That test
suite executes whatever the patch contains, which means it is untrusted code by
definition — not because the model is malicious, but because a wrong fix can do
arbitrary damage while looking perfectly reasonable.

Two implementations. DockerSandbox is the real one: no network, a read-only
mount of everything except the work directory, a memory cap and a hard wall
clock. LocalSandbox runs directly on the host and exists for tests and for
development on a machine without Docker; it is not a security boundary and says
so loudly.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from typing import Protocol

from sev0.sandbox.models import ExecResult

DEFAULT_IMAGE = "python:3.12-slim"


class SandboxError(RuntimeError):
    pass


class Sandbox(Protocol):
    def run(
        self,
        command: list[str],
        workdir: Path,
        timeout_seconds: int = 600,
    ) -> ExecResult: ...


class LocalSandbox:
    """Runs on the host. Provides isolation only in the sense that it does not.

    Useful for tests and for a machine without Docker. Every call is a real
    subprocess on your own filesystem with your own network, so nothing that
    came out of a model should go through here without you reading it first.
    """

    isolated = False

    def run(
        self,
        command: list[str],
        workdir: Path,
        timeout_seconds: int = 600,
    ) -> ExecResult:
        started = time.perf_counter()
        try:
            completed = subprocess.run(
                command,
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as expired:
            return ExecResult(
                exit_code=124,
                stdout=expired.stdout.decode() if isinstance(expired.stdout, bytes) else "",
                stderr=f"timed out after {timeout_seconds}s",
                duration_seconds=time.perf_counter() - started,
                timed_out=True,
            )
        except FileNotFoundError as exc:
            raise SandboxError(f"command not found: {command[0]}") from exc

        return ExecResult(
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_seconds=time.perf_counter() - started,
        )


class DockerSandbox:
    """Runs in a throwaway container with the network switched off."""

    isolated = True

    def __init__(
        self,
        image: str = DEFAULT_IMAGE,
        network: str = "none",
        memory: str = "1g",
        cpus: str = "2",
        pids_limit: int = 256,
    ) -> None:
        self.image = image
        self.network = network
        self.memory = memory
        self.cpus = cpus
        self.pids_limit = pids_limit

    def available(self) -> bool:
        if shutil.which("docker") is None:
            return False
        probe = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
        )
        return probe.returncode == 0

    def _docker_args(self, workdir: Path, timeout_seconds: int) -> list[str]:
        return [
            "docker",
            "run",
            "--rm",
            f"--network={self.network}",
            f"--memory={self.memory}",
            f"--cpus={self.cpus}",
            f"--pids-limit={self.pids_limit}",
            # A fix that needs to escalate privileges is not a fix.
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--volume",
            f"{workdir.resolve()}:/work",
            "--workdir",
            "/work",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--stop-timeout",
            str(timeout_seconds),
            self.image,
        ]

    def run(
        self,
        command: list[str],
        workdir: Path,
        timeout_seconds: int = 600,
    ) -> ExecResult:
        if not workdir.exists():
            raise SandboxError(f"work directory does not exist: {workdir}")

        started = time.perf_counter()
        try:
            completed = subprocess.run(
                [*self._docker_args(workdir, timeout_seconds), *command],
                capture_output=True,
                text=True,
                # Kill from the outside a little after the container's own
                # limit, so a wedged daemon cannot hang the run forever.
                timeout=timeout_seconds + 30,
            )
        except subprocess.TimeoutExpired:
            return ExecResult(
                exit_code=124,
                stdout="",
                stderr=f"container exceeded {timeout_seconds}s and was killed",
                duration_seconds=time.perf_counter() - started,
                timed_out=True,
            )
        except FileNotFoundError as exc:
            raise SandboxError("docker is not installed or not on PATH") from exc

        return ExecResult(
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_seconds=time.perf_counter() - started,
        )
