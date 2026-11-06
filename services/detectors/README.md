# traceblame-detectors

Plugin detectors that turn an assembled trace into versioned findings (design §1.1 component 6,
#15). The same engine runs online, per `trace.completed` event, and offline, over a frozen
snapshot for calibration (#17) and the eval reports (#19).

## The plugin contract

A detector is a class in `src/traceblame_detectors/plugins/<name>/detector.py`, registered in
`plugins/__init__.py` (an explicit allowlist, no entry points) and pinned in
`plugins/versions.lock.json`. It imports only from `traceblame_detectors.api`:

```python
@dataclass(frozen=True)
class Params:  # behaviour params only: thresholds, windows, switches
    min_score: float = 0.35


class RetrievalMiss(Detector):
    spec = DetectorSpec(
        name="retrieval_miss",
        code_version="1.0.0",
        component=Component.RETRIEVAL,
        reads=Reads(
            span_kinds=frozenset({SpanKind.RETRIEVAL, SpanKind.LLM}),
            attrs=frozenset({"aegis.output.cited_doc_ids"}),
        ),
        params_type=Params,
    )

    def detect(self, trace: TraceView, ctx: DetectorContext) -> Sequence[FindingDraft]: ...
```

- **Input.** A `TraceView` (one `trace_summary` row + the trace's `spans`), projected to the
  declared span kinds; `span.attr(key)` answers only declared keys. Declarable keys are the
  profile's span attributes without a column, minus the denylist in `policy.py`: eval and fault
  markers, `exception.*`, content, correlation ids. The sources never load anything else:
  `resource_attrs`, tenant, session, decision and content references are not even read.
- **Context.** Read-only, by declaration (`needs`): `SERVICE_BASELINE` (a baseline as of the
  trace's start, computed from production data, never labels) and `EVAL_GOLDEN` (eval_only
  detectors, eval-mode offline runs only). No storage access.
- **Output.** `FindingDraft(span_id, severity, score, evidence)`: one per span at most, score
  in [0, 1] (quantized to 1e-4), at most 64 per run. Evidence strings must be ids from the
  detector's own view (span ids, doc ids, tool names, models, finish reasons, args hashes,
  bundle digests, providers), id-typed strings its own context returned
  (`ServiceBaseline.bundle_digest`; golden doc ids, eval_only detectors only), or values of its
  declared `evidence_enums`. Anything else, outcome names and values included, fails the run
  (`evidence_rejected`). Never content.
- **Failures.** A detector that raises or breaks the contract fails its own run
  (`status=error`, `error_kind` = a fixed reason or the exception class name, never its message).

The static guard (`tests/test_static_guard.py`) allows plugins a few pure standard modules and
bans `open`, `eval`, `exec`, `__import__`, `getattr` and friends. It is a tripwire, not a
sandbox: Python can always escape a static check. The real boundary is what the trace view and
the context hand over.

## Versions and idempotency

- `detector_version = <code semver>+p<first 8 hex of sha256(canonical params)>`. Operational
  settings (`worker:`, `offline:`) never enter it. Every version is recorded with its params in
  `detector_params` (or the offline `.params.jsonl` sidecar); a writer refuses a version
  registered with other params.
- `FRAMEWORK_VERSION` (engine, view, canonical form, policy) is recorded on every run.
  `framework.lock.json` pins those modules' sources per version, `plugins/versions.lock.json`
  each plugin's per code version, `vectors/v1.json` the version strings, finding ids and
  canonical bytes; all are append-only. After a deliberate framework change: bump
  `FRAMEWORK_VERSION`, then add its lock entry with
  `uv run --frozen python -c "from traceblame_detectors.locks import framework_sources; print(framework_sources())"`.
- Findings are idempotent by `(trace_id, detector, detector_version)` per trace revision:
  `finding_id = uuid5(trace_id/detector/detector_version/span_id/revision/run_digest)`, where
  `run_digest` = sha256 of `{input_digest, context_digest, detector_version, framework_version}`.
  Per trace, the findings are written first, then one `detector_runs` row per detector (the
  commit marker, also for a run that found nothing). The current findings of a detector version
  are those whose `(revision, run_digest)` equal its newest `detector_runs` row, if that run is
  ok: a re-assembled trace's run replaces the earlier one whatever the insert order, a rerun
  with other context selects only its own rows, and a failed run selects none
  (`tests/integration/detectors_findings_current_read.sql`).
- `detector_runs.findings_digest`, `input_digest` and `run_digest` make runs comparable byte
  for byte (findings.score is Float32, so read-back rows are not).

## Online and offline

- **Online** (`online.handle`): production config only, no golden provider, ClickHouse
  source and sink. PR-B wraps it in the JetStream worker (ack after the writes).
- **Offline** (`traceblame-detectors run-offline`): the same view builder and engine over a
  snapshot. It never imports a publisher. `mode: eval` (eval_only detectors, golden ids) writes
  JSON lines only, never ClickHouse.

```
uv run --frozen traceblame-detectors snapshot-export --clickhouse-url URL \
    --assembler-version V --trace-ids-file ids.txt --out DIR [--postgres-url URL]
uv run --frozen traceblame-detectors run-offline --snapshot DIR --config detectors.yaml \
    --out runs.jsonl [--baselines baselines.json]
uv run --frozen traceblame-detectors versions --config detectors.yaml
```

### Snapshot format (`traceblame-detectors-snapshot/1`)

| File | Content |
|---|---|
| `manifest.json` | format, export time, commit and dirty flag, applied ClickHouse (and Postgres) migrations, profile and assembler versions, sha256 of the contract reads used, attr keys, trace and span counts, sha256 of every other file |
| `trace_summary.jsonl` | one `trace_summary_read.sql` row per trace |
| `spans.jsonl` | `spans_trace_view_read.sql` rows plus `trace_id`, attrs filtered to the declarable keys |
| `golden.jsonl` | optional, eval only: `{"trace_id", "golden_doc_ids"}` and nothing else |

No labels are ever written: no `injections`, no `eval_traces` column but `golden_doc_ids`, no
marker. The source verifies every file against the manifest and refuses unknown files.

**TTL.** `spans` and `trace_summary` expire 30 days after a trace's `start_ts` (migrations
0001, 0014). Export a dataset before then: D1 collected on 2026-11-06 expires around
2026-12-06.

## Tests

- `services/detectors/tests`: unit tests; `tests/plugins/` holds the test-only reference
  detectors (not registered in production).
- `tests/contract/test_detectors_contract.py`: policy against the profile and the spans view,
  embedded SQL copies, append-only locks and vectors.
- `tests/integration/test_detectors_live.py`: migrations, statements, supersession, and the
  golden fixtures through the built collector and assembler, online vs offline byte-identical.
- `tests/scripts/test_detectors_kind_scan.py`: the value scan on the newest fault run in kind
  (read-only, takes the kind lock itself).
