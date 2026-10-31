# ADR-0006: Shim-side content handling: in-process features, sealed provider, export allowlist

- **Status:** Proposed
- **Date:** 2026-10-31
- **Issue:** #12 (design issue #10); applies to the TypeScript shim (#13) as well
- **Decider:** Daniil Krizhanovskyi

## Context

Content capture is off by default (CLAUDE.md hard rule 2, ADR-0001 section 5), and the
normalizer drops or hashes what an SDK exports anyway (ADR-0002). Some detectors still need
facts about content (ADR-0002, "Negative and risks"):

- `loop` needs `aegis.tool.args_hash`, which the profile defines over the tool arguments;
- `schema_violation` needs to know whether a structured output parses;
- `retrieval_miss` needs to know whether the answer cites the retrieved documents.

Only the process that made the call sees the content, so the shims compute these features
there. Profile 1.6.0 adds `aegis.output.parse_ok` and `aegis.output.cited_doc_ids` for the
last two. This ADR fixes how a shim may look at content without letting any of it leave the
process.

Facts that shape the decision:

- **OpenInference masks at attribute-set time.** A `TraceConfig` hide flag replaces or drops
  an attribute before it is set, so a hidden value is never in memory. Hiding outputs also
  hides output messages (`openinference-instrumentation` 0.1.74), so `output.value` cannot be
  hidden while the output messages stay visible.
- **The OpenAI instrumentation puts `response_format` only in `llm.invocation_parameters`.**
  Whether the request asked for structured output is not visible anywhere else.
- **Error text echoes content.** The OpenAI client's errors read
  `Error code: 400 - <server body>`, in the status description and the `exception` event, and
  the body may quote the request. The normalizer reads `http.response.status_code` from the
  `Error code: NNN` prefix (#29).
- **Ended spans are immutable.** `SpanProcessor.on_end` receives a read-only span, so features
  computed after the call ends must go into a copy of the span made before export.

## Decision

### 1. One export path, owned by the shim

- `instrument()` creates the `TracerProvider` and sets it as the global provider. It fails
  if a global provider is already set.
- The provider is sealed: adding a span processor after `instrument()` raises. Nothing else
  can see a span.
- The pipeline is: a context processor (on start) → batch processor → **sanitizing
  exporter** → OTLP exporter. The sanitizing exporter computes the features, then builds a
  new span from a positive allowlist, and only that copy reaches the OTLP exporter.

### 2. What is visible in-process

- **Hidden at the source** (never in memory): inputs, input messages and text, input images,
  prompts, tool definitions, embedding text and vectors.
- **Visible in-process, never exported:** output messages and `output.value` (for
  `parse_ok` and citations), `llm.invocation_parameters` (for `response_format`).
- The helper spans (agent, retriever, tool) never put text on a span: the tool helper hashes
  the arguments it is given and drops them; the retriever helper records ids and scores
  only.

### 3. Positive export allowlist

Anything not listed is dropped, in every place a span can carry data:

| Place | Kept |
|---|---|
| Span attributes | profile attributes, and the OpenInference table's `kind.from`, `map` sources and `known` keys (`collector/processor/normalizer/mappings/openinference/`) |
| Resource attributes | profile resource attributes, and the OTel SDK's `telemetry.sdk.*` and `service.instance.id` |
| Events | `exception` only, with `exception.type` only |
| Links | the linked context; attributes by the span attribute rule |
| Status | the code; the description only as its `<Class>: Error code: NNN` prefix, when it has one |

The allowlist is disjoint from the profile's `forbidden` list and from every mapping table's
`content` rules. `tests/contract/test_shim_contract.py` checks both, so a key cannot be
exported by the shim and redacted by the normalizer at the same time.

From `llm.invocation_parameters` the shim copies two scalars into profile attributes before
dropping it: `model` → `gen_ai.request.model` and `max_tokens` → `gen_ai.request.max_tokens`.
A failed call carries no `llm.model_name`, so without the copy its required request model
would be lost with the parameters.

### 4. Feature definitions

Both shims implement these rules exactly.

**`aegis.tool.args_hash`.** As profile `source.algorithm` (`algorithm_version: 1`). The
arguments are hashed as text; a shim that holds them as an object serializes them with ASCII
escapes first (Python `json.dumps(ensure_ascii=True)`), which JCS reverses for valid text.
Each shim runs every golden vector (`schemas/profile/vectors/v1/`).

**`aegis.output.parse_ok`.** Set on an LLM span only when the request's `response_format`
has `type` `json_object` or `json_schema`:
- `json_object`: true when the first output message's content parses as JSON and is an
  object;
- `json_schema`: true when it parses and is valid against `json_schema.schema`
  (JSON Schema draft 2020-12).

No output message on a successful call counts as false. A failed call gets no value.

**`aegis.output.cited_doc_ids`.** Set on an LLM span when the trace has retrieval spans,
made through the retriever helper, that ended at or before the LLM span's start:
- the candidates are their ids, in retrieval order;
- an id is cited when it occurs in the first output message's content with no character of
  `[A-Za-z0-9_-]` immediately before or after it;
- the value is the cited ids in order of first occurrence in the text, without repeats, and
  an empty array when none is cited.

The per-trace id registry is in memory, bounded by a configurable number of traces (default
1024, hard rule 8), least recently used first out. A trace evicted before its LLM span is
exported gets no value, never a wrong one.

### 5. Correlation and markers

- `aegis.tenant.id` and `aegis.eval.run.id` are resource attributes, set once at
  `instrument()`. An eval run is one process with one run id.
- `aegis.session.id`, `aegis.bundle.digest` and `aegis.decision.id` are set by the context
  processor on every span started inside the matching context.
- `aegis.fault.injection.id` is set on the one span it names.
- Values are checked against the profile patterns when they are given, and a bad one raises:
  a marker that the normalizer would drop is a harness bug, not something to discover in
  ClickHouse.

## Alternatives considered

**A. Hide everything at the source; the application reports the features.** The app would
call `record_parse(ok)` and `record_citations(ids)` itself. Rejected: every call site must do
it right, the app diff grows with each feature, and an ended span cannot take the value, so
the app would also have to keep the LLM span open.

**B. Export content and let the normalizer compute the features.** Rejected: content would
cross the network and the agent's file queue, which ADR-0002 section 4 forbids.

**C. Negative list (drop known content keys).** Rejected: a new content attribute from an SDK
upgrade would be exported until someone lists it. The normalizer is fail-closed only on large
values (ADR-0002); the shim, which sees everything first, is fail-closed on every key.

## Consequences

**Positive**
- Content-free features without any content leaving the process, and no app call sites
  beyond the helpers.
- An SDK upgrade cannot leak a new content attribute: it is dropped until it is allowlisted,
  and allowlisting needs a mapping-table entry and the contract test.

**Negative and risks**
- Output text and invocation parameters sit in memory until export (batch delay). They are
  never written anywhere, but a heap dump would show them.
- The sealed provider means the app cannot add its own exporter to the same provider. An app
  that needs one must run a second provider for its own spans.
- `cited_doc_ids` sees only literal ids. An app that cites by title or by index needs an id
  in the answer, which may need a prompt change (agentic-rag got one).
- Two shims must keep the rules above identical. Only `args_hash` has golden vectors; the
  other two rules rely on this text and on each shim's tests.
