"""The online path's handler for one trace.completed event (#15), without the transport.

PR-B's worker calls `handle` per JetStream message and acks only after it returned (the writes
are acknowledged); the integration test calls it directly. Production only: an eval-mode config
or an eval_only detector never runs here, and no golden provider is ever built on this path.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from traceblame_detectors.config import ConfigError, DetectorConfig, Mode
from traceblame_detectors.context import BaselineProvider
from traceblame_detectors.engine import DetectorRun, Providers, run_trace
from traceblame_detectors.view import TraceView


class NotYetVisibleError(RuntimeError):
    """trace_summary does not hold the event's revision yet (or no row at all): retry later."""


class Outcome(StrEnum):
    OK = "ok"
    STALE = "stale"


@dataclass(frozen=True)
class HandleResult:
    outcome: Outcome
    revision_ms: int
    runs: tuple[DetectorRun, ...] = ()


class Source(Protocol):
    def load(
        self, trace_id: str, assembler_version: str, attr_keys: frozenset[str]
    ) -> TraceView | None: ...


class OnlineSink(Protocol):
    def write(self, runs: tuple[DetectorRun, ...]) -> None: ...

    def latest_revisions(self, trace_id: str) -> dict[tuple[str, str], int]: ...


def require_production(config: DetectorConfig) -> None:
    if config.mode is not Mode.PRODUCTION:
        raise ConfigError("the online path runs only mode: production")
    if any(d.spec.eval_only for d in config.detectors):
        raise ConfigError("the online path never runs eval_only detectors")


def handle(
    trace_id: str,
    revision_ms: int,
    assembler_version: str,
    *,
    config: DetectorConfig,
    source: Source,
    sink: OnlineSink,
    baseline: BaselineProvider | None = None,
) -> HandleResult:
    require_production(config)
    view = source.load(trace_id, assembler_version, config.attr_keys)
    if view is None or view.revision_ms < revision_ms:
        raise NotYetVisibleError(trace_id)
    latest = sink.latest_revisions(trace_id)
    if config.detectors and all(
        latest.get((d.spec.name, d.detector_version), -1) > view.revision_ms
        for d in config.detectors
    ):
        return HandleResult(Outcome.STALE, view.revision_ms)
    providers = Providers(baseline=baseline)
    runs = run_trace(view, config.detectors, providers)
    sink.write(runs)
    return HandleResult(Outcome.OK, view.revision_ms, runs)
