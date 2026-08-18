"""Git history collection over the repository under investigation.

Deliberately read-only. Nothing here mutates a tree, checks anything out, or
creates a branch, so an investigation can never damage the thing it is trying
to understand. Writing is the sandbox's job, later and behind limits.
"""

from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path

from sev0.collectors.models import CommitInfo

FIELD = "\x1f"
RECORD = "\x1e"
# RECORD leads rather than trails, because --name-only writes the file list
# after the whole format string. A trailing separator would push every commit's
# files into the next commit's record.
#
# The trailing FIELD matters too: a commit body legitimately contains blank
# lines, slashes and full stops, so an explicit delimiter is the only reliable
# place to cut the body from the file list.
FORMAT = RECORD + FIELD.join(["%H", "%an", "%ae", "%aI", "%s", "%b"]) + FIELD


class GitError(RuntimeError):
    pass


class GitHistoryCollector:
    def __init__(self, repo: Path) -> None:
        self.repo = Path(repo)
        if not (self.repo / ".git").is_dir():
            raise GitError(f"not a git repository: {self.repo}")

    def _run(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=self.repo,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
        return result.stdout

    def commits_in_window(
        self, start: datetime, end: datetime, limit: int = 50
    ) -> list[CommitInfo]:
        return self._log(
            f"--since={start.isoformat()}",
            f"--until={end.isoformat()}",
            f"-n{limit}",
        )

    def recent(self, limit: int = 20) -> list[CommitInfo]:
        return self._log(f"-n{limit}")

    def touching(self, path: str, limit: int = 20) -> list[CommitInfo]:
        return self._log(f"-n{limit}", "--", path)

    def diff(self, sha: str, context: int = 3) -> str:
        """The patch a commit introduced."""
        return self._run("show", f"--unified={context}", "--format=", sha)

    def file_at(self, sha: str, path: str) -> str:
        return self._run("show", f"{sha}:{path}")

    def blame(self, path: str, start_line: int, end_line: int) -> list[tuple[str, str, str]]:
        """Who last touched each line in a range: (sha, author, line)."""
        raw = self._run(
            "blame",
            "--porcelain",
            f"-L{start_line},{end_line}",
            "--",
            path,
        )
        return _parse_blame(raw)

    def _log(self, *args: str) -> list[CommitInfo]:
        raw = self._run("log", f"--pretty=format:{FORMAT}", "--name-only", *args)
        return _parse_log(raw)


def _parse_log(raw: str) -> list[CommitInfo]:
    commits: list[CommitInfo] = []

    for record in raw.split(RECORD):
        record = record.strip("\n")
        if not record.strip():
            continue

        fields = record.split(FIELD)
        if len(fields) < 7:
            continue

        sha, author, email, when, subject, body, file_block = fields[:7]
        files = [line.strip() for line in file_block.split("\n") if line.strip()]

        commits.append(
            CommitInfo(
                sha=sha[:8],
                author=author,
                email=email,
                committed_at=datetime.fromisoformat(when),
                subject=subject,
                body=body.strip(),
                files=tuple(files),
            )
        )

    return commits


def _parse_blame(raw: str) -> list[tuple[str, str, str]]:
    entries: list[tuple[str, str, str]] = []
    sha = ""
    author = ""

    for line in raw.split("\n"):
        if line.startswith("\t"):
            entries.append((sha[:8], author, line[1:]))
        elif line.startswith("author "):
            author = line[len("author ") :]
        elif line and line[0].isalnum() and len(line.split()[0]) == 40:
            sha = line.split()[0]

    return entries
