# Fault injection through kind: `smoke` run (2026-11-17)

`tools/faults/` (#14): the six fault kinds injected into real runs of agentic-rag
(CUAD) and runbook-agent (sandbox alerts), exported through kind as `tenant-eval`.
Per kind: the marker survives on the intended span, the injections row joins it, the
signal the #16 detector reads moved against a clean trace, and the trace reaches
`trace.completed`. Machine-readable: the `.json` next to this file.

## agentic-rag

- run `faults-smoke-20261117-s7-31e6064`, traceblame `31e60647721a`, app `5370b067fdd1`
- plan sha256 `0538f2aa61bad5a2`, 7 entries, seed 7; ledger sha256 `eff5cb1c48992c38`, 7 records (the counts below are re-derived from it)
- spans sent 126, stored 126; baseline digest `sha256:3d5cd7f97e85e17b`
- content check: 0 leaking. Needles: 1 sampled CUAD questions: each question's text and every 48-character window (step 16) of every chunk of its contract (403 chunks, 9013 windows). Scanned: every stored row of the run's traces in every table with a trace_id, 200 rows (blame 0, detector_runs 0, eval_exclusions 2, eval_traces 7, findings 0, injections 6, spans 127, trace_arrivals 51, trace_summary 7); the 127 exported spans; the run's files ledger.jsonl (7 lines), plan.jsonl (7 lines), queries.jsonl (1 line), state.json (1496 lines), attempts.jsonl (28 lines), outcomes.a001-20261117T210052Z.jsonl (2 lines), outcomes.a002-20261117T210431Z.jsonl (5 lines), otlp.a001-20261117T210052Z.jsonl (14 lines), otlp.a002-20261117T210431Z.jsonl (50 lines), attempt.a002-20261117T210431Z.json (230 lines), attempt.a003-20261117T212751Z.json (230 lines), app.a001-20261117T210052Z.log (27 lines), app.a002-20261117T210431Z.log (30 lines); left out: 29 values of query_ref, ref that are exactly one of the run's own dataset ids in its format (`<task>@s<seed>`, CUAD `<title>__<category>`; a runbook task id is also its workload's name, which the alert needles hold). Any other value in those fields is scanned
- host 1-minute load average per trace: in the tables below (16 CPUs)
- dirty flag at the build: traceblame false, app false
- applied migrations and the images kind ran, read at the start and the end of each phase (a change within a phase, or a read that found nothing, is a problem):

  | Phase | Start | End | Applied migrations (count, list hash) at the end | Changed within the phase | Problems |
  |---|---|---|---|---|---|
  | run | 2026-11-17T21:27:49Z | 2026-11-17T21:27:51Z | ClickHouse 19 (`57367744`), Postgres 2 (`2ebdd18e`) | none | 0 |
  | record | 2026-11-17T21:27:53Z | 2026-11-17T21:27:55Z | ClickHouse 19 (`57367744`), Postgres 2 (`2ebdd18e`) | none | 0 |
  | check | 2026-11-17T21:27:57Z | 2026-11-17T21:28:27Z | ClickHouse 19 (`57367744`), Postgres 2 (`2ebdd18e`) | none | 0 |

  ClickHouse at the end of the run phase: 0001 spans (expand, ec39fa3ba092, 2026-11-05T13:23:49Z); 0002 otel_traces_ingest (expand, 2bf4555ab291, 2026-11-05T13:23:49Z); 0003 spans_from_otel (expand, fffa026e8a2e, 2026-11-05T13:23:50Z); 0004 findings (expand, 6dcc9ca329c1, 2026-11-05T13:23:50Z); 0005 blame (expand, 500e7a24481c, 2026-11-05T13:23:50Z); 0006 injections (expand, ed6f0e72f1bb, 2026-11-05T13:23:50Z); 0007 trace_roots (expand, d10f004f53e2, 2026-11-05T13:23:50Z); 0008 trace_roots_from_otel (expand, a39825f34fac, 2026-11-05T13:23:51Z); 0009 trace_arrivals (expand, 06f38018e34f, 2026-11-05T13:23:51Z); 0010 trace_arrivals_from_otel (expand, e86b012f785a, 2026-11-05T13:23:51Z); 0011 drop_trace_roots_from_otel (contract, d91fa5370215, 2026-11-05T13:23:51Z); 0012 drop_trace_roots (contract, 6eef3c7d38f4, 2026-11-05T13:23:52Z); 0013 injections_injection_id (expand, 8e9c3e726c14, 2026-11-05T13:23:52Z); 0014 trace_summary (expand, e4ace319061a, 2026-11-05T13:23:52Z); 0015 eval_traces (expand, 6510d1f912cd, 2026-11-05T16:51:45Z); 0016 findings_revision (expand, d7b267093a99, 2026-11-17T20:37:51Z); 0017 detector_runs (expand, afd264215c30, 2026-11-17T20:37:51Z); 0018 detector_params (expand, 50695d6451fe, 2026-11-17T20:37:51Z); 0019 eval_exclusions (expand, 6c2131e11edd, 2026-11-17T20:37:52Z)

  Postgres: 0001 assembler_partitions (expand, 8ae1a3710e21, 2026-11-04T15:31:58Z); 0002 assembler_replicas (expand, 0328e6b9c1a0, 2026-11-04T17:25:03Z)

  | Role | Pods | Container | imageID |
  |---|---|---|---|
  | otel-agent | 2 | otelcol | `docker.io/library/import-2026-11-17@sha256:544dff4a1da44bf7c1406fde4eee81d3b1b6e07d6ae2fae27c53a8dd701c8814` |
  | otel-gateway | 2 | otelcol | `docker.io/library/import-2026-11-17@sha256:544dff4a1da44bf7c1406fde4eee81d3b1b6e07d6ae2fae27c53a8dd701c8814` |
  | trace-assembler | 2 | assembler | `docker.io/library/import-2026-11-17@sha256:fd04e3e40cbf9ef169c2dbfc0e676061c3dec7007abda2377c974aa0d24010e5` |
  | clickhouse | 1 | clickhouse | `docker.io/clickhouse/clickhouse-server@sha256:8b28eb09479c36c4533eec7a6268917d04ba3a6fd90e3f4205f70e761b6d7779` |
  | nats | 1 | nats | `docker.io/library/nats@sha256:127c5ccbc538a93f53ebb19461dc193130df386c2d6dab8199661a28a30d85bd` |
- golden chunks of the sampled answerable queries: {'mapped': 12, 'answerable_sampled': 12}

Resume (#70):

- checked: 7 plan entries, 7 ledger records, 7 `eval_traces` rows, 8 traces stored under the run's marker, 6 injection ids
- attempts: 3, killed: 1; entries in flight that ran again: 1; torn ledger tails: 0
- abandoned traces (in `eval_exclusions`, never in `eval_traces` or `injections`): 1; by kind: malformed_output 1; by reason: killed 1
- injection ids with `injections` rows on more than one trace: 0
- orphans (stored, neither completed nor abandoned): 0

| Attempt | Status | Started | Ended | Last event |
|---|---|---|---|---|
| a001-20261117T210052Z | killed | 2026-11-17T21:00:52.240Z | 2026-11-17T21:04:31.465Z | entry_trace agentic-rag:malformed_output:9465608b62ea |
| a002-20261117T210431Z | completed | 2026-11-17T21:04:31.486Z | 2026-11-17T21:13:41.228Z | entry_trace agentic-rag:tool_failure:9465608b62ea |
| a003-20261117T212751Z | completed | 2026-11-17T21:27:51.235Z | 2026-11-17T21:27:51.260Z | attempt_start |

| Abandoned trace | Entry | Kind | Attempt | Reason | Found in | Stored spans |
|---|---|---|---|---|---|---|
| `91714c396783ddf9c8f5b0bf9402f517` | agentic-rag:malformed_output:9465608b62ea | malformed_output | a001-20261117T210052Z | killed | attempt_log | 1 |

| Kind | Component | Traces | Applied | Not reached | Marker on its span | Joined | Signal moved | Assembled | Median s | p95 s | Load1 median |
|---|---|---|---|---|---|---|---|---|---|---|---|
| clean | - | 1 | 0 | 0 | 0 | 0 | 0 | 1 | 77.5 | 77.45 | 3.62 |
| golden_context_removed | retrieval | 1 | 1 | 0 | 1 | 1 | 1 | 1 | 52.1 | 52.11 | 2.63 |
| malformed_output | model | 1 | 1 | 0 | 1 | 1 | 1 | 1 | 71.8 | 71.85 | 5.99 |
| output_budget_exhausted | orchestration | 1 | 1 | 0 | 1 | 1 | 1 | 1 | 60.5 | 60.5 | 3.01 |
| prompt_variant | prompt | 1 | 1 | 0 | 1 | 1 | 1 | 1 | 159.5 | 159.48 | 3.58 |
| step_memory_loss | orchestration | 1 | 1 | 0 | 1 | 1 | 1 | 1 | 133.2 | 133.2 | 2.71 |
| tool_failure | tool | 1 | 1 | 0 | 1 | 1 | 1 | 1 | 87.2 | 87.2 | 1.22 |

Per trace (query = sha256 of the dataset id, first 12):

| Query | Kind | Status | Outcome | Load1 | Signal |
|---|---|---|---|---|---|
| 9465608b62ea | clean | clean | answered | 3.62 | {} |
| 9465608b62ea | golden_context_removed | applied | answered | 2.63 | moved {'golden_in_topk': [False, False], 'max_score': [0.1586613, -0.2219435], 'mode': 'distractor', 'counted': True} |
| 9465608b62ea | malformed_output | applied | answered | 5.99 | moved {'parse_ok': [['true'], ['false']]} |
| 9465608b62ea | output_budget_exhausted | applied | answered | 3.01 | moved {'finish_reasons': [['stop'], ['length']], 'parse_ok': ['false']} |
| 9465608b62ea | prompt_variant | applied | answered | 3.58 | moved {'bundle_digest': [['sha256:3d5cd7f97e85e17beae69c4e6a58b33d26823c78bf296251516e842ec5c86965'], ['sha256:99ff7808c1e62c8b797a7988fb76f3379d2c31716054bf8599751b10e89fc5e7']]} |
| 9465608b62ea | step_memory_loss | applied | answered | 2.71 | moved {'loop_max': [2, 7], 'repeat_min': 3} |
| 9465608b62ea | tool_failure | applied | answered | 1.22 | moved {'marked_tool_error': 1, 'consumed_by_llm': 1, 'retried': 0, 'error_type': ['ToolFailure']} |

## runbook-agent

- run `faults-smoke-20261117-s7-31e6064`, traceblame `31e60647721a`, app `92fb6f8fcbcf`
- plan sha256 `71680c2ce41e354c`, 3 entries, seed 7; ledger sha256 `044612c111e203ed`, 3 records (the counts below are re-derived from it)
- spans sent 9, stored 9; baseline digest `sha256:1b5b700607ed7cf7`
- content check: 0 leaking. Needles: 5 alert needles (32-character windows of each alert summary and the task-unique label values) and 153 40-character windows of the triage prompt and its variants. Scanned: every stored row of the run's traces in every table with a trace_id, 26 rows (blame 0, detector_runs 0, eval_exclusions 1, eval_traces 3, findings 0, injections 2, spans 12, trace_arrivals 4, trace_summary 4); the 12 exported spans; the run's files ledger.jsonl (3 lines), plan.jsonl (3 lines), queries.jsonl (3 lines), state.json (1135 lines), attempts.jsonl (15 lines), outcomes.a002-20261117T212321Z.jsonl (1 line), outcomes.a003-20261117T212907Z.jsonl (2 lines), otlp.a002-20261117T212321Z.salvaged.json (1 line), otlp.a003-20261117T212907Z.b0.json (1 line), otlp.a003-20261117T212907Z.b1.json (1 line), attempt.a001-20261117T211524Z.json (241 lines), attempt.a002-20261117T212321Z.json (9 lines), attempt.a003-20261117T212907Z.json (240 lines); left out: 15 values of query_ref, ref that are exactly one of the run's own dataset ids in its format (`<task>@s<seed>`, CUAD `<title>__<category>`; a runbook task id is also its workload's name, which the alert needles hold). Any other value in those fields is scanned
- host 1-minute load average per trace: in the tables below (16 CPUs)
- dirty flag at the build: traceblame false, app false
- applied migrations and the images kind ran, read at the start and the end of each phase (a change within a phase, or a read that found nothing, is a problem):

  | Phase | Start | End | Applied migrations (count, list hash) at the end | Changed within the phase | Problems |
  |---|---|---|---|---|---|
  | run | 2026-11-17T21:29:06Z | 2026-11-17T21:33:59Z | ClickHouse 19 (`57367744`), Postgres 2 (`2ebdd18e`) | none | 0 |
  | record | 2026-11-17T21:34:00Z | 2026-11-17T21:34:01Z | ClickHouse 19 (`57367744`), Postgres 2 (`2ebdd18e`) | none | 0 |
  | check | 2026-11-17T21:34:02Z | 2026-11-17T21:34:24Z | ClickHouse 19 (`57367744`), Postgres 2 (`2ebdd18e`) | none | 0 |

  ClickHouse at the end of the run phase: 0001 spans (expand, ec39fa3ba092, 2026-11-05T13:23:49Z); 0002 otel_traces_ingest (expand, 2bf4555ab291, 2026-11-05T13:23:49Z); 0003 spans_from_otel (expand, fffa026e8a2e, 2026-11-05T13:23:50Z); 0004 findings (expand, 6dcc9ca329c1, 2026-11-05T13:23:50Z); 0005 blame (expand, 500e7a24481c, 2026-11-05T13:23:50Z); 0006 injections (expand, ed6f0e72f1bb, 2026-11-05T13:23:50Z); 0007 trace_roots (expand, d10f004f53e2, 2026-11-05T13:23:50Z); 0008 trace_roots_from_otel (expand, a39825f34fac, 2026-11-05T13:23:51Z); 0009 trace_arrivals (expand, 06f38018e34f, 2026-11-05T13:23:51Z); 0010 trace_arrivals_from_otel (expand, e86b012f785a, 2026-11-05T13:23:51Z); 0011 drop_trace_roots_from_otel (contract, d91fa5370215, 2026-11-05T13:23:51Z); 0012 drop_trace_roots (contract, 6eef3c7d38f4, 2026-11-05T13:23:52Z); 0013 injections_injection_id (expand, 8e9c3e726c14, 2026-11-05T13:23:52Z); 0014 trace_summary (expand, e4ace319061a, 2026-11-05T13:23:52Z); 0015 eval_traces (expand, 6510d1f912cd, 2026-11-05T16:51:45Z); 0016 findings_revision (expand, d7b267093a99, 2026-11-17T20:37:51Z); 0017 detector_runs (expand, afd264215c30, 2026-11-17T20:37:51Z); 0018 detector_params (expand, 50695d6451fe, 2026-11-17T20:37:51Z); 0019 eval_exclusions (expand, 6c2131e11edd, 2026-11-17T20:37:52Z)

  Postgres: 0001 assembler_partitions (expand, 8ae1a3710e21, 2026-11-04T15:31:58Z); 0002 assembler_replicas (expand, 0328e6b9c1a0, 2026-11-04T17:25:03Z)

  | Role | Pods | Container | imageID |
  |---|---|---|---|
  | otel-agent | 2 | otelcol | `docker.io/library/import-2026-11-17@sha256:544dff4a1da44bf7c1406fde4eee81d3b1b6e07d6ae2fae27c53a8dd701c8814` |
  | otel-gateway | 2 | otelcol | `docker.io/library/import-2026-11-17@sha256:544dff4a1da44bf7c1406fde4eee81d3b1b6e07d6ae2fae27c53a8dd701c8814` |
  | trace-assembler | 2 | assembler | `docker.io/library/import-2026-11-17@sha256:fd04e3e40cbf9ef169c2dbfc0e676061c3dec7007abda2377c974aa0d24010e5` |
  | clickhouse | 1 | clickhouse | `docker.io/clickhouse/clickhouse-server@sha256:8b28eb09479c36c4533eec7a6268917d04ba3a6fd90e3f4205f70e761b6d7779` |
  | nats | 1 | nats | `docker.io/library/nats@sha256:127c5ccbc538a93f53ebb19461dc193130df386c2d6dab8199661a28a30d85bd` |

  Eval image of the run: `runbook-agent:92fb6f8-tb31e6064-faults`. Proof: the config digest built `sha256:bf046085e19d7176de738549aa8cf295a18f0b7f25cd6a13d3ac51eb62ab7e09` against the config digest the pods' nodes held for their image IDs (`crictl images`, matched on the pod's imageID) `sha256:bf046085e19d7176de738549aa8cf295a18f0b7f25cd6a13d3ac51eb62ab7e09`: equal. Additional signals: the pods ran the built tag and kept the imageID `docker.io/library/import-2026-11-17@sha256:20d95de9993adee4179a24a9a77f25044d506afbf3e900c6f2779001c29dd33e` across 3 samples (one per deploy and one at the end). For reference, the build's `eval_image_id` is `sha256:ac3987b41bd71df96330b3b6742ad766d9e152ec2c6360b7c5380d9c66477741`, an index digest that a node never reports (ADR-0009).
- image guard {'base_node_options': False, 'base_has_preload': False, 'eval_node_options': True, 'eval_has_preload': True, 'fetch_without_switch': 'fetch', 'fetch_other_tenant': 'fetch', 'fetch_eval_tenant': 'handle'}
- preload used by this run: sha256 `99165cf4425ea58a50cca9b0614f44d22217aca5e279ef7015438c3bc7c94bd4` (equal to the file at the run's commit)
- the committed preload is the same file

Resume (#70):

- checked: 3 plan entries, 3 ledger records, 3 `eval_traces` rows, 4 traces stored under the run's marker, 2 injection ids
- attempts: 3, killed: 1; entries in flight that ran again: 1; torn ledger tails: 0
- abandoned traces (in `eval_exclusions`, never in `eval_traces` or `injections`): 1; by kind: clean 1; by reason: killed 1
- injection ids with `injections` rows on more than one trace: 0
- orphans (stored, neither completed nor abandoned): 0

| Attempt | Status | Started | Ended | Last event |
|---|---|---|---|---|
| a001-20261117T211524Z | failed: SystemExit | 2026-11-17T21:15:24.989Z | 2026-11-17T21:21:34.739Z | attempt_start |
| a002-20261117T212321Z | killed | 2026-11-17T21:23:21.988Z | 2026-11-17T21:29:07.488Z | entry_start runbook-agent:clean:f6bf7d9ddd50 |
| a003-20261117T212907Z | completed | 2026-11-17T21:29:07.977Z | 2026-11-17T21:33:59.010Z | entry_trace runbook-agent:prompt_variant:6b0b52f1a142 |

| Abandoned trace | Entry | Kind | Attempt | Reason | Found in | Stored spans |
|---|---|---|---|---|---|---|
| `8134a2bd1eef483a79fec7ccbae2d1bb` | runbook-agent:clean:f6bf7d9ddd50 | clean | a002-20261117T212321Z | killed | preload | 3 |

| Kind | Component | Traces | Applied | Not reached | Marker on its span | Joined | Signal moved | Assembled | Median s | p95 s | Load1 median |
|---|---|---|---|---|---|---|---|---|---|---|---|
| clean | - | 1 | 0 | 0 | 0 | 0 | 0 | 1 | 88.4 | 88.4 | 4.55 |
| malformed_output | model | 1 | 1 | 0 | 1 | 1 | 1 | 1 | 109.1 | 109.1 | 5.52 |
| prompt_variant | prompt | 1 | 1 | 0 | 1 | 1 | 1 | 1 | 97.4 | 97.4 | 1.62 |

Per trace (query = sha256 of the dataset id, first 12):

| Query | Kind | Status | Outcome | Load1 | Signal |
|---|---|---|---|---|---|
| 9d4442119917 | malformed_output | applied | no-runbook | 5.52 | moved {'parse_ok': [['true'], ['false']]} |
| f6bf7d9ddd50 | clean | clean | no-runbook | 4.55 | {} |
| 6b0b52f1a142 | prompt_variant | applied | no-runbook | 1.62 | moved {'bundle_digest': [['sha256:1b5b700607ed7cf70d1f3ddc96c0953677ee75cec8219e5095edea3bb0c378f1'], ['sha256:99983a64628c33e47965d9b2a35493ba2a9939f74dd0ec86aab3baefc94e0d33']]} |

## golden_context_removed: mode and the D1 rule

D1 (#19) counts a `golden_context_removed` trace as a retrieval fault only when
the golden set was in the clean run's top-k, or the mode is `distractor`. A
`remove` whose run never had a golden chunk in its results is `not_reached`: the
store wrapper reports nothing removed, the ledger gets no injection id, and
`injections` gets no row.

Re-derived from the ledger: 1 applied (distractor 1), 0 not_reached; 1 counted for D1.

Queries with no golden chunk in the clean top-k: 1; `9465608b62ea` ran `distractor`.

| Query | Mode | Golden in clean top-k | Golden in faulted top-k | Max score clean -> faulted | Signal moved | Counted |
|---|---|---|---|---|---|---|
| 9465608b62ea | distractor | False | False | 0.1586613 -> -0.2219435 | True | True |

## step_memory_loss gate

The loop signal (one `(tool_name, args_hash)` at least 3 times) fired on 1 of 1 applied injections: 100%, threshold 60% (`tools/faults/validation.yaml`). Reported, not gated: the gate was the #14 validation run's.

## Reproducibility

Fixed by the seed: the plan (which query gets which kind, its parameters and injection
point; byte-identical, its sha256 above), the injection ids, and a `seed` on every
Ollama chat request (`ids.request_seed(seed, query_ref, call_index)`), clean and faulted
alike. The apps' own temperatures are unchanged (agentic-rag: agent 0.2, judge 0.0,
summarizer 0.3; runbook-agent as recorded per request below).

Not reproducible: token-level model output across Ollama versions, hardware, threads
and parallel requests (the request seed is best-effort); therefore the agent's path,
and whether a conditional injection point is reached (`not_reached`); judge scores;
latencies; trace and span ids; sandbox alert timing.

runbook-agent request fields seen by the preload: ['{"max_tokens": null, "temperature": 0}']

## D1 projection (#19)

Wall time per trace measured here (median per kind) times the D1 composition proposed
on #19. Orchestration has two kinds, so D1 holds 150 orchestration traces
against 75 for each other component; #19 reports macro-F1 by
component and per app as well as pooled.

| App | Kind | Traces | Median s | Hours |
|---|---|---|---|---|
| agentic-rag | golden_context_removed | 75 | 52.1 | 1.1 |
| agentic-rag | tool_failure | 75 | 87.2 | 1.8 |
| agentic-rag | output_budget_exhausted | 75 | 60.5 | 1.3 |
| agentic-rag | step_memory_loss | 75 | 133.2 | 2.8 |
| agentic-rag | malformed_output | 50 | 71.8 | 1.0 |
| agentic-rag | prompt_variant | 50 | 159.5 | 2.2 |
| agentic-rag | clean | 110 | 77.5 | 2.4 |
| runbook-agent | malformed_output | 25 | 109.1 | 0.8 |
| runbook-agent | prompt_variant | 25 | 97.4 | 0.7 |
| runbook-agent | clean | 40 | 88.4 | 1.0 |

Total about 14.9 h of serial run time (Ollama serves one request at a time), plus settle and assembly; M2 is due 2026-11-22.

## Blockers

None recorded.

## Result

All checks passed.
