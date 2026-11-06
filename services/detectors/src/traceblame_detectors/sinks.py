"""Where runs go: ClickHouse (online, and offline in production mode) or JSON lines (offline).

ClickHouse writes are plain synchronous INSERTs, acknowledged by the server before they return
(CLAUDE.md rule 10). Per trace: the findings first, then the detector_runs rows, the commit
marker; a crash in between leaves findings no run row points to, and the replay writes the same
rows again.
"""

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from traceblame_detectors.config import EnabledDetector
from traceblame_detectors.engine import FRAMEWORK_VERSION, DetectorRun
from traceblame_detectors.sql_statements import statement


class ParamsDriftError(RuntimeError):
    """A detector_version is registered with other params: a digest collision or a drifted
    config. Refuse to write findings under it."""


class Client(Protocol):
    def command(self, sql: str, parameters: Mapping[str, Any] | None = None) -> object: ...

    def query_rows(
        self, sql: str, parameters: Mapping[str, Any] | None = None
    ) -> list[tuple[Any, ...]]: ...


class Sink(Protocol):
    def register(self, detectors: Sequence[EnabledDetector]) -> None: ...

    def write(self, runs: Sequence[DetectorRun]) -> None: ...


def params_record(d: EnabledDetector) -> dict[str, str]:
    return {
        "detector": d.spec.name,
        "detector_version": d.detector_version,
        "code_version": d.spec.code_version,
        "framework_version": FRAMEWORK_VERSION,
        "params": d.params_json,
    }


class ClickHouseSink:
    def __init__(self, client: Client) -> None:
        self._client = client

    def register(self, detectors: Sequence[EnabledDetector]) -> None:
        """Record every detector_version's params; fail if one is registered differently."""
        if not detectors:
            return
        wanted = {(d.spec.name, d.detector_version): params_record(d) for d in detectors}
        rows = self._client.query_rows(
            statement("detectors_params_read.sql"),
            {"detector_versions": sorted({v for _, v in wanted})},
        )
        have = {(str(r[0]), str(r[1])): str(r[4]) for r in rows}
        for key, record in sorted(wanted.items()):
            if key in have and have[key] != record["params"]:
                raise ParamsDriftError(
                    f"{key[0]} {key[1]} is registered with other params: "
                    f"{have[key]} != {record['params']}"
                )
            if key not in have:
                self._client.command(statement("detectors_params_write.sql"), record)

    def write(self, runs: Sequence[DetectorRun]) -> None:
        for trace_runs in _by_trace(runs):
            self._write_trace(trace_runs)

    def _write_trace(self, runs: Sequence[DetectorRun]) -> None:
        first = runs[0]
        findings = [f for r in runs for f in r.findings]
        if findings:
            self._client.command(
                statement("detectors_findings_write.sql"),
                {
                    "trace_id": first.trace_id,
                    "revision_ms": first.revision_ms,
                    "finding_ids": [f.finding_id for f in findings],
                    "span_ids": [f.span_id for f in findings],
                    "detectors": [f.detector for f in findings],
                    "detector_versions": [f.detector_version for f in findings],
                    "input_digests": [f.input_digest for f in findings],
                    "run_digests": [f.run_digest for f in findings],
                    "components": [f.component for f in findings],
                    "severities": [f.severity for f in findings],
                    "scores": [f.score for f in findings],
                    "details": [f.evidence_json() for f in findings],
                },
            )
        self._client.command(
            statement("detectors_runs_write.sql"),
            {
                "trace_id": first.trace_id,
                "revision_ms": first.revision_ms,
                "assembler_version": first.assembler_version,
                "framework_version": first.framework_version,
                "detectors": [r.detector for r in runs],
                "detector_versions": [r.detector_version for r in runs],
                "input_digests": [r.input_digest for r in runs],
                "context_digests": [r.context_digest for r in runs],
                "run_digests": [r.run_digest for r in runs],
                "statuses": [r.status.value for r in runs],
                "error_kinds": [r.error_kind for r in runs],
                "finding_counts": [len(r.findings) for r in runs],
                "findings_digests": [r.findings_digest for r in runs],
            },
        )

    def latest_revisions(self, trace_id: str) -> dict[tuple[str, str], int]:
        """(detector, detector_version) -> the newest revision a run was recorded for."""
        rows = self._client.query_rows(statement("detectors_runs_read.sql"), {"trace_id": trace_id})
        return {(str(r[0]), str(r[1])): int(r[2]) for r in rows}


def _by_trace(runs: Sequence[DetectorRun]) -> list[list[DetectorRun]]:
    groups: dict[tuple[str, int], list[DetectorRun]] = {}
    for r in runs:
        groups.setdefault((r.trace_id, r.revision_ms), []).append(r)
    return list(groups.values())


class JsonlSink:
    """Offline output: one canonical run per line (findings inside), and the params sidecar
    `<out>.params.jsonl` that resolves every detector_version."""

    def __init__(self, out: Path) -> None:
        self.out = out
        self.params_out = out.with_name(out.name + ".params.jsonl")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("", encoding="utf-8")

    def register(self, detectors: Sequence[EnabledDetector]) -> None:
        lines = [json.dumps(params_record(d), sort_keys=True) for d in detectors]
        self.params_out.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")

    def write(self, runs: Sequence[DetectorRun]) -> None:
        with self.out.open("a", encoding="utf-8") as fh:
            for r in runs:
                fh.write(json.dumps(r.canonical(), sort_keys=True, separators=(",", ":")) + "\n")
