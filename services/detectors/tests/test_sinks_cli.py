"""Sinks (params registration, drift), the CLI and the JSON logs."""

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from traceblame_detectors.api import Detector, DetectorContext, FindingDraft, TraceView
from traceblame_detectors.cli import main
from traceblame_detectors.config import DetectorConfig
from traceblame_detectors.engine import Providers, run_trace
from traceblame_detectors.export import export_snapshot
from traceblame_detectors.logs import FIELDS, JsonFormatter, log_runs, setup
from traceblame_detectors.policy import DECLARABLE
from traceblame_detectors.sinks import ClickHouseSink, ParamsDriftError
from traceblame_detectors.testing import TRACE_ID, FakeClickHouse, span_rows, summary_row
from traceblame_detectors.view import build_view

Make = Callable[..., DetectorConfig]


def test_params_are_registered_once(make_config: Make) -> None:
    config = make_config(reference_root={}, reference_baseline={})
    ch = FakeClickHouse()
    ClickHouseSink(ch).register(config.detectors)
    ClickHouseSink(ch).register(config.detectors)
    assert [(p["detector"], p["params"]) for p in ch.params] == [
        ("reference_baseline", '{"score":0.7}'),
        ("reference_root", '{"error_score":0.5,"min_spans":1}'),
    ]
    assert all(p["framework_version"] == "1.0.0" for p in ch.params)
    ClickHouseSink(ch).register(())
    assert len(ch.params) == 2


def test_a_version_registered_with_other_params_stops_the_writer(make_config: Make) -> None:
    config = make_config(reference_root={})
    ch = FakeClickHouse()
    ClickHouseSink(ch).register(config.detectors)
    ch.params[0]["params"] = '{"error_score":0.9,"min_spans":1}'  # collision or drift
    with pytest.raises(ParamsDriftError):
        ClickHouseSink(ch).register(config.detectors)


def test_every_written_version_resolves_to_its_params(make_config: Make) -> None:
    config = make_config(reference_root={"params": {"min_spans": 2}})
    ch = FakeClickHouse()
    sink = ClickHouseSink(ch)
    sink.register(config.detectors)
    view = build_view(summary_row(), span_rows(), DECLARABLE)
    sink.write(run_trace(view, config.detectors, Providers()))
    versions = {f["detector_version"] for f in ch.findings}
    assert versions
    registered = {p["detector_version"]: p["params"] for p in ch.params}
    assert {v: registered[v] for v in versions} == {
        config.detectors[0].detector_version: '{"error_score":0.5,"min_spans":2}'
    }


def test_versions_prints_each_detector(
    tmp_path: Path, registry: dict[str, type[Detector]], capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text("detectors:\n  reference_root: {}\n")
    assert main(["versions", "--config", str(cfg)], registry) == 0
    (line,) = capsys.readouterr().out.splitlines()
    assert json.loads(line)["detector"] == "reference_root"


def _snapshot(tmp_path: Path) -> Path:
    snap = tmp_path / "snap"
    ch = FakeClickHouse([summary_row()], {TRACE_ID: span_rows()})
    export_snapshot(ch, [TRACE_ID], "1.0.0", snap, repo=tmp_path)
    return snap


def test_run_offline_writes_runs_and_the_params_sidecar(
    tmp_path: Path, registry: dict[str, type[Detector]], capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text("detectors:\n  reference_root: {}\n")
    out = tmp_path / "out" / "findings.jsonl"
    argv = [
        "run-offline",
        "--snapshot",
        str(_snapshot(tmp_path)),
        "--config",
        str(cfg),
        "--out",
        str(out),
    ]
    assert main(argv, registry) == 0
    report = json.loads(capsys.readouterr().out)
    assert report == {
        "error_runs": 0,
        "findings": 2,
        "framework_version": "1.0.0",
        "runs": 1,
        "traces": 1,
    }
    (run,) = [json.loads(line) for line in out.read_text().splitlines()]
    assert run["framework_version"] == "1.0.0"
    assert len(run["findings"]) == 2
    assert (tmp_path / "out" / "findings.jsonl.params.jsonl").is_file()


def test_run_offline_with_baselines(tmp_path: Path, registry: dict[str, type[Detector]]) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text("detectors:\n  reference_baseline: {}\n")
    baselines = tmp_path / "b.json"
    row = {
        "service": "agentic-rag",
        "bundle_digest": "sha256:" + "b" * 64,
        "window_start_us": 0,
        "window_end_us": 1,
        "trace_count": 5,
    }
    baselines.write_text(json.dumps([row]))
    out = tmp_path / "f.jsonl"
    argv = [
        "run-offline",
        "--snapshot",
        str(_snapshot(tmp_path)),
        "--config",
        str(cfg),
        "--out",
        str(out),
        "--baselines",
        str(baselines),
    ]
    assert main(argv, registry) == 0
    (run,) = [json.loads(line) for line in out.read_text().splitlines()]
    assert run["findings"][0]["evidence"]["baseline_traces"] == 5


def test_eval_mode_refuses_clickhouse(
    tmp_path: Path, registry: dict[str, type[Detector]], capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text("mode: eval\ndetectors:\n  reference_golden: {}\n")
    argv = [
        "run-offline",
        "--snapshot",
        str(_snapshot(tmp_path)),
        "--config",
        str(cfg),
        "--clickhouse-url",
        "http://127.0.0.1:1/x",
    ]
    assert main(argv, registry) == 2
    assert "never ClickHouse" in capsys.readouterr().err


def test_a_bad_config_or_snapshot_exits_2(
    tmp_path: Path, registry: dict[str, type[Detector]]
) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text("detectors:\n  reference_golden: {}\n")  # eval_only in production
    assert main(["versions", "--config", str(cfg)], registry) == 2
    cfg.write_text("detectors: {}\n")
    argv = [
        "run-offline",
        "--snapshot",
        str(tmp_path),
        "--config",
        str(cfg),
        "--out",
        str(tmp_path / "o"),
    ]
    assert main(argv, registry) == 2


def test_export_needs_trace_ids(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ids = tmp_path / "ids.txt"
    ids.write_text("\n")
    argv = [
        "snapshot-export",
        "--clickhouse-url",
        "http://127.0.0.1:1/x",
        "--assembler-version",
        "1.0.0",
        "--out",
        str(tmp_path / "s"),
        "--trace-ids-file",
        str(ids),
    ]
    assert main(argv) == 2
    assert "no trace ids" in capsys.readouterr().err


def test_logs_are_json_with_trace_detector_and_version(make_config: Make) -> None:
    logger = setup("debug")
    assert setup("info") is logger
    assert len(logger.handlers) == 1
    records: list[logging.LogRecord] = []

    class Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    keep = Keep()
    logger.addHandler(keep)
    logger.setLevel("DEBUG")
    config = make_config(reference_root={})

    class Boom(Detector):
        spec = config.detectors[0].spec

        def detect(self, trace: TraceView, ctx: DetectorContext) -> Sequence[FindingDraft]:
            raise RuntimeError("prompt text must not be logged")

    try:
        bad = replace(config.detectors[0], cls=Boom)
        view = build_view(summary_row(), span_rows(), DECLARABLE)
        log_runs(logger, run_trace(view, [bad, config.detectors[0]], Providers()))
    finally:
        logger.removeHandler(keep)
    lines = [json.loads(JsonFormatter().format(r)) for r in records]
    assert [line["msg"] for line in lines] == ["detector run failed", "detector run"]
    assert lines[0]["error_kind"] == "RuntimeError"
    assert {"trace_id", "detector", "version"} <= set(lines[0])
    assert lines[0]["trace_id"] == TRACE_ID
    assert all("prompt text" not in json.dumps(line) for line in lines)
    assert getattr(records[0], FIELDS)["status"] == "error"
