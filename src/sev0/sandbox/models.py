"""Shapes the sandbox and verifier return."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# pytest -q -rf prints one of these per failure, before the summary line.
FAILED_LINE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)")
SUMMARY_COUNT = re.compile(r"(\d+)\s+(passed|failed|error|errors|skipped|xfailed|xpassed)")


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    @property
    def output(self) -> str:
        return (self.stdout + "\n" + self.stderr).strip()


@dataclass(frozen=True)
class TestRun:
    passed: int
    failed: int
    errors: int
    failing_tests: tuple[str, ...]
    exit_code: int
    timed_out: bool
    output: str

    @property
    def green(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and self.failed == 0 and self.errors == 0

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.errors

    def render(self) -> str:
        if self.timed_out:
            return "tests timed out"
        parts = [f"{self.passed} passed"]
        if self.failed:
            parts.append(f"{self.failed} failed")
        if self.errors:
            parts.append(f"{self.errors} errors")
        return ", ".join(parts)


def parse_pytest(result: ExecResult) -> TestRun:
    """Read a pytest run from its own output.

    Counts come from the summary line, but the failing test ids come from the
    `-rf` short summary. Both are needed: the counts alone cannot tell you
    whether the failure you set out to fix is the one that went away.
    """
    text = result.output

    counts = {kind: int(number) for number, kind in SUMMARY_COUNT.findall(text)}
    failing = tuple(
        match.group(1)
        for line in text.splitlines()
        if (match := FAILED_LINE.match(line.strip()))
    )

    return TestRun(
        passed=counts.get("passed", 0),
        failed=counts.get("failed", 0),
        errors=counts.get("error", 0) + counts.get("errors", 0),
        failing_tests=failing,
        exit_code=result.exit_code,
        timed_out=result.timed_out,
        output=text,
    )


@dataclass(frozen=True)
class Violation:
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.rule}: {self.detail}"


@dataclass(frozen=True)
class VerificationResult:
    reproduced: bool
    before: TestRun | None
    after: TestRun | None
    fixed: tuple[str, ...] = field(default=())
    broke: tuple[str, ...] = field(default=())
    violations: tuple[Violation, ...] = field(default=())
    note: str = ""

    @property
    def verified(self) -> bool:
        """A fix is verified only if it repaired the failure and broke nothing."""
        return (
            self.reproduced
            and not self.violations
            and self.after is not None
            and self.after.green
            and not self.broke
        )

    def render(self) -> str:
        if self.violations:
            return "rejected before running: " + "; ".join(str(v) for v in self.violations)
        if not self.reproduced:
            return f"failure did not reproduce: {self.note}"

        lines = [
            f"before: {self.before.render() if self.before else 'not run'}",
            f"after:  {self.after.render() if self.after else 'not run'}",
        ]
        if self.fixed:
            lines.append(f"fixed: {', '.join(self.fixed)}")
        if self.broke:
            lines.append(f"BROKE: {', '.join(self.broke)}")
        lines.append("verified" if self.verified else "not verified")
        return "\n".join(lines)
