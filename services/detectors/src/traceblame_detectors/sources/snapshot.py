"""Trace views from a frozen snapshot directory (#15; #19 exports D1 into this format).

```
<dir>/manifest.json        format, provenance and the sha256 of every other file
<dir>/trace_summary.jsonl  one trace_summary_read.sql row per trace (column -> value)
<dir>/spans.jsonl          spans_trace_view_read.sql rows plus trace_id
<dir>/golden.jsonl         optional, eval only: {"trace_id", "golden_doc_ids"}
```

The snapshot never holds labels: no injections, no eval_traces columns but golden_doc_ids, no
eval or fault markers (attrs are exported filtered to the declarable keys). The source checks
every file against the manifest before it reads anything.
"""

import json
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any, cast

from traceblame_detectors.canonical import sha256_hex
from traceblame_detectors.view import TraceView, build_view

FORMAT = "traceblame-detectors-snapshot/1"
MANIFEST = "manifest.json"
SUMMARY_FILE = "trace_summary.jsonl"
SPANS_FILE = "spans.jsonl"
GOLDEN_FILE = "golden.jsonl"
ALLOWED_FILES = frozenset({MANIFEST, SUMMARY_FILE, SPANS_FILE, GOLDEN_FILE})


class SnapshotError(ValueError):
    pass


def _jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield cast("dict[str, Any]", json.loads(line))


class SnapshotTraceSource:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.manifest = self._verified_manifest()
        self._summaries = {str(r["trace_id"]): r for r in _jsonl(directory / SUMMARY_FILE)}
        spans: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in _jsonl(directory / SPANS_FILE):
            spans[str(row.pop("trace_id"))].append(row)
        self._spans = dict(spans)
        if int(self.manifest["trace_count"]) != len(self._summaries):
            raise SnapshotError("trace_count in the manifest differs from trace_summary.jsonl")

    def _verified_manifest(self) -> Mapping[str, Any]:
        path = self.directory / MANIFEST
        if not path.is_file():
            raise SnapshotError(f"{path}: no manifest")
        manifest = cast("dict[str, Any]", json.loads(path.read_text(encoding="utf-8")))
        if manifest.get("format") != FORMAT:
            raise SnapshotError(f"{path}: format is not {FORMAT}")
        files = cast("dict[str, str]", manifest.get("files") or {})
        present = {p.name for p in self.directory.iterdir()}
        if present - ALLOWED_FILES:
            raise SnapshotError(
                f"unexpected files in the snapshot: {sorted(present - ALLOWED_FILES)}"
            )
        if present - {MANIFEST} != set(files):
            raise SnapshotError("the manifest's file list differs from the directory")
        for name, sha in files.items():
            if sha256_hex((self.directory / name).read_bytes()) != sha:
                raise SnapshotError(f"{name}: sha256 differs from the manifest")
        for required in (SUMMARY_FILE, SPANS_FILE):
            if required not in files:
                raise SnapshotError(f"{required} is missing")
        return manifest

    def trace_ids(self) -> list[str]:
        return sorted(self._summaries)

    def load(self, trace_id: str, attr_keys: Iterable[str]) -> TraceView:
        return build_view(self._summaries[trace_id], self._spans.get(trace_id, []), attr_keys)

    def golden_rows(self) -> Iterator[dict[str, Any]]:
        path = self.directory / GOLDEN_FILE
        if path.is_file():
            yield from _jsonl(path)
