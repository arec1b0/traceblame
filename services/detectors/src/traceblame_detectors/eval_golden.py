"""Eval-only context: golden chunk ids of a trace's source query (#15, rule 9).

Read only from a snapshot's golden.jsonl (eval_traces.golden_doc_ids, nothing else of
eval_traces), only in an eval-mode offline run, and handed only to eval_only detectors (the
engine never gives it to another). The online path never imports this module.
"""

from traceblame_detectors.sources.snapshot import SnapshotTraceSource


class SnapshotGoldenProvider:
    def __init__(self, snapshot: SnapshotTraceSource) -> None:
        self._golden: dict[str, tuple[str, ...]] = {}
        for row in snapshot.golden_rows():
            extra = set(row) - {"trace_id", "golden_doc_ids"}
            if extra:
                raise ValueError(f"golden.jsonl holds more than golden ids: {sorted(extra)}")
            self._golden[str(row["trace_id"])] = tuple(str(x) for x in row["golden_doc_ids"])

    def golden_doc_ids(self, trace_id: str) -> tuple[str, ...] | None:
        return self._golden.get(trace_id)
