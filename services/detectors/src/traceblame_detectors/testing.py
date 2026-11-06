"""Test support: load a detector from a file, a sample trace, and an in-memory stand-in for the
ClickHouse statements the detectors run (unit tests; the integration tests use a real server).

The sample trace is shaped like an agentic-rag run (agent, retrieval, a failed tool call, an LLM
call whose output did not parse) and carries what a source must never pass on: the eval and
fault markers, an exception message, resource attributes and the tenant."""

import hashlib
import importlib.util
import inspect
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from traceblame_detectors.api import Detector
from traceblame_detectors.sql_statements import statement
from traceblame_detectors.view import SPAN_COLUMNS, SUMMARY_COLUMNS

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
ROOT = "00f067aa0ba902b7"
RUN_ID = "faults-2026-11-05-s7-9f5013b"
INJECTION_ID = "faults-2026-11-05-s7-9f5013b:tool_failure:0123456789ab"
INJECTED_TEXT = "vector store unavailable (injected)"


def summary_row(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "trace_id": TRACE_ID,
        "assembler_version": "1.0.0",
        "revision_ms": 1_793_700_000_123,
        "service": "agentic-rag",
        "root_span_id": ROOT,
        "root_status_code": "Unset",
        "start_us": 1_793_699_990_000_000,
        "duration_ms": 8123.5,
        "span_count": 4,
        "error_span_count": 1,
        "bundle_digest": "sha256:" + "a" * 64,
    }
    row.update(over)
    return row


def _span(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "span_id": ROOT,
        "parent_span_id": "",
        "service": "agentic-rag",
        "operation": "invoke_agent",
        "span_kind": "agent",
        "start_us": 1_793_699_990_000_000,
        "duration_ms": 8123.5,
        "status_code": "Unset",
        "provider": "",
        "model": "",
        "finish_reasons": [],
        "input_tokens": 0,
        "output_tokens": 0,
        "tool_name": "",
        "args_hash": "",
        "retrieval_scores": [],
        "retrieved_doc_ids": [],
        "bundle_digest": "sha256:" + "a" * 64,
        "error_type": "",
        "http_status_code": 0,
        "source_sdk": "openinference",
        "profile_version": "1.6.0",
        "attrs": {},
    }
    row.update(over)
    return row


def span_rows() -> list[dict[str, Any]]:
    """Rows as a source returns them, plus columns and attrs a source must never pass on."""
    return [
        _span(resource_attrs={"aegis.eval.run.id": RUN_ID}, tenant_id="tenant-eval"),
        _span(
            span_id="1111111111111111",
            parent_span_id=ROOT,
            operation="retrieve",
            span_kind="retrieval",
            start_us=1_793_699_990_100_000,
            duration_ms=40.25,
            retrieval_scores=[0.8999999761581421, 0.5],
            retrieved_doc_ids=["cuad-0123456789ab-3", "cuad-0123456789ab-7"],
            attrs={"gen_ai.data_source.id": "cuad"},
        ),
        _span(
            span_id="2222222222222222",
            parent_span_id=ROOT,
            operation="execute_tool",
            span_kind="tool",
            start_us=1_793_699_990_200_000,
            duration_ms=3.5,
            status_code="Error",
            tool_name="search_contract",
            args_hash="0123456789abcdef",
            error_type="ConnectionError",
            attrs={
                "aegis.fault.injection.id": INJECTION_ID,
                "exception.message": INJECTED_TEXT,
            },
        ),
        _span(
            span_id="3333333333333333",
            parent_span_id=ROOT,
            operation="chat",
            span_kind="llm",
            start_us=1_793_699_990_300_000,
            duration_ms=2100.0,
            provider="openai",
            model="qwen2.5:7b",
            finish_reasons=["stop"],
            input_tokens=812,
            output_tokens=64,
            attrs={
                "aegis.output.parse_ok": "false",
                "aegis.output.cited_doc_ids": "[]",
                "gen_ai.response.model": "qwen2.5:7b",
            },
        ),
    ]


def load_detector(path: Path) -> type[Detector]:
    """The one Detector subclass defined in a plugin file."""
    name = f"plugin_{path.parent.name}_{hashlib.sha256(str(path).encode()).hexdigest()[:8]}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # inspect finds the source through it
    spec.loader.exec_module(module)
    found = [
        obj
        for _, obj in inspect.getmembers(module, inspect.isclass)
        if issubclass(obj, Detector) and obj is not Detector and obj.__module__ == module.__name__
    ]
    if len(found) != 1:
        raise ImportError(f"{path}: expected one detector, found {len(found)}")
    return found[0]


class FakeClickHouse:
    """Answers the detectors' statements from memory, as the server would (FINAL included)."""

    def __init__(
        self,
        summaries: Sequence[Mapping[str, Any]] = (),
        spans: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    ) -> None:
        self.summaries = [dict(s) for s in summaries]
        self.spans = {k: [dict(r) for r in v] for k, v in (spans or {}).items()}
        self.findings: list[dict[str, Any]] = []
        self.runs: list[dict[str, Any]] = []
        self.params: list[dict[str, Any]] = []
        self.commands: list[str] = []
        self._sql = {
            name: statement(name)
            for name in (
                "trace_summary_read.sql",
                "spans_trace_view_read.sql",
                "detectors_findings_write.sql",
                "detectors_runs_write.sql",
                "detectors_runs_read.sql",
                "detectors_params_write.sql",
                "detectors_params_read.sql",
            )
        }

    def _which(self, sql: str) -> str:
        for name, text in self._sql.items():
            if sql == text:
                return name
        if "schema_migrations" in sql:
            return "schema_migrations"
        raise AssertionError(f"unexpected statement: {sql[:80]}")

    def query_rows(
        self, sql: str, parameters: Mapping[str, Any] | None = None
    ) -> list[tuple[Any, ...]]:
        p = dict(parameters or {})
        match self._which(sql):
            case "trace_summary_read.sql":
                rows = [
                    s
                    for s in self.summaries
                    if s["trace_id"] == p["trace_id"]
                    and s["assembler_version"] == p["assembler_version"]
                ]
                rows.sort(key=lambda s: int(s["revision_ms"]))
                return [tuple(rows[-1][c] for c in SUMMARY_COLUMNS)] if rows else []
            case "spans_trace_view_read.sql":
                keys = set(p["attr_keys"])
                out: list[tuple[Any, ...]] = []
                for r in self.spans.get(p["trace_id"], []):
                    if int(r["start_us"]) < int(p["since_ms"]) * 1000:
                        continue
                    row = {**r, "attrs": {k: v for k, v in r["attrs"].items() if k in keys}}
                    out.append(tuple(row[c] for c in SPAN_COLUMNS))
                return out
            case "detectors_runs_read.sql":
                latest: dict[tuple[str, str], dict[str, Any]] = {}
                for r in self.runs:
                    if r["trace_id"] != p["trace_id"]:
                        continue
                    key = (r["detector"], r["detector_version"])
                    if key not in latest or r["revision_ms"] >= latest[key]["revision_ms"]:
                        latest[key] = r
                return [
                    (
                        r["detector"],
                        r["detector_version"],
                        r["revision_ms"],
                        r["input_digest"],
                        r["context_digest"],
                        r["framework_version"],
                        r["status"],
                        r["error_kind"],
                        r["finding_count"],
                        r["findings_digest"],
                        r["run_digest"],
                    )
                    for r in latest.values()
                ]
            case "detectors_params_read.sql":
                return [
                    (
                        r["detector"],
                        r["detector_version"],
                        r["code_version"],
                        r["framework_version"],
                        r["params"],
                    )
                    for r in self.params
                    if r["detector_version"] in p["detector_versions"]
                ]
            case _:
                return []

    def command(self, sql: str, parameters: Mapping[str, Any] | None = None) -> object:
        p = dict(parameters or {})
        name = self._which(sql)
        self.commands.append(name)
        if name == "detectors_findings_write.sql":
            for i, fid in enumerate(p["finding_ids"]):
                self.findings.append(
                    {
                        "finding_id": fid,
                        "trace_id": p["trace_id"],
                        "revision_ms": p["revision_ms"],
                        **{
                            col: p[arr][i]
                            for col, arr in (
                                ("span_id", "span_ids"),
                                ("detector", "detectors"),
                                ("detector_version", "detector_versions"),
                                ("input_digest", "input_digests"),
                                ("run_digest", "run_digests"),
                                ("component", "components"),
                                ("severity", "severities"),
                                ("score", "scores"),
                                ("details", "details"),
                            )
                        },
                    }
                )
        elif name == "detectors_runs_write.sql":
            for i, det in enumerate(p["detectors"]):
                self.runs.append(
                    {
                        "trace_id": p["trace_id"],
                        "revision_ms": p["revision_ms"],
                        "assembler_version": p["assembler_version"],
                        "framework_version": p["framework_version"],
                        "detector": det,
                        **{
                            col: p[arr][i]
                            for col, arr in (
                                ("detector_version", "detector_versions"),
                                ("input_digest", "input_digests"),
                                ("run_digest", "run_digests"),
                                ("context_digest", "context_digests"),
                                ("status", "statuses"),
                                ("error_kind", "error_kinds"),
                                ("finding_count", "finding_counts"),
                                ("findings_digest", "findings_digests"),
                            )
                        },
                    }
                )
        elif name == "detectors_params_write.sql":
            self.params.append(p)
        return None
