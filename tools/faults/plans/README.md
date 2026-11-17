# Fault run plans

Committed plan files of `make faults-d1 PLAN=<name>` (#70). A run is created from a plan file
and resumed with its `RUN_ID`; nothing here is edited before a run. `../validation.yaml` is the
#14 validation run's plan and stays where it is.

| File | What |
|---|---|
| `d1.yaml` | D1 (#19): 450 faulted + 150 clean traces over both apps |
| `smoke.yaml` | the resume smoke: every kind once, for a kill and resume on kind |
| `d1.queries.jsonl` | every D1 source query: `app`, `query_ref`, `split`, `kinds`. Ids only |
| `d1.queries.lock.json` | what the list was generated from, and its sha256 |
| `validation.queries.jsonl` | every candidate query of the #14 validation run; D1 excludes them |

## The D1 split

- **By source query** (design §1.6): all traces of a query are on one side. The grouping key is
  the CUAD question id for agentic-rag, and the task's (scenario, parameters) tuple for
  runbook-agent (two tasks with equal parameters are one query, whatever their names).
- **50/50 per app**, and for every fault kind the number of its queries on the test side is
  within `split.max_kind_deviation` (2) of half. `traceblame_faults.split` pairs queries within
  a stratum (the set of kinds that drew the query; scenario and kind for runbook tasks) and
  refuses a split outside these bounds.
- **Committed before any trace exists.** `d1.queries.jsonl` is generated once and committed;
  `prepare` / `build` recompute it and refuse a difference.
- **Disjoint from validation.** A different seed alone is not disjoint (a D1 sample shares
  questions with validation's candidates, and seeded runbook tasks repeat parameter tuples), so
  D1 excludes `validation.queries.jsonl` by list: CUAD by question id, runbook tasks by tuple.
- **CUAD contracts** are not grouped: questions of one contract can be on both sides. The
  report states how many contracts are.

**For #19.** Report query-cluster bootstrap 95% confidence intervals (resample source queries,
not traces: a CUAD query carries up to seven traces), and flag every cell with n < 30 as
indicative. The runbook-agent kinds have 12-13 test queries each.

## Regenerating the lists

```bash
uv run --frozen python tools/faults/d1_queries.py generate --app-dir ../agentic-rag
```

```bash
uv run --frozen python tools/faults/d1_queries.py check --app-dir ../agentic-rag
```

Needs the agentic-rag checkout (its chunker decides which questions have mapped golden chunks)
and the capture environment; no kind, no model server. `check` regenerates into a temporary
directory and compares bytes. The lock records the CUAD commit and archive sha256, the
agentic-rag commit, the commit of this repository the generator ran at, and the sha256 of
`d1.yaml` and of both lists, so the same inputs give the same bytes.
