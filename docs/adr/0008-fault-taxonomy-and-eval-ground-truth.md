# ADR-0008: Fault taxonomy, injection points and eval ground truth

- **Status:** Proposed
- **Date:** 2026-11-05
- **Issue:** #14 (design issue #12). The number is the next free one; it is assigned at merge.
- **Decider:** Daniil Krizhanovskyi

## Context

Design §1.1 gives `faults` as a "Python lib + CLI" with "6 fault kinds with a ground-truth
label in injections", and §1.6 builds D1 from them: 6 kinds × 75 + 150 clean, real queries
(CUAD, runbook-agent tasks, agentic RAG), split by source query. The design never names the six
kinds, where they are injected, or how a clean trace and a source query are recorded. This ADR
fills that gap.

Facts that shape the decision:

- **Two output features are flat on clean traffic** (#12, #16). On 24 real CUAD questions
  `aegis.output.cited_doc_ids` is `[]` on 67/67 LLM calls and `aegis.output.parse_ok` is
  `true` on 91/91. A fault that should move `retrieval_miss` or `schema_violation` must move
  something else, or make parse_ok false.
- **The apps must not change.** Faults go into real runs of the real apps. agentic-rag runs
  in-process under the capture driver, which owns its one `RAGAgent`. runbook-agent runs in a
  kind sandbox from an image the driver builds.
- **Only an eval tenant may mark traces** (ADR-0005, #36). `aegis.fault.injection.id` (span)
  and `aegis.eval.run.id` (resource) survive the normalizer only for tenants in
  `eval_tenants`, and the gateway keeps every marked trace.
- **runbook-agent rarely reaches a tool.** With qwen2.5:7b, 2 of 24 tasks ran a kubectl step
  in the #13 capture. Every task, though, makes one triage call.
- **Content stays in the process** (ADR-0002, ADR-0006). Fault parameters are ground truth
  stored in ClickHouse, so they must hold no prompt or document text.

## Decision

### 1. Six kinds over five components

One kind per component, and two for orchestration, which has two MVP detectors.

| Kind | Ground truth | Apps | Injection point (seam) | Marker span | Signal for #16 |
|---|---|---|---|---|---|
| `golden_context_removed` | retrieval | agentic-rag | store wrapper under the retriever, on every retrieval of the run: golden chunks removed and backfilled with the next nearest (`remove`), or replaced by another contract's nearest chunks (`distractor`) | each retriever span whose results it changed | `retrieval_miss`: lower max retrieval score; eval-only `golden_in_topk` false |
| `tool_failure` | tool | agentic-rag | the retriever's store raises a connection error or timeout on the k-th retriever call; the app's own `except` turns it into a FAILURE result fed back to the model | the TOOL span | `tool_error_propagation`: an ERROR tool span consumed by the next LLM step |
| `malformed_output` | model | both | response content of the k-th LLM call truncated, fenced or prefixed with prose; finish_reason as returned (TS: truncation only) | the LLM span | `schema_violation`: `parse_ok=false` |
| `output_budget_exhausted` | orchestration | agentic-rag | `max_tokens` (8–32) added to the k-th request, so Ollama really stops (finish_reason=length) | the LLM span | `context_truncation`: finish_reason=length (parse_ok=false follows) |
| `step_memory_loss` | orchestration | agentic-rag | from iteration k on, the loop hands the model no previous steps and the initial observation | the AGENT span | `loop`: one (tool_name, args_hash) at least 3 times |
| `prompt_variant` | prompt | both | a different prompt template for the whole run (TS: the triage request's system message) | AGENT (Py), doGenerate (TS) | `prompt_regression` (moved into the MVP set, see Consequences): `aegis.bundle.digest` differs from the baseline |

D1 per component: orchestration 150, retrieval, tool, model and prompt 75 each. TypeScript
carries only model and prompt (25 + 25 of D1's 75 each), because those fire on every task.
#19 reports per component, per app and pooled, and names both imbalances.

A `golden_context_removed` trace counts as a retrieval fault in D1 only when the golden set
was in the clean run's top-k, or the mode is `distractor`. A `remove` that finds no golden
chunk in any result is `not_reached` (section 3).

`output_budget_exhausted` moves `parse_ok` too: a truncated generation does not parse. The
attributor has to prefer orchestration when finish_reason=length explains the parse failure.
That overlap is real, not an artefact.

### 2. Seams without app changes

- **agentic-rag (in-process, `tools/faults/agentic_rag_app.py`).**
  - An httpx2 transport under the agent's, judge's and summarizer's `AsyncOpenAI` clients.
    OpenInference runs it inside the LLM span, so the marked span is the call's.
  - Wrappers of the store behind the retriever.
  - Instance attributes of the one agent: `_generate_step`, the two prompt templates, and
    `_reasoning_loop`, which reads the trace id.
  - Everything is undone after each plan entry.
- **runbook-agent (the eval image).**
  - The image is built `FROM` the capture image and adds `packages/faults/preload/
    faults-preload.mjs` and `NODE_OPTIONS=--import=...`, nothing else.
  - The preload wraps `globalThis.fetch`, which the AI SDK resolves per call, and marks the
    active doGenerate span through traceblame-ts.
  - Before each task the driver writes the task's entry to `/tmp/faults/armed.json` in the pod.
    The preload appends one content-free line per call to `/tmp/faults/ledger.jsonl`.
  - The preload exists only in the eval image: a test scans every non-eval manifest and
    capture path, and the driver inspects both images. It is a no-op unless
    `TRACEBLAME_FAULTS=1` and the tenant is one of `TRACEBLAME_EVAL_TENANTS`. The Python
    injector applies the same guard (`traceblame_faults.guard`). Its sha256 is in the
    provenance.

### 3. Reproducibility

- **The plan is a pure function of the seed** (`traceblame_faults.plan`): which query gets
  which kind, its parameters and its injection point, written as JSON lines whose sha256 the
  report records.
  - Per kind, queries are ranked by `sha256("seed:kind:query_ref")`.
  - Each entry's parameters come from `Random(sha256("seed:query_ref:kind"))`.
- **Every Ollama chat request of an eval run gets a `seed`**, `request_seed(seed, query_ref,
  call_index)`, clean and faulted alike. The apps' own temperatures are unchanged.
- **Not reproducible**, and recorded as such:
  - token-level output across Ollama versions, hardware and parallelism (the seed is
    best-effort);
  - hence the agent's path, and whether a conditional injection point is reached;
  - judge scores, latencies, trace and span ids, and sandbox alert timing.
- **A planned fault whose point never comes is `not_reached`**: no injections row, and #19
  excludes it.
- **Each ledger record carries the host's 1-minute load average** when its trace ended
  (`host_load1`, ledger only). Wall time per trace depends on it, so the D1 throughput
  numbers come with it.

### 4. Ground truth

- **`injection_id`** is `{eval_run_id}:{kind}:{sha256(query_ref)[:12]}`, inside the profile
  pattern. It is the value of `aegis.fault.injection.id` on the marked span(s), so an
  injection joins its spans on `(trace_id, spans.attrs['aegis.fault.injection.id'])`
  (contract statement `injections_spans_read.sql`).
- **`injections`** gets one row per applied fault, through the contract statement
  `injections_write.sql`, which inserts only when `(trace_id, injection_id)` is new.
  - `params` is a closed schema per kind (`traceblame_faults.kinds`): numbers, booleans,
    enum values, ids and sha256 digests, never text.
  - It includes the run's `baseline_digest`.
- **`eval_traces`** (new expand migration) gets one row per eval trace, clean or faulted:
  - run, app, source and `query_ref`, the dataset's own id (CUAD question id; runbook task
    `<task>@s<seed>`);
  - stratum and golden chunk ids;
  - planned kind and `injection_id`.

  A clean trace is `planned_fault_kind = ''`. The table has no TTL; spans expire after 30
  days, so #19 exports a frozen D1 snapshot (spans + eval_traces + injections, sha256
  manifest) under `evals/datasets/`.
- **Golden chunks** are the chunk ids whose text holds a CUAD answer, or a 64-character
  window of a longer one. Only the eval-only feature `golden_in_topk` may read them, never the
  production `retrieval_miss` path. Metrics are reported with and without eval-only features.
- **No feature may read the labels**: no detector or attributor reads
  `aegis.fault.injection.id`, `aegis.eval.run.id`, `eval_traces` or `injections`.

### 5. Prompt variants without promptops

- `aegis.bundle.digest = "sha256:" + sha256(JCS({"app": <app>, "templates": {<name>:
  <text>}}))` over the prompt templates in the app's code, the same in both languages (shared
  vectors).
- It is a bundle identity, not a digest of request content (ADR-0002 §2). Only the digest
  leaves the process.
- Clean traces carry the baseline digest of the app's own templates.
- The variants are files in `scenarios/prompts/` with their source and rationale:
  - agentic-rag's reasoning template from before abc8838, a real regression;
  - a stripped system prompt;
  - runbook-agent's triage prompt without its decline rule, and without its candidate rule.

## Alternatives considered

- **Faults from a proxy in front of Ollama** (`OPENAI_BASE_URL` / `LLM_BASE_URL`). Rejected:
  a proxy runs outside the span, so it cannot mark the span it changed, and every switch
  needs a redeploy in the sandbox.
- **A fault middleware inside traceblame-ts.** Rejected: it puts fault code into the
  production shim. The preload keeps it in the eval image.
- **Golden documents as a profile attribute** (eval-only, like the markers). Rejected for
  now: it touches the profile, the normalizer and both shims, and a table is enough for
  evaluation.
- **Labels in a committed manifest only.** Rejected: detectors could not read golden ids
  online, and clean traces would not be distinguishable in ClickHouse.

## Consequences

**Positive**

- Every D1 trace is a real run on a real input. The fault is the only difference from a
  clean run, and that difference is recorded.
- Labels, the source query and clean status are all in ClickHouse, joinable to spans.

**Negative and risks**

- **The prompt class had no MVP detector.** `prompt_regression` was Full-stage, so prompt
  recall would be about 0 and macro-F1 could not reach 0.75. Decided on #16: it moves into
  the MVP set (digest change against the run's baseline plus an outcome shift). This ADR
  records what it needs: the run's `baseline_digest` in `injections.params`, and clean
  traces that carry the baseline digest.
- **`step_memory_loss` depends on the model repeating itself.** It is collected for D1 only
  if the loop signal fires on at least 60% of applied injections in the validation run
  (`tools/faults/validation.yaml`); otherwise the kind is redesigned first.
- **Prompt variants also move other signals.** The historical template's invalid tool calls
  produce tool errors and repeats. That is realistic, and the digest change is what tells
  them apart.
- **The seams depend on app internals** (attribute names, the retriever's error handling,
  the AI SDK's fetch lookup). An app change can break them; the validation run's per-kind
  marker and signal checks catch it.
