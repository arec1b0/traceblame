# Fault injection through kind: validation run (2026-11-05)

`tools/faults/` (#14): the six fault kinds injected into real runs of agentic-rag
(CUAD) and runbook-agent (sandbox alerts), exported through kind as `tenant-eval`.
Per kind: the marker survives on the intended span, the injections row joins it, the
signal the #16 detector reads moved against a clean trace, and the trace reaches
`trace.completed`. Machine-readable: the `.json` next to this file.

## agentic-rag

- run `faults-20261105-s7-b1b6b60`, traceblame `b1b6b6048b98`, app `5370b067fdd1`
- plan sha256 `76aae3c66e04d97b`, 28 entries, seed 7
- spans sent 366, stored 366; baseline digest `sha256:3d5cd7f97e85e17b`
- content check: {'checked': 4, 'leaking': 0}
- golden chunks of the sampled answerable queries: {'mapped': 12, 'answerable_sampled': 12}

| Kind | Component | Traces | Applied | Not reached | Marker on its span | Joined | Signal moved | Assembled | Median s | p95 s |
|---|---|---|---|---|---|---|---|---|---|---|
| clean | - | 4 | 0 | 0 | 0 | 0 | 0 | 4 | 58.9 | 93.21 |
| golden_context_removed | retrieval | 4 | 4 | 0 | 4 | 4 | 4 | 4 | 57.5 | 65.14 |
| malformed_output | model | 4 | 4 | 0 | 4 | 4 | 4 | 4 | 72.3 | 77.95 |
| output_budget_exhausted | orchestration | 4 | 4 | 0 | 4 | 4 | 4 | 4 | 60.3 | 118.81 |
| prompt_variant | prompt | 4 | 4 | 0 | 4 | 4 | 4 | 4 | 67.8 | 183.41 |
| step_memory_loss | orchestration | 4 | 4 | 0 | 4 | 4 | 4 | 4 | 174.2 | 187.88 |
| tool_failure | tool | 4 | 4 | 0 | 4 | 4 | 4 | 4 | 64.3 | 87.33 |

Per trace (query = sha256 of the dataset id, first 12):

| Query | Kind | Status | Outcome | Signal |
|---|---|---|---|---|
| 9465608b62ea | clean | clean | answered | {} |
| 9465608b62ea | golden_context_removed | applied | answered | moved {'golden_in_topk': [False, False], 'max_score': [0.1586613, -0.2219435]} |
| 9465608b62ea | malformed_output | applied | answered | moved {'parse_ok': [['true'], ['false']]} |
| 9465608b62ea | output_budget_exhausted | applied | answered | moved {'finish_reasons': [['stop'], ['length']], 'parse_ok': ['false']} |
| 9465608b62ea | prompt_variant | applied | answered | moved {'bundle_digest': [['sha256:3d5cd7f97e85e17beae69c4e6a58b33d26823c78bf296251516e842ec5c86965'], ['sha256:99ff7808c1e62c8b797a7988fb76f3379d2c31716054bf8599751b10e89fc5e7']]} |
| 9465608b62ea | step_memory_loss | applied | answered | moved {'loop_max': [2, 7], 'repeat_min': 3} |
| 9465608b62ea | tool_failure | applied | answered | moved {'marked_tool_error': 1, 'consumed_by_llm': 1, 'retried': 0, 'error_type': ['ToolFailure']} |
| 71508edacc6c | clean | clean | answered | {} |
| 71508edacc6c | golden_context_removed | applied | answered | moved {'golden_in_topk': [True, False], 'max_score': [0.13639897, 0.09639299]} |
| 71508edacc6c | malformed_output | applied | answered | moved {'parse_ok': [['true'], ['false']]} |
| 71508edacc6c | output_budget_exhausted | applied | answered | moved {'finish_reasons': [['stop'], ['length']], 'parse_ok': ['false']} |
| 71508edacc6c | prompt_variant | applied | empty_answer | moved {'bundle_digest': [['sha256:3d5cd7f97e85e17beae69c4e6a58b33d26823c78bf296251516e842ec5c86965'], ['sha256:99ff7808c1e62c8b797a7988fb76f3379d2c31716054bf8599751b10e89fc5e7']]} |
| 71508edacc6c | step_memory_loss | applied | answered | moved {'loop_max': [1, 5], 'repeat_min': 3} |
| 71508edacc6c | tool_failure | applied | answered | moved {'marked_tool_error': 1, 'consumed_by_llm': 1, 'retried': 0, 'error_type': ['ToolFailure']} |
| 64445239fb24 | clean | clean | answered | {} |
| 64445239fb24 | golden_context_removed | applied | answered | moved {'golden_in_topk': [True, False], 'max_score': [-0.10814011, -0.14029014]} |
| 64445239fb24 | malformed_output | applied | answered | moved {'parse_ok': [['true'], ['false']]} |
| 64445239fb24 | output_budget_exhausted | applied | answered | moved {'finish_reasons': [['stop'], ['length']], 'parse_ok': ['false']} |
| 64445239fb24 | prompt_variant | applied | answered | moved {'bundle_digest': [['sha256:3d5cd7f97e85e17beae69c4e6a58b33d26823c78bf296251516e842ec5c86965'], ['sha256:efb1b6be85504b589451fa68ed4396fae2ffedeee2d584934bb7f44eaa0e688f']]} |
| 64445239fb24 | step_memory_loss | applied | answered | moved {'loop_max': [1, 4], 'repeat_min': 3} |
| 64445239fb24 | tool_failure | applied | answered | moved {'marked_tool_error': 1, 'consumed_by_llm': 1, 'retried': 0, 'error_type': ['ToolFailure']} |
| c6b82487fd31 | clean | clean | answered | {} |
| c6b82487fd31 | golden_context_removed | applied | answered | moved {'golden_in_topk': [False, False], 'max_score': [-0.23447287, -0.4736333]} |
| c6b82487fd31 | malformed_output | applied | answered | moved {'parse_ok': [['true'], ['false']]} |
| c6b82487fd31 | output_budget_exhausted | applied | answered | moved {'finish_reasons': [['stop'], ['length']], 'parse_ok': ['false']} |
| c6b82487fd31 | prompt_variant | applied | empty_answer | moved {'bundle_digest': [['sha256:3d5cd7f97e85e17beae69c4e6a58b33d26823c78bf296251516e842ec5c86965'], ['sha256:99ff7808c1e62c8b797a7988fb76f3379d2c31716054bf8599751b10e89fc5e7']]} |
| c6b82487fd31 | step_memory_loss | applied | answered | moved {'loop_max': [1, 5], 'repeat_min': 3} |
| c6b82487fd31 | tool_failure | applied | answered | moved {'marked_tool_error': 1, 'consumed_by_llm': 1, 'retried': 0, 'error_type': ['ToolFailure']} |

## runbook-agent

- run `faults-20261105-s7-b1b6b60`, traceblame `b1b6b6048b98`, app `92fb6f8fcbcf`
- plan sha256 `4fd1295b28bb9283`, 9 entries, seed 7
- spans sent 33, stored 33; baseline digest `sha256:1b5b700607ed7cf7`
- content check: {'alert_needles': 30, 'prompt_windows': 153, 'leaking': 0}
- preload sha256 `48ec262641600789`; image guard {'base_node_options': False, 'base_has_preload': False, 'eval_node_options': True, 'eval_has_preload': True, 'fetch_without_switch': 'fetch', 'fetch_other_tenant': 'fetch', 'fetch_eval_tenant': 'handle'}

| Kind | Component | Traces | Applied | Not reached | Marker on its span | Joined | Signal moved | Assembled | Median s | p95 s |
|---|---|---|---|---|---|---|---|---|---|---|
| clean | - | 3 | 0 | 0 | 0 | 0 | 0 | 3 | 113.6 | 364.8 |
| malformed_output | model | 3 | 3 | 0 | 3 | 3 | 3 | 3 | 93.1 | 120.1 |
| prompt_variant | prompt | 3 | 3 | 0 | 3 | 3 | 3 | 3 | 60.4 | 84.5 |

Per trace (query = sha256 of the dataset id, first 12):

| Query | Kind | Status | Outcome | Signal |
|---|---|---|---|---|
| 9d4442119917 | malformed_output | applied | no-runbook | moved {'parse_ok': [['true'], ['false']]} |
| 6b0b52f1a142 | malformed_output | applied | no-runbook | moved {'parse_ok': [['true'], ['false']]} |
| f6bf7d9ddd50 | clean | clean | no-runbook | {} |
| ffda37087a98 | clean | clean | remediated | {} |
| b7c734ff8c4b | malformed_output | applied | no-runbook | moved {'parse_ok': [['true'], ['false']]} |
| d704e3f127ed | clean | clean | no-runbook | {} |
| 664c2877e038 | prompt_variant | applied | remediated | moved {'bundle_digest': [['sha256:1b5b700607ed7cf70d1f3ddc96c0953677ee75cec8219e5095edea3bb0c378f1'], ['sha256:99983a64628c33e47965d9b2a35493ba2a9939f74dd0ec86aab3baefc94e0d33']]} |
| 286a4cdc6314 | prompt_variant | applied | no-runbook | moved {'bundle_digest': [['sha256:1b5b700607ed7cf70d1f3ddc96c0953677ee75cec8219e5095edea3bb0c378f1'], ['sha256:85dda408a6af4dd9872cbb965a4c67da11b38c7a2a534999c7754abd32f5ab90']]} |
| 2ec8cd13b616 | prompt_variant | applied | rejected | moved {'bundle_digest': [['sha256:1b5b700607ed7cf70d1f3ddc96c0953677ee75cec8219e5095edea3bb0c378f1'], ['sha256:85dda408a6af4dd9872cbb965a4c67da11b38c7a2a534999c7754abd32f5ab90']]} |

## step_memory_loss gate

The loop signal (one `(tool_name, args_hash)` at least 3 times) fired on 4 of 4 applied injections: 100%, threshold 60% (`tools/faults/validation.yaml`). **Passed**: the kind is collected for D1.

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
| agentic-rag | golden_context_removed | 75 | 57.5 | 1.2 |
| agentic-rag | tool_failure | 75 | 64.3 | 1.3 |
| agentic-rag | output_budget_exhausted | 75 | 60.3 | 1.3 |
| agentic-rag | step_memory_loss | 75 | 174.2 | 3.6 |
| agentic-rag | malformed_output | 50 | 72.3 | 1.0 |
| agentic-rag | prompt_variant | 50 | 67.8 | 0.9 |
| agentic-rag | clean | 110 | 58.9 | 1.8 |
| runbook-agent | malformed_output | 25 | 93.1 | 0.6 |
| runbook-agent | prompt_variant | 25 | 60.4 | 0.4 |
| runbook-agent | clean | 40 | 113.6 | 1.3 |

Total about 13.5 h of serial run time (Ollama serves one request at a time), plus settle and assembly; M2 is due 2026-11-22.

## Blockers

- ClickHouse persistence in kind (#58): D1 collection was blocked until it was fixed. Fixed by PR #59 (merged 2026-11-05 14:03 UTC) before this run: the CHI pod mounts the Bound PVC data-chi-traceblame-demo-0-0-0, and this run's database was the persistent one. It no longer blocks D1.
- Prompt class without an MVP detector: decided on #16 (prompt_regression moves into the MVP set). This run records what it needs: baseline_digest per run in injections.params, and clean traces carrying the baseline digest. It no longer blocks D1.
- D1 itself (#19) still needs about 13.5 h of serial run time (projection above) in slots on the shared kind cluster (flock), plus the frozen snapshot export under evals/datasets/.

## Result

All checks passed.
