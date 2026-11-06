"""Read-only context beyond the trace (#15).

A detector never gets a storage client. It declares what it needs (`DetectorSpec.needs`) and
receives a `DetectorContext` that answers only those needs, through providers its caller built:

- SERVICE_BASELINE: a `BaselineProvider` (prompt_regression, #16). Answers are as of a point
  no later than the trace's start, so a run is reproducible.
- EVAL_GOLDEN: a `GoldenProvider`, only for eval_only detectors in an eval-mode offline run
  (traceblame_detectors.eval_golden). The online path never builds one.

Every answer a detector got is recorded; `context_digest` of a run hashes them.
"""

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol, cast

from traceblame_detectors.model import ContextNeed, ServiceBaseline


class UndeclaredContextError(LookupError):
    """A detector asked for context it did not declare."""


class BaselineProvider(Protocol):
    def service_baseline(self, service: str, as_of_us: int) -> ServiceBaseline | None: ...


class GoldenProvider(Protocol):
    def golden_doc_ids(self, trace_id: str) -> tuple[str, ...] | None: ...


class DetectorContext:
    """What one detector run may ask beyond its trace view."""

    def __init__(
        self,
        *,
        trace_id: str,
        trace_start_us: int,
        needs: frozenset[ContextNeed],
        baseline: BaselineProvider | None = None,
        golden: GoldenProvider | None = None,
    ) -> None:
        self._trace_id = trace_id
        self._trace_start_us = trace_start_us
        self._needs = needs
        self._baseline = baseline
        self._golden = golden
        self._used: list[dict[str, Any]] = []

    def service_baseline(self, service: str, as_of_us: int) -> ServiceBaseline | None:
        self._require(ContextNeed.SERVICE_BASELINE)
        if as_of_us > self._trace_start_us:
            raise ValueError("a baseline is read as of the trace's start or earlier")
        value = (
            None if self._baseline is None else self._baseline.service_baseline(service, as_of_us)
        )
        self._used.append(
            {
                "need": ContextNeed.SERVICE_BASELINE.value,
                "service": service,
                "as_of_us": as_of_us,
                "value": None if value is None else value.canonical(),
            }
        )
        return value

    def golden_doc_ids(self) -> tuple[str, ...] | None:
        self._require(ContextNeed.EVAL_GOLDEN)
        value = None if self._golden is None else self._golden.golden_doc_ids(self._trace_id)
        self._used.append(
            {
                "need": ContextNeed.EVAL_GOLDEN.value,
                "value": None if value is None else list(value),
            }
        )
        return value

    def used(self) -> list[dict[str, Any]]:
        """The answers given, in the order they were asked."""
        return list(self._used)

    def _require(self, need: ContextNeed) -> None:
        if need not in self._needs:
            raise UndeclaredContextError(need.value)


class StaticBaselineProvider:
    """Baselines from a fixed list (tests, offline runs): the newest baseline of the service
    whose window ended at or before the requested point."""

    def __init__(self, baselines: Sequence[ServiceBaseline]) -> None:
        self._by_service: dict[str, list[ServiceBaseline]] = {}
        for b in sorted(baselines, key=lambda b: b.window_end_us):
            self._by_service.setdefault(b.service, []).append(b)

    @classmethod
    def from_file(cls, path: Path) -> "StaticBaselineProvider":
        rows = cast("list[Mapping[str, Any]]", json.loads(path.read_text(encoding="utf-8")))
        return cls(
            [
                ServiceBaseline(
                    service=str(r["service"]),
                    bundle_digest=str(r["bundle_digest"]),
                    window_start_us=int(r["window_start_us"]),
                    window_end_us=int(r["window_end_us"]),
                    trace_count=int(r["trace_count"]),
                    outcomes={
                        str(k): float(v)
                        for k, v in cast("Mapping[str, Any]", r.get("outcomes", {})).items()
                    },
                )
                for r in rows
            ]
        )

    def service_baseline(self, service: str, as_of_us: int) -> ServiceBaseline | None:
        found = [b for b in self._by_service.get(service, []) if b.window_end_us <= as_of_us]
        return found[-1] if found else None
