"""The online handler, the offline runner and the snapshot between them: the same trace gives
the same findings on both paths; offline never publishes; eval mode never writes ClickHouse."""

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from traceblame_detectors.config import ConfigError, DetectorConfig, Mode
from traceblame_detectors.engine import RunStatus
from traceblame_detectors.export import export_snapshot
from traceblame_detectors.offline import run_offline
from traceblame_detectors.online import (
    NotYetVisibleError,
    Outcome,
    handle,
    require_production,
)
from traceblame_detectors.sinks import ClickHouseSink, JsonlSink
from traceblame_detectors.sources.clickhouse import ClickHouseTraceSource
from traceblame_detectors.sources.snapshot import SnapshotError, SnapshotTraceSource
from traceblame_detectors.testing import (
    INJECTED_TEXT,
    INJECTION_ID,
    RUN_ID,
    TRACE_ID,
    FakeClickHouse,
    span_rows,
    summary_row,
)

Make = Callable[..., DetectorConfig]
SECOND = "5bf92f3577b34da6a3ce929d0e0e4737"


def _ch() -> FakeClickHouse:
    second = [{**r, "status_code": "Unset"} for r in span_rows()]
    return FakeClickHouse(
        summaries=[summary_row(), summary_row(trace_id=SECOND, revision_ms=1_793_700_000_500)],
        spans={TRACE_ID: span_rows(), SECOND: second},
    )


def _online(config: DetectorConfig, ch: FakeClickHouse, trace_id: str, revision_ms: int) -> Any:
    return handle(
        trace_id,
        revision_ms,
        "1.0.0",
        config=config,
        source=ClickHouseTraceSource(ch),
        sink=ClickHouseSink(ch),
    )


def test_online_and_offline_give_identical_findings(make_config: Make, tmp_path: Path) -> None:
    config = make_config(reference_root={}, reference_baseline={})
    online_ch = _ch()
    for tid, rev in ((TRACE_ID, 1_793_700_000_123), (SECOND, 1_793_700_000_500)):
        assert _online(config, online_ch, tid, rev).outcome is Outcome.OK

    snap = tmp_path / "snap"
    manifest = export_snapshot(_ch(), [TRACE_ID, SECOND], "1.0.0", snap, repo=tmp_path)
    assert manifest["trace_count"] == 2
    assert manifest["span_count"] == 8
    out = tmp_path / "findings.jsonl"
    report = run_offline(SnapshotTraceSource(snap), config, JsonlSink(out))
    assert (report.traces, report.runs, report.error_runs) == (2, 4, 0)

    offline = [json.loads(line) for line in out.read_text().splitlines()]
    online = {(r["trace_id"], r["detector"]): r for r in online_ch.runs}
    checked = 0
    for run in offline:
        mine = online[(run["trace_id"], run["detector"])]
        assert (mine["input_digest"], mine["findings_digest"], mine["detector_version"]) == (
            run["input_digest"],
            run["findings_digest"],
            run["detector_version"],
        )
        checked += 1
    assert checked == 4
    assert sum(r["finding_count"] for r in online_ch.runs) == report.findings == 3


def test_the_snapshot_holds_no_labels(tmp_path: Path) -> None:
    snap = tmp_path / "snap"
    export_snapshot(_ch(), [TRACE_ID], "1.0.0", snap, repo=tmp_path)
    assert sorted(p.name for p in snap.iterdir()) == [
        "manifest.json",
        "spans.jsonl",
        "trace_summary.jsonl",
    ]
    text = "".join(p.read_text() for p in snap.iterdir())
    for needle in (
        RUN_ID,
        INJECTION_ID,
        INJECTED_TEXT,
        "aegis.eval",
        "aegis.fault",
        "exception.",
        "injection",
        "planned_fault_kind",
        "query_ref",
        "tenant",
        "resource_attrs",
    ):
        assert needle not in text, needle


def test_the_manifest_records_provenance(tmp_path: Path) -> None:
    manifest = export_snapshot(
        _ch(),
        [TRACE_ID],
        "1.0.0",
        tmp_path / "s",
        repo=tmp_path,
        postgres_migrations=[(1, "x", "ab")],
    )
    assert manifest["commit"] is None
    assert manifest["dirty"] is None
    assert manifest["postgres_migrations"] == [{"version": 1, "name": "x", "sha256": "ab"}]
    assert set(manifest["contract_reads"]) == {
        "trace_summary_read.sql",
        "spans_trace_view_read.sql",
    }
    assert manifest["profile_versions"] == ["1.6.0"]
    assert manifest["assembler_versions"] == ["1.0.0"]
    assert "30 days" in manifest["ttl_note"]
    repo = Path(__file__).resolve().parents[3]
    git = export_snapshot(_ch(), [TRACE_ID], "1.0.0", tmp_path / "g", repo=repo)
    assert len(git["commit"]) == 40
    assert isinstance(git["dirty"], bool)


def test_export_refuses_a_missing_trace_and_a_used_directory(tmp_path: Path) -> None:
    with pytest.raises(LookupError):
        export_snapshot(_ch(), ["f" * 32], "1.0.0", tmp_path / "a", repo=tmp_path)
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "x").write_text("x")
    with pytest.raises(FileExistsError):
        export_snapshot(_ch(), [TRACE_ID], "1.0.0", tmp_path / "b", repo=tmp_path)


@pytest.mark.parametrize(
    ("name", "content", "match"),
    [
        ("spans.jsonl", "{}\n", "sha256"),
        ("injections.jsonl", "{}\n", "unexpected files"),
        ("manifest.json", None, "no manifest"),
        ("golden.jsonl", "{}\n", "file list"),
    ],
)
def test_a_damaged_snapshot_is_refused(
    tmp_path: Path, name: str, content: str | None, match: str
) -> None:
    snap = tmp_path / "snap"
    export_snapshot(_ch(), [TRACE_ID], "1.0.0", snap, repo=tmp_path)
    if content is None:
        (snap / name).unlink()
    else:
        (snap / name).write_text(content)
    with pytest.raises(SnapshotError, match=match):
        SnapshotTraceSource(snap)


def _with_golden(snap: Path, rows: list[dict[str, Any]]) -> None:
    from traceblame_detectors.canonical import sha256_hex

    golden = snap / "golden.jsonl"
    golden.write_text("".join(json.dumps(r) + "\n" for r in rows))
    manifest = json.loads((snap / "manifest.json").read_text())
    manifest["files"]["golden.jsonl"] = sha256_hex(golden.read_bytes())
    (snap / "manifest.json").write_text(json.dumps(manifest))


def test_eval_mode_reads_golden_ids_and_writes_json_lines_only(
    make_config: Make, tmp_path: Path
) -> None:
    snap = tmp_path / "snap"
    export_snapshot(_ch(), [TRACE_ID], "1.0.0", snap, repo=tmp_path)
    _with_golden(snap, [{"trace_id": TRACE_ID, "golden_doc_ids": ["cuad-ffffffffffff-1"]}])
    config = make_config("eval", reference_golden={})
    with pytest.raises(ConfigError, match="never ClickHouse"):
        run_offline(SnapshotTraceSource(snap), config, ClickHouseSink(FakeClickHouse()))
    out = tmp_path / "eval.jsonl"
    report = run_offline(SnapshotTraceSource(snap), config, JsonlSink(out))
    assert report.findings == 1
    params = [
        json.loads(line) for line in (tmp_path / "eval.jsonl.params.jsonl").read_text().splitlines()
    ]
    assert [p["detector"] for p in params] == ["reference_golden"]
    assert params[0]["params"] == '{"top_k":5}'


def test_golden_rows_hold_golden_ids_only(make_config: Make, tmp_path: Path) -> None:
    snap = tmp_path / "snap"
    export_snapshot(_ch(), [TRACE_ID], "1.0.0", snap, repo=tmp_path)
    _with_golden(snap, [{"trace_id": TRACE_ID, "golden_doc_ids": [], "planned_fault_kind": "x"}])
    with pytest.raises(ValueError, match="more than golden ids"):
        run_offline(SnapshotTraceSource(snap), make_config("eval"), JsonlSink(tmp_path / "o"))


def test_the_online_path_runs_production_only(make_config: Make) -> None:
    eval_config = make_config("eval", reference_root={})
    with pytest.raises(ConfigError, match="production"):
        require_production(eval_config)
    sneaked = DetectorConfig(
        Mode.PRODUCTION, make_config("eval", reference_golden={}).detectors, {}, {}
    )
    with pytest.raises(ConfigError, match="eval_only"):
        _online(sneaked, _ch(), TRACE_ID, 1)


def test_a_revision_not_yet_in_trace_summary_is_retried_later(make_config: Make) -> None:
    config = make_config(reference_root={})
    with pytest.raises(NotYetVisibleError):
        _online(config, _ch(), TRACE_ID, 1_793_700_000_124)
    with pytest.raises(NotYetVisibleError):
        _online(config, _ch(), "e" * 32, 1)


def test_an_older_event_after_a_newer_run_is_stale(make_config: Make) -> None:
    config = make_config(reference_root={})
    ch = _ch()
    ch.summaries.append(summary_row(revision_ms=1_793_700_000_900))
    assert _online(config, ch, TRACE_ID, 1_793_700_000_123).outcome is Outcome.OK
    writes = len(ch.commands)
    ch.summaries.pop()  # a replica that still answers with the older summary
    result = _online(config, ch, TRACE_ID, 1_793_700_000_123)
    assert result.outcome is Outcome.STALE
    assert len(ch.commands) == writes


def test_a_redelivered_event_writes_the_same_rows(make_config: Make) -> None:
    config = make_config(reference_root={})
    ch = _ch()
    _online(config, ch, TRACE_ID, 1_793_700_000_123)
    first = (list(ch.findings), list(ch.runs))
    _online(config, ch, TRACE_ID, 1_793_700_000_123)
    assert ch.findings[len(first[0]) :] == first[0]
    assert ch.runs[len(first[1]) :] == first[1]


def test_findings_are_written_before_their_run_rows(make_config: Make) -> None:
    ch = _ch()
    _online(
        make_config(reference_root={"params": {"min_spans": 99}}, reference_baseline={}),
        ch,
        TRACE_ID,
        1,
    )
    assert ch.commands == ["detectors_runs_write.sql"]  # nothing found: the marker only
    ch2 = _ch()
    _online(make_config(reference_root={}), ch2, TRACE_ID, 1)
    assert ch2.commands == ["detectors_findings_write.sql", "detectors_runs_write.sql"]
    assert all(r["status"] == RunStatus.OK.value for r in ch2.runs)


def _imports(module: str) -> set[str]:
    code = f"import sys, {module}; print('\\n'.join(sorted(sys.modules)))"
    out = subprocess.run(  # noqa: S603 - the test's own interpreter
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True
    )
    return set(out.stdout.split())


def test_the_offline_path_never_loads_a_publisher() -> None:
    loaded = _imports("traceblame_detectors.offline") | _imports("traceblame_detectors.cli")
    assert "traceblame_detectors.offline" in loaded  # the check saw the module
    assert not {m for m in loaded if m == "nats" or m.startswith("nats.")}
    assert "traceblame_detectors.publish" not in loaded


def test_the_online_path_never_loads_the_golden_provider() -> None:
    loaded = _imports("traceblame_detectors.online")
    assert "traceblame_detectors.online" in loaded
    assert "traceblame_detectors.eval_golden" not in loaded
    assert "traceblame_detectors.offline" not in loaded
