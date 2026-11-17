"""The process that tests/scripts/test_faults_resume.py kills (#70): a driver that opens an
attempt of a run through traceblame_faults.runner, and a fake agent that runs the attempt's
entries. Not a test file.

The agent is fake only in what it computes: per entry it makes a real OpenTelemetry trace with
the eval resource attributes (a root span and a child span, the child marked with the entry's
injection id), exports every span when it ends to a JSON-lines span file (the stand-in for the
store, fsynced, so a SIGKILL leaves exactly the spans that had ended), logs entry_start and the
trace id, and writes the entry's ledger record once the root span has ended.

  driver --run-dir D --plan P --spans F [--provenance JSON] [--child] [--block POINT:ENTRY
         --ready FILE --fifo FIFO]
      POINT: before-first (after attempt_start), mid-trace (child span ended, root open),
      after-record (the entry's ledger line written). At the point the process touches FILE and
      blocks reading FIFO, until it is killed or the FIFO gets a writer.
      --child: the entries run in a child process that inherits the run lock, as the
      agentic-rag app does under its driver.
"""

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult

from traceblame_faults import kinds, ledger, plan, runner

RUN = "faults-smoke-20261117-s7-abc1234"
MARKER = "aegis.fault.injection.id"


class FileExporter(SpanExporter):
    """One JSON line per ended span, durably: what a kill leaves behind is what had ended."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        fd = os.open(self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            for span in spans:
                context = span.get_span_context()
                assert context is not None
                doc = {
                    "trace_id": format(context.trace_id, "032x"),
                    "span_id": format(context.span_id, "016x"),
                    "name": span.name,
                    "start": (span.start_time or 0) / 1e9,
                    "attrs": dict(span.attributes or {}),
                    "resource": dict(span.resource.attributes),
                }
                os.write(fd, (json.dumps(doc, sort_keys=True) + "\n").encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None


class Block:
    """--block POINT:ENTRY: touch the ready file and wait on the FIFO at that point."""

    def __init__(self, spec: str | None, ready: str | None, fifo: str | None) -> None:
        self.point, _, self.entry = (spec or ":").partition(":")
        self._ready, self._fifo = ready, fifo

    def at(self, point: str, entry_id: str = "") -> None:
        if point != self.point or (self.entry and entry_id != self.entry):
            return
        assert self._ready is not None and self._fifo is not None  # noqa: PT018
        Path(self._ready).write_text(f"{point}:{entry_id}\n", encoding="utf-8")
        with open(self._fifo, encoding="utf-8") as fifo:  # blocks until a writer opens it
            fifo.read()


def _record(entry: plan.Entry, trace_id: str) -> ledger.Record:
    base = ledger.Record(
        trace_id=trace_id,
        eval_run_id=RUN,
        service=entry.app,
        source="cuad",
        query_ref=entry.query_ref,
        stratum="answerable",
        golden_doc_ids=(),
        golden_status="unmapped",
        planned_fault_kind=entry.kind,
        injection_id="",
        fault_kind="",
        target_component="",
        seed=entry.seed,
    )
    if not entry.kind:
        return base
    return replace(
        base,
        injection_id=entry.injection_id,
        fault_kind=entry.kind,
        target_component=kinds.KINDS[entry.kind].component,
        params=dict(entry.params),
        injected_at=ledger.now_iso(datetime.now(UTC)),
    )


def agent(args: argparse.Namespace) -> int:
    """Every remaining entry of the attempt, one real trace each."""
    entries = plan.loads(Path(args.plan).read_text(encoding="utf-8"))
    block = Block(args.block, args.ready, args.fifo)
    provider = TracerProvider(
        resource=Resource.create({"service.name": "agentic-rag", "aegis.eval.run.id": RUN})
    )
    provider.add_span_processor(SimpleSpanProcessor(FileExporter(Path(args.spans))))
    tracer = provider.get_tracer("faults-resume-agent")
    log = runner.EntryLog(Path(args.run_dir), args.attempt_id, RUN)

    def run_one(entry: plan.Entry, log: runner.EntryLog) -> ledger.Record:
        with tracer.start_as_current_span("agent") as root:
            trace_id = format(root.get_span_context().trace_id, "032x")
            log.trace(entry, trace_id)
            with tracer.start_as_current_span("llm") as child:
                if entry.kind:
                    child.set_attribute(MARKER, entry.injection_id)
            block.at("mid-trace", entry.entry_id)  # the child span is stored, the root is open
        return _record(entry, trace_id)

    for entry in log.remaining(entries):
        log.start(entry)
        log.record(entry, run_one(entry, log))
        block.at("after-record", entry.entry_id)
    provider.shutdown()
    return 0


def driver(args: argparse.Namespace) -> int:
    entries = plan.loads(Path(args.plan).read_text(encoding="utf-8"))
    try:
        attempt = runner.open_attempt(
            Path(args.run_dir),
            entries,
            eval_run_id=RUN,
            provenance=cast("dict[str, object]", json.loads(args.provenance)),
        )
    except runner.RunnerError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3
    print(json.dumps({"attempt": attempt.id, "remaining": len(attempt.remaining)}), flush=True)
    args.attempt_id = attempt.id
    Block(args.block, args.ready, args.fifo).at("before-first")
    if args.child:
        argv = [sys.executable, __file__, "agent", "--run-dir", args.run_dir, "--plan", args.plan,
                "--spans", args.spans, "--attempt-id", attempt.id]  # fmt: skip
        for name in ("block", "ready", "fifo"):
            if getattr(args, name):
                argv += [f"--{name}", getattr(args, name)]
        code = subprocess.run(argv, pass_fds=(attempt.lock_fd,), check=False).returncode  # noqa: S603
    else:
        code = agent(args)
    attempt.end("completed" if code == 0 else f"failed: exit {code}")
    return code


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["driver", "agent"])
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--spans", required=True)
    parser.add_argument("--provenance", default='{"commit": "abc1234"}')
    parser.add_argument("--attempt-id")
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--block")
    parser.add_argument("--ready")
    parser.add_argument("--fifo")
    args = parser.parse_args()
    return driver(args) if args.command == "driver" else agent(args)


if __name__ == "__main__":
    raise SystemExit(main())
