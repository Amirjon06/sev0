"""Tests for the evidence collectors.

Loki and Prometheus are stubbed with a mock transport rather than a live stack,
so these run in CI without Docker. What is being tested is the parsing and the
judgement — onset detection, deduplication — not httpx.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from sev0.collectors import history, logs, metrics
from sev0.collectors.models import MetricPoint, Series

T0 = datetime(2026, 8, 17, 21, 45, tzinfo=UTC)


def ns(offset_seconds: float) -> str:
    return str(int((T0 + timedelta(seconds=offset_seconds)).timestamp() * 1e9))


def stub(payload: dict[str, object], status_code: int = 200) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload)

    return httpx.Client(transport=httpx.MockTransport(handler))


def line(message: str, **fields: object) -> str:
    return json.dumps({"level": "INFO", "service": "cart", "message": message, **fields})


class TestLokiParsing:
    def build(self, values: list[list[str]]) -> logs.LokiCollector:
        payload = {
            "status": "success",
            "data": {"result": [{"stream": {"service": "cart"}, "values": values}]},
        }
        return logs.LokiCollector("http://loki:3100", client=stub(payload))

    def test_structured_lines_are_parsed_into_fields(self) -> None:
        collector = self.build(
            [
                [
                    ns(0),
                    line("request done", status=500, path="/carts/{user_id}", duration_ms=4.2),
                ]
            ]
        )
        (parsed,) = collector.query_range("{}", T0, T0 + timedelta(minutes=1))

        assert parsed.service == "cart"
        assert parsed.status == 500
        assert parsed.path == "/carts/{user_id}"
        assert parsed.fields["duration_ms"] == 4.2

    def test_non_json_lines_are_kept_verbatim(self) -> None:
        # Tracebacks and Postgres output are not ours, and are often the most
        # useful thing in the window.
        collector = self.build([[ns(0), "  TypeError: unsupported operand type(s)  "]])
        (parsed,) = collector.query_range("{}", T0, T0 + timedelta(minutes=1))

        assert parsed.message == "TypeError: unsupported operand type(s)"
        assert parsed.status is None

    def test_a_non_200_response_raises(self) -> None:
        collector = logs.LokiCollector("http://loki:3100", client=stub({}, status_code=503))
        with pytest.raises(logs.LokiError, match="503"):
            collector.query_range("{}", T0, T0 + timedelta(minutes=1))

    def test_a_failed_query_raises_even_on_200(self) -> None:
        collector = logs.LokiCollector(
            "http://loki:3100", client=stub({"status": "error", "error": "parse error"})
        )
        with pytest.raises(logs.LokiError, match="query failed"):
            collector.query_range("{{", T0, T0 + timedelta(minutes=1))


class TestLogSummarising:
    def make(self, count: int, message: str = "request completed") -> list[logs.LogLine]:
        return [
            logs.LogLine(
                timestamp=T0 + timedelta(seconds=i),
                service="cart",
                level="INFO",
                message=message,
                status=500,
            )
            for i in range(count)
        ]

    def test_repeated_lines_collapse_to_a_few_examples(self) -> None:
        assert len(logs.summarise(self.make(500), per_shape=2)) == 2

    def test_distinct_shapes_are_kept_separately(self) -> None:
        lines = self.make(10, "request completed") + self.make(10, "charge declined")
        assert len(logs.summarise(lines, per_shape=2)) == 4

    def test_ids_do_not_split_a_shape(self) -> None:
        # Two lines differing only by a request id are the same event.
        lines = [
            logs.LogLine(T0, "cart", "ERROR", "failed for user-0042", status=500),
            logs.LogLine(T0, "cart", "ERROR", "failed for user-9999", status=500),
        ]
        assert len(logs.summarise(lines, per_shape=1)) == 1

    def test_summary_is_ordered_oldest_first(self) -> None:
        lines = list(reversed(self.make(5, "a"))) + list(reversed(self.make(5, "b")))
        kept = logs.summarise(lines, per_shape=1)
        assert kept == sorted(kept, key=lambda item: item.timestamp)

    def test_shape_counts_report_the_real_volume(self) -> None:
        lines = self.make(30, "noisy") + self.make(3, "rare")
        counts = dict((shape.split("|")[2], n) for shape, n in logs.shape_counts(lines))
        assert counts["noisy"] == 30
        assert counts["rare"] == 3


class TestPrometheusParsing:
    def test_matrix_is_parsed_into_series(self) -> None:
        payload = {
            "status": "success",
            "data": {
                "result": [
                    {
                        "metric": {"service": "gateway"},
                        "values": [[T0.timestamp(), "0.01"], [T0.timestamp() + 15, "0.14"]],
                    }
                ]
            },
        }
        collector = metrics.PrometheusCollector("http://prom:9090", client=stub(payload))
        (series,) = collector.error_rate(T0, T0 + timedelta(minutes=1))

        assert series.name == "gateway"
        assert [p.value for p in series.points] == [0.01, 0.14]

    def test_nan_samples_are_dropped(self) -> None:
        # A ratio with no traffic in the denominator comes back as "NaN".
        payload = {
            "status": "success",
            "data": {
                "result": [
                    {
                        "metric": {"service": "cart"},
                        "values": [[T0.timestamp(), "NaN"], [T0.timestamp() + 15, "0.5"]],
                    }
                ]
            },
        }
        collector = metrics.PrometheusCollector("http://prom:9090", client=stub(payload))
        (series,) = collector.error_rate(T0, T0 + timedelta(minutes=1))

        assert [p.value for p in series.points] == [0.5]

    def test_a_non_200_response_raises(self) -> None:
        collector = metrics.PrometheusCollector("http://prom:9090", client=stub({}, 500))
        with pytest.raises(metrics.PrometheusError, match="500"):
            collector.error_rate(T0, T0 + timedelta(minutes=1))


class TestOnsetDetection:
    def series(self, values: list[float], name: str = "gateway") -> Series:
        return Series(
            labels={"service": name},
            points=tuple(
                MetricPoint(T0 + timedelta(seconds=15 * i), v) for i, v in enumerate(values)
            ),
        )

    def test_a_sustained_breach_is_an_onset(self) -> None:
        onset = metrics.find_onset(self.series([0, 0, 0, 0.2, 0.3, 0.25]), threshold=0.05)

        assert onset is not None
        assert onset.at == T0 + timedelta(seconds=45)
        assert onset.peak == pytest.approx(0.3)
        assert onset.baseline == pytest.approx(0.0)

    def test_a_single_spike_is_not_an_onset(self) -> None:
        # Restarts and one unlucky request produce exactly this shape.
        assert metrics.find_onset(self.series([0, 0, 0.9, 0, 0, 0]), threshold=0.05) is None

    def test_a_flat_healthy_series_has_no_onset(self) -> None:
        assert metrics.find_onset(self.series([0.0] * 10), threshold=0.05) is None

    def test_a_longer_sustain_requirement_rejects_a_short_burst(self) -> None:
        values = [0, 0, 0.2, 0.2, 0, 0]
        assert metrics.find_onset(self.series(values), 0.05, sustain=2) is not None
        assert metrics.find_onset(self.series(values), 0.05, sustain=4) is None

    def test_first_onset_returns_the_earliest_across_services(self) -> None:
        early = self.series([0, 0.3, 0.3, 0.3], name="gateway")
        late = self.series([0, 0, 0, 0.3, 0.3, 0.3], name="cart")

        onset = metrics.first_onset([late, early], threshold=0.05)
        assert onset is not None
        assert onset.series == "gateway"

    def test_first_onset_is_none_when_nothing_breached(self) -> None:
        assert metrics.first_onset([self.series([0.0] * 5)], threshold=0.05) is None

    def test_the_incident_window_leans_on_what_came_before(self) -> None:
        onset = metrics.find_onset(self.series([0, 0, 0.2, 0.2]), threshold=0.05)
        assert onset is not None

        start, end = metrics.incident_window(onset)
        assert onset.at - start == timedelta(minutes=15)
        assert end - onset.at == timedelta(minutes=5)


class TestGitHistory:
    @pytest.fixture
    def repo(self, tmp_path: Path) -> Path:
        def git(*args: str) -> None:
            subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

        git("init", "-q", "-b", "main")
        git("config", "user.name", "Dana Whitfield")
        git("config", "user.email", "dana@storefront.example")

        (tmp_path / "app.py").write_text("def total(x):\n    return x\n")
        git("add", "-A")
        git("commit", "-q", "-m", "feat: Add totals")

        (tmp_path / "other.py").write_text("value = 1\n")
        git("add", "-A")
        git(
            "commit",
            "-q",
            "-m",
            "refactor: Tidy up\n\nA body with a full stop and a path/like/thing.\n\nRefs: #12",
        )
        return tmp_path

    def test_recent_returns_commits_newest_first(self, repo: Path) -> None:
        collector = history.GitHistoryCollector(repo)
        found = collector.recent()

        assert [c.subject for c in found] == ["refactor: Tidy up", "feat: Add totals"]

    def test_a_multi_paragraph_body_is_not_mistaken_for_filenames(self, repo: Path) -> None:
        collector = history.GitHistoryCollector(repo)
        latest = collector.recent()[0]

        assert latest.files == ("other.py",)
        assert "full stop" in latest.body
        assert "Refs: #12" in latest.body

    def test_touching_filters_to_one_path(self, repo: Path) -> None:
        collector = history.GitHistoryCollector(repo)
        found = collector.touching("app.py")

        assert [c.subject for c in found] == ["feat: Add totals"]

    def test_diff_returns_the_patch(self, repo: Path) -> None:
        collector = history.GitHistoryCollector(repo)
        patch = collector.diff(collector.recent()[0].sha)

        assert "other.py" in patch
        assert "+value = 1" in patch

    def test_blame_attributes_each_line(self, repo: Path) -> None:
        collector = history.GitHistoryCollector(repo)
        blamed = collector.blame("app.py", 1, 2)

        assert [text for _, _, text in blamed] == ["def total(x):", "    return x"]
        assert all(author == "Dana Whitfield" for _, author, _ in blamed)

    def test_file_at_reads_a_revision(self, repo: Path) -> None:
        collector = history.GitHistoryCollector(repo)
        assert "def total" in collector.file_at("HEAD", "app.py")

    def test_a_directory_without_git_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(history.GitError, match="not a git repository"):
            history.GitHistoryCollector(tmp_path / "nowhere")
