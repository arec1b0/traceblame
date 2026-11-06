"""The offline runner (#15): the engine over a frozen snapshot, for calibration (#17) and the
eval reports (#19).

It runs the same view builder and the same engine as the online path, and writes runs to a
sink. It has no publisher and never imports one: an offline replay of D1 must not reach the
live attributor. `mode: eval` (eval_only detectors, golden ids) writes only JSON lines, never
ClickHouse: eval_only findings come from ground truth and must not reach the findings table
the production attributor reads (rule 9).
"""

import logging
from dataclasses import dataclass

from traceblame_detectors.config import ConfigError, DetectorConfig, Mode
from traceblame_detectors.context import BaselineProvider
from traceblame_detectors.engine import FRAMEWORK_VERSION, Providers, RunStatus, run_trace
from traceblame_detectors.eval_golden import SnapshotGoldenProvider
from traceblame_detectors.logs import log_runs
from traceblame_detectors.sinks import JsonlSink, Sink
from traceblame_detectors.sources.snapshot import SnapshotTraceSource


@dataclass(frozen=True)
class OfflineReport:
    traces: int
    runs: int
    error_runs: int
    findings: int
    framework_version: str


def run_offline(
    snapshot: SnapshotTraceSource,
    config: DetectorConfig,
    sink: Sink,
    *,
    baseline: BaselineProvider | None = None,
    logger: logging.Logger | None = None,
) -> OfflineReport:
    if config.mode is Mode.EVAL and not isinstance(sink, JsonlSink):
        raise ConfigError("mode: eval writes JSON lines only, never ClickHouse")
    golden = SnapshotGoldenProvider(snapshot) if config.mode is Mode.EVAL else None
    providers = Providers(baseline=baseline, golden=golden)
    sink.register(config.detectors)
    traces = runs = errors = findings = 0
    for trace_id in snapshot.trace_ids():
        view = snapshot.load(trace_id, config.attr_keys)
        result = run_trace(view, config.detectors, providers)
        sink.write(result)
        if logger is not None:
            log_runs(logger, result)
        traces += 1
        runs += len(result)
        errors += sum(r.status is RunStatus.ERROR for r in result)
        findings += sum(len(r.findings) for r in result)
    return OfflineReport(traces, runs, errors, findings, FRAMEWORK_VERSION)
