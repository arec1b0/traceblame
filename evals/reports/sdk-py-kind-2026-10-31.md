# traceblame-py through kind: agentic-rag on CUAD (2026-10-31)

`make fixtures-agentic-rag` (#12): the agentic-rag app, instrumented with traceblame-py,
answers real CUAD questions with qwen2.5:7b and exports through the pinned collector to
the kind agent and gateway (ADR-0005, ADR-0006). Machine-readable: the `.json` next to
this file.

- traceblame `3a8545b20db6`, app `0e3780d1f4fa`
- Canary preflight: 8 spans, steps ['retriever', 'calculator', 'final_answer'], no problems

## Run B: eval tenant, every marked trace kept

`rag-cuad-20261031-s7-3a8545b-b` as `tenant-eval`.

| Scenario | Queries | Traces | Spans | Outcomes |
|---|---|---|---|---|
| answerable | 12 | 12 | 102 | {'answered': 11, 'empty_answer': 1} |
| unanswerable | 12 | 12 | 99 | {'answered': 12} |

Spans sent 201, rows in ClickHouse 201 (the same span ids are required).

## Run A: tenant not in eval_tenants

`rag-cuad-20261031-s7-3a8545b-a` as `tenant-a`: 4 queries, outcomes {'answered': 4}.

- Marked resource blocks sent: 19; `traceblame_normalizer_eval_marker_rejected_total{tenant="tenant-a",attr="aegis.eval.run.id",reason="tenant_not_allowed"}` rose by 19 on the agents and 0 on the gateways.
- Spans sent 39; stored 39 in 4 complete traces (10% sampling once the marker is dropped), none with the marker.

## Content

No query text and no 48-character window of any chunk of the 24 queried contracts in the exported files or the stored rows: passed.

## Result

All checks passed.
