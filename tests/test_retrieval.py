"""Tests for code retrieval."""

from __future__ import annotations

from pathlib import Path

import pytest

from sev0.retrieval import code

SAMPLE = '''\
"""A module."""

TAX = 0.2


class Cart:
    """A shopping cart."""

    def total(self, items):
        return sum(items)

    @property
    def empty(self):
        return not self.items


@decorated
def compute(subtotal, percent):
    if percent is None:
        return subtotal
    return subtotal - subtotal * percent // 100


async def fetch(url):
    return url
'''


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "sample.py").write_text(SAMPLE)
    (tmp_path / "pkg" / "__pycache__").mkdir()
    (tmp_path / "pkg" / "__pycache__" / "junk.py").write_text("x = 1\n")
    return tmp_path


class TestSymbols:
    def test_functions_classes_and_methods_are_all_found(self, tree: Path) -> None:
        names = {s.name for s in code.symbols(tree / "pkg/sample.py", tree)}
        assert names == {"Cart", "total", "empty", "compute", "fetch"}

    def test_async_functions_are_labelled(self, tree: Path) -> None:
        found = {s.name: s.kind for s in code.symbols(tree / "pkg/sample.py", tree)}
        assert found["fetch"] == "async function"
        assert found["Cart"] == "class"
        assert found["total"] == "function"

    def test_a_decorator_is_part_of_the_definition(self, tree: Path) -> None:
        # A route decorator is often the thing that explains a failure, so the
        # span has to reach above the def line.
        compute = next(s for s in code.symbols(tree / "pkg/sample.py", tree) if s.name == "compute")
        assert compute.source.startswith("@decorated")

    def test_source_is_a_whole_definition(self, tree: Path) -> None:
        compute = next(s for s in code.symbols(tree / "pkg/sample.py", tree) if s.name == "compute")
        assert "if percent is None:" in compute.source
        assert compute.source.rstrip().endswith("// 100")

    def test_a_syntax_error_is_reported_not_swallowed(self, tmp_path: Path) -> None:
        broken = tmp_path / "broken.py"
        broken.write_text("def oops(:\n")
        with pytest.raises(code.RetrievalError):
            code.symbols(broken)


class TestEnclosingSymbol:
    def test_the_innermost_definition_wins(self, tree: Path) -> None:
        # Line 10 is inside Cart.total, which is inside Cart. The method is the
        # useful answer.
        found = code.enclosing_symbol(tree / "pkg/sample.py", 10, tree)
        assert found is not None
        assert found.name == "total"

    def test_module_level_lines_have_no_enclosing_symbol(self, tree: Path) -> None:
        assert code.enclosing_symbol(tree / "pkg/sample.py", 3, tree) is None


class TestSearch:
    def test_matches_are_annotated_with_their_function(self, tree: Path) -> None:
        (match,) = code.search(tree, r"subtotal \* percent")
        assert match.symbol == "compute"
        assert match.file == "pkg/sample.py"

    def test_module_level_matches_report_no_symbol(self, tree: Path) -> None:
        (match,) = code.search(tree, r"^TAX")
        assert match.symbol is None

    def test_cache_directories_are_skipped(self, tree: Path) -> None:
        assert code.search(tree, r"x = 1") == []

    def test_the_limit_is_respected(self, tree: Path) -> None:
        assert len(code.search(tree, r".", limit=3)) == 3

    def test_a_bad_pattern_fails_loudly(self, tree: Path) -> None:
        with pytest.raises(code.RetrievalError, match="bad pattern"):
            code.search(tree, r"(unclosed")


class TestGetSymbol:
    def test_a_named_symbol_is_returned_whole(self, tree: Path) -> None:
        symbol = code.get_symbol(tree, "pkg/sample.py", "total")
        assert symbol.source.strip().startswith("def total")

    def test_an_unknown_symbol_lists_what_is_available(self, tree: Path) -> None:
        with pytest.raises(code.RetrievalError, match="compute"):
            code.get_symbol(tree, "pkg/sample.py", "nonexistent")

    def test_a_missing_file_is_reported(self, tree: Path) -> None:
        with pytest.raises(code.RetrievalError, match="no such file"):
            code.get_symbol(tree, "pkg/gone.py", "total")
