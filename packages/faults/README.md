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
- `preload/faults-preload.mjs`: the same rewrites for runbook-agent, as a Node preload in the
  eval image only.
- `vectors/mutations.json`: golden vectors that both languages run.

The drivers that run the apps are in `tools/faults/` (`make faults-agentic-rag`,
`make faults-runbook-agent`, `make faults-report`).

## CLI

```bash
uv run traceblame-faults scenarios check
```

```bash
uv run traceblame-faults plan --app agentic-rag --queries queries.jsonl --seed 7 --eval-run-id faults-x --count tool_failure=4 --clean 4 --out plan.jsonl
```

```bash
uv run traceblame-faults record --ledger ledger.jsonl --url http://user:pass@127.0.0.1:8123/traceblame
```
