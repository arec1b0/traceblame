import math

import assembler_report as report
import pytest


def test_percentile_is_nearest_rank() -> None:
    values = [float(i) for i in range(1, 101)]
    assert report.percentile(values, 0.5) == 50
    assert report.percentile(values, 0.95) == 95
    assert report.percentile([3.0], 0.95) == 3
    assert math.isnan(report.percentile([], 0.5))


def test_the_first_revision_is_the_first_assembly_and_later_ones_the_tail() -> None:
    events = [
        report.Event("a", 100, 3, 4_000),
        report.Event("a", 900, 4, 70_000),
        report.Event("b", 200, 2, 5_000),
        report.Event("other-run", 1, 1, 1),
    ]
    lags = report.completion_lags(events, {"a": 1_000, "b": 2_000})
    assert lags.first == [3.0, 3.0]
    assert lags.again == [69.0]


def test_a_split_trace_counts_once_per_outcome() -> None:
    stored = (
        [("whole", "gw-0")] * 2 + [("whole", "gw-1")] * 2
        + [("orphan", "gw-0"), ("orphan", "gw-1")]
        + [("missing", "gw-0"), ("missing", "gw-1")]
        + [("short", "gw-0"), ("short", "gw-1"), ("short", "gw-1")]
        + [("single", "gw-0")] * 3
    )  # fmt: skip
    got = report.rejoin_report(
        stored,
        roots={"whole", "missing", "short", "single"},
        summary_span_count={"whole": 4, "short": 2, "single": 3},
    )
    assert got == report.Rejoin(split_kept=4, rejoined=1, no_root=1, unassembled=1, mismatched=1)


def test_the_markdown_has_both_tables_and_marks_missing_data() -> None:
    text = report.render(report.Lags([1.0, 2.0, 3.0], []), report.Rejoin(5, 4, 1, 0, 0), "0a1b2c3d")
    assert "| first assembly | 3 | 2.00 s | 3.00 s |" in text
    assert "| re-assembly (late spans) | 0 | n/a | n/a |" in text
    assert "| 5 | 4 | 1 | 0 | 0 |" in text
    assert "0a1b2c3d" in text


@pytest.mark.parametrize("q", [0.5, 0.95])
def test_percentile_of_one_value_is_that_value(q: float) -> None:
    assert report.percentile([7.5], q) == 7.5
