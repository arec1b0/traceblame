# ADR-0002: Content redaction in the normalizer, and opt-in content capture

- **Status:** Proposed
- **Date:** 2026-10-31
- **Issue:** #9 (design issue #7). The content store is a separate issue (M3).
- **Decider:** Daniil Krizhanovskyi

## Context

CLAUDE.md hard rule 2 keeps content capture off by default and has the normalizer hash or
redact prompt/completion content. ADR-0001 section 5 settles the outline:
- shims ship with capture off;
- the profile JSON Schema rejects the semconv content attributes;
- the normalizer hashes or drops content from SDKs that capture it;
- a tenant can opt in to an encrypted out-of-band copy with a 7 d TTL, and the span then
  carries only `aegis.content.ref`.

This ADR fills in the details. The content-on golden fixtures (`*_content`, captured with
each SDK's content switch on) showed what real exports carry:

| SDK | Where the content is |
|---|---|
| OpenInference 0.1.68 | `input.value` / `output.value` (the whole request and response JSON); `llm.input_messages.*` / `llm.output_messages.*`, including tool-call arguments; `embedding.embeddings.*.embedding.text` and `.vector`; `llm.tools.*` (tool definitions); `llm.invocation_parameters` |
| OpenLLMetry 0.62.4 | `gen_ai.input.messages`, `gen_ai.output.messages`, `gen_ai.tool.definitions`; older releases use `gen_ai.prompt.N.*` / `gen_ai.completion.N.*` (semconv-ai 0.5.1) |
| otel-genai 2.4b0, latest experimental | `gen_ai.input.messages`, `gen_ai.output.messages`; the default mode writes content to log events, not spans |

Three more facts shape the decision:
- **Profile scope.** The profile's forbidden list names semconv attributes only, and adding
  vendor names to it would be a major change (ADR-0001 section 2).
- **Error details.** The captured errors carry a server message that echoes the request
  (`[] is too short - 'messages'`) and Python tracebacks with local paths, in status
  messages and `exception` events.
- **Pass-through.** ADR-0001 passes unknown attributes through unchanged, so a content
  attribute that no rule names yet would ride through.

## Decision

### 1. What the normalizer removes, always

- **Content attributes.** These are the profile's forbidden list plus the `content:` list of
  every mapping table (`collector/processor/normalizer/mappings/`).
  - They are dropped from every span, its events, its links and its resource, whatever the
    span's SDK.
  - Content rules only remove, so they may name attributes from an SDK's published
    conventions before a fixture shows them. Mapping rows still need a fixture.
- **Never mapped.** A mapping rule may not use a content attribute as an `attr` source. A
  `json` source may read one named scalar field out of it first. Example: the request model
  in `llm.invocation_parameters`, which OpenInference error spans carry and nothing else
  does.
- **Error details on GenAI spans.** A GenAI span is one a table matched, or one that carries
  `gen_ai.*` attributes. On such spans:
  - the status message is dropped;
  - every event but `exception` is dropped;
  - every `exception` attribute but `exception.type` is dropped.

  `error.type` and the status code stay; `tool_error_propagation` needs nothing more.
  Non-GenAI spans keep their events and status message, minus content attributes.
- **Fail closed on unknown content.** This deviates from ADR-0001's pass-through.
  - On GenAI spans, an attribute that neither the profile nor the table knows is dropped
    when its string value is longer than `max_unmapped_value_bytes`. The limit is in config,
    starting at 256 (hard rule 8). For an array, its strings count together.
  - Every drop is counted in `traceblame_normalizer_dropped_large_values_total{sdk,attr}`.
  - Shorter unknown attributes pass through and count as unmapped, as in ADR-0001.
    Non-GenAI spans are not size-checked.

### 2. What the normalizer hashes

- **Only into a profile slot.** That means `aegis.tool.args_hash`, derived from
  `gen_ai.tool.call.arguments`.
  - Profile 1.2.0 fixes the algorithm: the first 16 lowercase hex characters of SHA-256 over
    the RFC 8785 (JCS) serialization. Arguments that are not I-JSON are hashed over their
    raw UTF-8 bytes.
  - A hash the shim set wins. Shims and the normalizer must agree byte for byte; the
    normalizer's JCS is tested against the RFC's own vectors.
- **No content digests** such as `aegis.content.input_hash`.
  - The profile has no slot for them: forbidden keys reject any value, and `aegis.*` is
    closed.
  - An unkeyed digest of a short prompt is a dictionary target.
- **Low-entropy arguments are reversible.** `aegis.tool.args_hash` is unkeyed. Arguments with
  few possible values (`kubectl delete ns <name>`, a city) can be recovered by hashing every
  candidate.
  - This is accepted for v1: the loop detector needs one hash across shims and normalizer,
    with no keys to distribute.
  - Keyed hashing is a future decision, e.g. HMAC-SHA-256 with a per-tenant key from the
    content store's key service. It changes the profile algorithm and needs keys in every
    shim. It also needs the compat checker to treat an algorithm change as major, which
    today it does not see at all.

### 3. Opt-in content capture (design only, not implemented)

The collector rejects `content_capture: true` until the content store exists, so content
stays hashed or dropped. The store is its own issue: "feat(content): encrypted opt-in content
store (MinIO, keys, 7d TTL)", M3. Its design:

- **Who opts in.** A tenant, in collector configuration. A span or request cannot opt itself
  in.
- **Where.** In the normalizer's content stage, before any queue or exporter (section 4).
- **Write path.**
  - The content stage hands what it would drop to a content sink, one record per span, then
    drops it from the span as usual.
  - The sink is asynchronous with a bounded queue. The span gets `aegis.content.ref` only
    when the sink accepts the record.
  - A full queue or a failed write loses the content, never the span, and is counted.
    Consumers treat a missing object as "content unavailable".
- **Reference.** `s3://<bucket>/<tenant>/<yyyy-mm-dd>/<trace_id>/<span_id>`. It fits the
  profile pattern `^s3://.+`, and it holds no content hash, which would be a digest of the
  content.
- **Encryption and TTL.**
  - Envelope encryption: a per-tenant AES-256-GCM data key, wrapped by a KMS key (MinIO KES
    in the demo).
  - Object lock retention of 7 d, with a lifecycle rule that deletes after 7 d.
  - Reads are audited.
- **Readers.** The replayer and `faithfulness_judge`, through the API, with tenant
  authorization.
- **Alternatives rejected.**
  - A synchronous write from the processor: it blocks the pipeline on S3.
  - A connector routing content spans to an S3 exporter: content would cross queues and
    exporters in clear, and the contrib S3 exporter has no per-object envelope encryption.

### 4. Where the content stage runs

The agent tier keeps spans in a `file_storage` queue on the node's disk, before the gateway,
so content must be removed before that queue. The content stage must be able to run on the
agent tier, and the processor runs on either tier. The tier layout belongs to issue #10.

## Consequences

**Positive**
- Redaction is proven on real SDK output, not only on hand-made spans:
  - every fixture, including the content-on ones, leaves the normalizer and the built
    collector without the capture's words, tracebacks or local paths;
  - the collector's own debug logs stay clean too.
- The tests check the content-on fixtures carry content, so they cannot pass vacuously.
- One rule set serves both sides: the Go normalizer embeds the YAML, and the Python contract
  tests read the same files.

**Negative and risks**
- A non-content attribute longer than 256 bytes is lost on GenAI spans until a table lists
  it as known. The counter makes that visible.
- Tool definitions are dropped as content.
- Detectors that need content have to wait for content-free features from the shims
  (#12/#13, a profile minor) or for the content store:
  - `schema_violation`: does the structured output parse?
  - `retrieval_miss`: does the answer cite the retrieved chunks?
- Content rules name attributes, so a new content attribute shorter than the limit passes
  until a table names it. It is counted as unmapped and feeds `UnmappedAttrsSpike`.

## Verification

- `collector/processor/normalizer/content_test.go`:
  - every fixture: no capture word in the JSON or protobuf export, no content-shaped key,
    clean logs;
  - canaries in content attributes, events, links, the status and the resource;
  - real error fixtures keep only `exception.type`;
  - `aegis.tool.args_hash`, the size guard, and a fuzz target.
- `internal/jcs`: RFC 8785 Appendix B number samples, the section 3.2.2 sample, and the
  section 3.2.3 sorting sample.
- `tests/contract`: content fixtures carry content and content-off ones do not; after the
  Go normalizer, none do.
- `collector/test/smoke`: the built distribution, logging at `debug`, exports and logs no
  content.
