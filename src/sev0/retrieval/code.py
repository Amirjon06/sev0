"""Code retrieval over the repository under investigation.

Uses Python's own `ast` rather than tree-sitter. The target is single-language
by design, so the extra dependency would buy nothing, and the stdlib parser
already knows exactly where a function starts and ends.

That matters more than it sounds. Retrieval that returns a fixed window of
lines around a match will cut a function in half, and half a function is often
worse than none: the model sees a branch without the guard above it and
confidently explains a bug that is not there. Everything here returns whole
definitions.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules", ".mypy_cache", ".ruff_cache"}


class RetrievalError(RuntimeError):
    pass


@dataclass(frozen=True)
class Symbol:
    name: str
    kind: str
    file: str
    start_line: int
    end_line: int
    source: str

    @property
    def location(self) -> str:
        return f"{self.file}:{self.start_line}-{self.end_line}"


@dataclass(frozen=True)
class Match:
    file: str
    line_number: int
    line: str
    symbol: str | None


def python_files(root: Path) -> list[Path]:
    found: list[Path] = []
    for path in sorted(root.rglob("*.py")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        found.append(path)
    return found


def _parse(path: Path) -> ast.Module:
    try:
        return ast.parse(path.read_text())
    except SyntaxError as exc:
        raise RetrievalError(f"{path}: {exc}") from exc


def _definitions(tree: ast.Module) -> list[ast.AST]:
    found: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.append(node)
    return found


def _kind(node: ast.AST) -> str:
    if isinstance(node, ast.ClassDef):
        return "class"
    if isinstance(node, ast.AsyncFunctionDef):
        return "async function"
    return "function"


def _span(node: ast.AST) -> tuple[int, int]:
    start = getattr(node, "lineno", 0)
    # Decorators sit above the def line and are part of the definition's
    # meaning -- a route decorator is often exactly what explains a failure.
    decorators = getattr(node, "decorator_list", [])
    if decorators:
        start = min(start, min(d.lineno for d in decorators))
    end = getattr(node, "end_lineno", start) or start
    return start, end


def symbols(path: Path, root: Path | None = None) -> list[Symbol]:
    """Every function and class in a file, with its full source."""
    tree = _parse(path)
    lines = path.read_text().splitlines()
    relative = str(path.relative_to(root)) if root else str(path)

    found: list[Symbol] = []
    for node in _definitions(tree):
        start, end = _span(node)
        found.append(
            Symbol(
                name=getattr(node, "name", "?"),
                kind=_kind(node),
                file=relative,
                start_line=start,
                end_line=end,
                source="\n".join(lines[start - 1 : end]),
            )
        )

    return sorted(found, key=lambda s: s.start_line)


def outline(path: Path, root: Path | None = None) -> list[str]:
    """A cheap map of a file, for deciding whether it is worth reading."""
    return [f"{s.start_line:>4}  {s.kind} {s.name}" for s in symbols(path, root)]


def enclosing_symbol(path: Path, line_number: int, root: Path | None = None) -> Symbol | None:
    """The innermost definition containing a line.

    Innermost matters: a method inside a class matches both, and the method is
    the useful answer.
    """
    candidates = [
        s for s in symbols(path, root) if s.start_line <= line_number <= s.end_line
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda s: s.end_line - s.start_line)


def get_symbol(root: Path, file: str, name: str) -> Symbol:
    path = root / file
    if not path.exists():
        raise RetrievalError(f"no such file: {file}")

    for symbol in symbols(path, root):
        if symbol.name == name:
            return symbol

    known = ", ".join(s.name for s in symbols(path, root)) or "none"
    raise RetrievalError(f"{file} has no symbol named {name!r}; found: {known}")


def search(
    root: Path,
    pattern: str,
    *,
    limit: int = 50,
    ignore_case: bool = False,
) -> list[Match]:
    """Regex search across the tree, annotated with the enclosing definition.

    The annotation is the point. A bare grep hit tells the model a string
    exists somewhere; knowing it sits inside `compute_total` tells it what to
    read next.
    """
    try:
        expression = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        raise RetrievalError(f"bad pattern {pattern!r}: {exc}") from exc

    matches: list[Match] = []

    for path in python_files(root):
        try:
            lines = path.read_text().splitlines()
        except UnicodeDecodeError:
            continue

        hits = [(n, text) for n, text in enumerate(lines, start=1) if expression.search(text)]
        if not hits:
            continue

        try:
            file_symbols = symbols(path, root)
        except RetrievalError:
            file_symbols = []

        for line_number, text in hits:
            enclosing = [
                s for s in file_symbols if s.start_line <= line_number <= s.end_line
            ]
            innermost = (
                min(enclosing, key=lambda s: s.end_line - s.start_line) if enclosing else None
            )
            matches.append(
                Match(
                    file=str(path.relative_to(root)),
                    line_number=line_number,
                    line=text.strip(),
                    symbol=innermost.name if innermost else None,
                )
            )
            if len(matches) >= limit:
                return matches

    return matches
