# traceblame-faults

Fault injection with ground-truth labels and seeds (#14, ADR-0008). Six kinds, injected into
real runs of the real apps (agentic-rag on CUAD, runbook-agent in the kind sandbox), with the
label written to ClickHouse and the marker `aegis.fault.injection.id` on the span the fault was
injected at.

| Kind | Ground truth | Apps |
|---|---|---|
| `golden_context_removed` | retrieval | agentic-rag |
| `tool_failure` | tool | agentic-rag |
| `malformed_output` | model | agentic-rag, runbook-agent |
| `output_budget_exhausted` | orchestration | agentic-rag |
| `step_memory_loss` | orchestration | agentic-rag |
| `prompt_variant` | prompt | agentic-rag, runbook-agent |

## Layout

- `src/traceblame_faults/`:
  - `kinds.py`: the kinds and their closed parameter schemas; no text can reach
    `injections.params`.
  - `scenarios/*.yaml`: one scenario per (kind, app): generators for each parameter,
    eligibility, prompt variants (`scenarios/prompts/`).
  - `plan.py`, `ids.py`: the seeded plan, injection ids and request seeds.
  - `guard.py`: faults fire only with `TRACEBLAME_FAULTS=1` and an eval tenant.
  - `mutate.py`, `retrieval.py`: the rewrites at the LLM seam and the store wrappers.
  - `golden.py`, `bundle.py`: golden chunks of a CUAD answer; the prompt-variant
    `aegis.bundle.digest`.
  - `ledger.py`, `record.py`: the in-process ledger, and the ledger into `eval_traces` and
    `injections` (the `store` extra).
  - `appendlog.py`, `attempts.py`, `runner.py`: a resumable run (#70, below).
  - `validate.py`, `dataset.py`: what must hold after a run, and the one read of ground truth.
  - `planfile.py`, `querylist.py`, `split.py`: committed plan files, a plan's committed query
    list, and the calibration / test split by source query.
- `preload/faults-preload.mjs`: the same rewrites for runbook-agent, as a Node preload in the
  eval image only.
- `vectors/mutations.json`: golden vectors that both languages run.

The drivers that run the apps are in `tools/faults/` (`make faults-agentic-rag`,
`make faults-runbook-agent`, `make faults-report`, `make faults-d1`).

## Resumable runs (#70)

`make faults-d1 PLAN=d1|smoke` creates a run from a committed plan file
(`tools/faults/plans/`); `make faults-d1 RUN_ID=<id>` resumes it after a kill.

- **What is done.** An entry is complete only by its line in `ledger.jsonl`, written after the
  trace has ended (and, in agentic-rag, after its spans are flushed). The ledger is append-only:
  one `write` on `O_APPEND`, then `fsync`; it is never unlinked or rewritten. A second record
  for an entry is refused.
- **Attempts.** `attempts.jsonl` (same durability) has `attempt_start`, `entry_start`, the
  trace id as soon as it is known, `attempt_end`. One attempt at a time: the attempt holds
  `flock` on `run.lock`, and the app subprocess inherits it, so a driver killed alone blocks a
  resume until its child is gone.
- **Resume.** An attempt without an end is recorded as `attempt_killed`. A torn last ledger
  line is copied to `ledger.torn.<attempt>` and cut off. Every entry that was started and has
  no record runs again under a new trace id; the trace ids known for it are recorded as
  `abandoned`. Finished entries are skipped.
- **Abandoned traces** go to `eval_exclusions`, never to `eval_traces` or `injections`, and
  nothing is deleted from `spans`. Every reader of ground truth starts from `eval_traces`, so
  they are excluded by construction; `dataset.read` is the one label read, and it joins
  `injections` on `(trace_id, injection_id)`.
- **Provenance.** A resume compares both checkouts' commit and dirty flag, every role's image
  ID, the eval image's config digest, both databases' applied migrations, the plan, the
  preload, this package's files, and the model's digest and server version with the run's
  first attempt. A difference is named and refused; the remedy is a new run id.
- **Check.** `validate.validate`: one record, one `eval_traces` row and one injection per
  entry; per injection id one completed trace; no stored trace of the run that is neither
  completed nor abandoned (the orphan sweep attributes a trace to an entry in flight, or
  fails); zero checked fails.
- **Progress.** `make faults-progress RUN_ID=<id>` (read-only).

## CLI

```bash
uv run traceblame-faults scenarios check
```

```bash
uv run traceblame-faults plan --app agentic-rag --queries queries.jsonl --seed 7 --eval-run-id faults-x --count tool_failure=4 --clean 4 --out plan.jsonl
```

```bash
uv run traceblame-faults record --ledger ledger.jsonl --attempts attempts.jsonl --url http://user:pass@127.0.0.1:8123/traceblame
```

```bash
uv run traceblame-faults dataset trace-ids --eval-run-id faults-d1-x --url http://user:pass@127.0.0.1:8123/traceblame --out trace-ids.txt
```
