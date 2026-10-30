# ADR-0001: Telemetry profile v1 on pinned OTel GenAI semantic conventions

- **Status:** Accepted
- **Date:** 2026-10-30
- **Issue:** #1 (M0 Foundations)
- **Decider:** Daniil Krizhanovskyi

## Context

TraceBlame reads spans from instrumented LLM applications and turns them into a blame
verdict. Detectors, storage and the attributor need one span shape that does not move
under them. The design ([docs/design.md](../design.md), "Shared conventions") fixes the
approach: an `aegis-telemetry` profile v1 on top of pinned OTel GenAI semantic
conventions, own attributes only under `aegis.*`, with OpenInference as the alternative
to record. Hard rule 1 in CLAUDE.md requires an explicit version verified from official
sources.

State of upstream on 2026-10-30:

1. **GenAI left core semconv.** Release v1.42.0 (2026-06-12, tag commit
   `ae3a98640194ed405c4c797281502e4d3bd258b3`) deprecated every `gen_ai.*` attribute,
   metric, event and span in `open-telemetry/semantic-conventions` and moved them to
   `open-telemetry/semantic-conventions-genai` [S2]. Three official sources confirm it:
   - the release notes;
   - `CHANGELOG.md` at the tag, which lists the move under "Breaking changes" [S13];
   - the model at the tag: `model/gen-ai/registry.yaml` is gone, and all 60 `gen_ai.*`
     attribute definitions now sit in `model/gen-ai/deprecated/registry-deprecated.yaml`,
     each marked as moved to the GenAI repository [S14].

   The latest core release is v1.44.0 (2026-08-04) [S3, S4].
2. **The new repository has no release.** It has no tags and no GitHub releases.
   `model/manifest.yaml` at commit `72e0d0a097db3ba31ba3f33d0519401a044f6e23` declares
   `schema_url: https://opentelemetry.io/schemas/gen-ai-dev/1.42.0-dev` and
   `stability: development`, with a dependency on core v1.44.0 [S5]. That schema URL
   returns HTTP 404. `RELEASING.md` describes only a dev channel with `vX.Y.Z-dev`
   tags [S6], and none has been published.
3. **v1.41.1 is the last tagged release that defines GenAI.** It was released 2026-05-11
   (tag commit `ead83b9b0fa36540c1642fce46e874f002ac23f1`) [S1]. Its schema file
   `https://opentelemetry.io/schemas/1.41.1` resolves [S7]. The registry [S8] defines,
   at Development stability:
   - `gen_ai.provider.name`, which replaces the deprecated `gen_ai.system`;
   - `gen_ai.operation.name`, with well-known values that include `chat`, `embeddings`,
     `retrieval`, `execute_tool` and `invoke_agent`;
   - `gen_ai.request.*`, `gen_ai.response.*` and `gen_ai.usage.input_tokens` /
     `output_tokens`;
   - `gen_ai.tool.name` and `gen_ai.tool.call.id`, and `gen_ai.agent.name` and
     `gen_ai.agent.id`;
   - `gen_ai.conversation.id`, `gen_ai.data_source.id`, `gen_ai.retrieval.documents` and
     `gen_ai.retrieval.query.text`.

   Attributes added later in the GenAI repository, such as `gen_ai.retrieval.top_k`,
   are not in v1.41.1.
4. **Instrumentation SDKs diverge.** None of them emits exactly one semconv version, so a
   normalizer is needed whatever we pin.
   - OTel `opentelemetry-instrumentation-openai-v2` 2.4b0 (2026-05-01) follows semconv
     v1.30.0 by default. With `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental`
     it emits the latest conventions. It does not capture content by default [S9].
   - OpenInference `openinference-instrumentation-openai` 0.1.68 (2026-10-29) emits only
     OpenInference conventions: `openinference.span.kind`, and flattened, indexed `llm.*`
     keys [S10, S11].
   - OpenLLMetry `opentelemetry-instrumentation-openai` 0.62.4 (2026-09-29) captures
     prompts and completions by default. `TRACELOOP_TRACE_CONTENT=false` turns that
     off [S12].
5. **Repository topology.** The design keeps contracts in the umbrella repository
   `aegis-platform/contracts`. That repository does not exist yet.

## Decision

### 1. Pin

Profile `aegis-telemetry/1` is defined on **OpenTelemetry semantic conventions v1.41.1**:

| Field | Value |
|---|---|
| Repository | `open-telemetry/semantic-conventions` |
| Tag | `v1.41.1` |
| Commit | `ead83b9b0fa36540c1642fce46e874f002ac23f1` |
| Schema URL | `https://opentelemetry.io/schemas/1.41.1` |

Every non-`aegis.*` attribute in the profile must exist in v1.41.1, must not be deprecated
there, and must use the v1.41.1 type. A contract test checks this against a vendored
snapshot of the v1.41.1 registry (issue #3).

**Why v1.41.1**
- **It is the newest release where `gen_ai.*` is defined and not deprecated.** From
  v1.42.0 on, core carries only deprecated stubs that point to the GenAI repository
  [S13, S14]. Pinning v1.42.0 or later would pin deprecated definitions.
- **It is a real, immutable release:**
  - a tag that resolves to one commit;
  - a published schema file that resolves [S7];
  - a registry we vendor and checksum.

  The GenAI repository offers only a moving dev line. Its `schema_url` returns 404, and
  it has published no release [S5, S6, S15].
- **It covers everything profile v1 needs:**
  - `gen_ai.provider.name`;
  - operation names for all six span kinds (`chat`, `embeddings`, `retrieval`,
    `execute_tool`, `invoke_agent`, `invoke_workflow`);
  - the tool, agent, retrieval and usage attributes.

  The contract test confirms that all 18 semconv attributes in the profile exist there,
  undeprecated, with matching types.
- **It keeps the way forward cheap.** Every `gen_ai.*` name the profile uses exists
  unchanged in `semantic-conventions-genai` at `72e0d0a`, so a later re-pin is mostly a
  version change, not a remapping.
- **v1.41.1 rather than v1.41.0:** the patch only excludes two Kubernetes metrics from
  code generation [S1]. The GenAI definitions are identical, so we take the latest patch.

### 2. Profile identity and versioning

- The profile id is `aegis-telemetry/<major>`. This ADR defines major `1`.
- The profile YAML carries a semver `revision`.
- The compat checker (`traceblame_contracts.compat.diff_profiles`, issue #3) computes a
  semantic diff of two profile revisions and classifies every change. Within one major,
  CI allows only minor changes. A major change needs the next profile major.

  | Major (needs `aegis-telemetry/<N+1>`) | Minor (allowed within the major; bump `revision`) |
  |---|---|
  | an attribute removed or renamed (a rename is a removal plus an addition) | an optional attribute added |
  | an attribute retyped, or moved between resource and span | an enum value, span kind or allowed operation added |
  | an enum value removed, including a span kind or an allowed operation | a requirement loosened |
  | a requirement tightened to `required` or `conditionally_required`, or a new attribute added at that level | a pattern, const or enum constraint removed |
  | a pattern, const or enum constraint added or changed, or a kind's operations constrained | an attribute removed from the forbidden list |
  | an attribute added to the forbidden list | the semconv pin changed, with no major change at attribute level |

  Removing, renaming or retyping is major for optional attributes too: consumers may read
  them, and the `aegis.*` namespace is closed. A pin change is judged by what it does to
  the profile's attributes, not by the version string.
- Every normalized span carries `aegis.profile.version` = `aegis-telemetry/1`. This
  matches `profile_version` in the design's ClickHouse `spans` table and blame API.

### 3. Namespace

- Our own attributes live only under `aegis.*`.
- The `aegis.*` namespace is closed. An `aegis.*` key that the profile does not define is a
  contract violation.
- An attribute that is not in v1.41.1 is not part of profile v1, even if a newer GenAI
  convention defines it. Adopting one needs either a re-pin or a new `aegis.*` attribute,
  each through an ADR.

### 4. Correlation IDs

| Attribute | Scope | Requirement | Set by |
|---|---|---|---|
| `trace_id` | span (OTLP field) | required | SDK |
| `aegis.tenant.id` | resource | required | shim; the normalizer may fill it from per-pipeline config |
| `aegis.session.id` | span | recommended | shim; may be mapped from `gen_ai.conversation.id` or OpenInference `session.id` |
| `aegis.bundle.digest` | span | conditionally required: when a promptops bundle is in use. Format `sha256:<64 hex>` | shim |
| `aegis.decision.id` | span | conditionally required: on spans that passed an AgentGate decision | AgentGate / shim |

### 5. Content

- Shims ship with content capture off.
- A normalized span must not carry raw content. The profile JSON Schema rejects
  `gen_ai.input.messages`, `gen_ai.output.messages`, `gen_ai.system_instructions`,
  `gen_ai.tool.call.arguments`, `gen_ai.tool.call.result`, `gen_ai.retrieval.query.text`,
  and the deprecated `gen_ai.prompt` and `gen_ai.completion`.
- The normalizer hashes or drops content from SDKs that capture it by default (issue #7).
- When a tenant opts in, content is stored encrypted out of band with a 7 d TTL, and the
  span carries only `aegis.content.ref`.

### 6. Where the contracts live

The profile YAML, the generated JSON Schema and the semconv snapshot live in
`traceblame/schemas/` until `aegis-platform` exists. They then move to
`aegis-platform/contracts`, and traceblame consumes them as a pinned package. That move
gets its own ADR.

## Adapter layer

```
SDK spans ─▶ detect source ─▶ map to canonical model ─▶ emit profile vN ─▶ normalized spans
```

- **Detect.** The normalizer identifies the SDK and its convention version from:
  - the instrumentation scope name and version;
  - the scope or resource `schema_url`;
  - `telemetry.sdk.*`;
  - marker attributes: `openinference.span.kind`, `llm.*`, `traceloop.*`.
- **Map.** There is one mapping table per (SDK, source convention version), kept as data
  in `collector/processor/normalizer/`. Examples:
  - OpenInference `llm.model_name` → `gen_ai.request.model`;
  - `openinference.span.kind=RETRIEVER` → `aegis.span.kind=retrieval`;
  - v1.30-era `gen_ai.system` → `gen_ai.provider.name`.
- **Emit.** One emitter per profile major writes the span and sets
  `aegis.profile.version`.
- **Derived span class.** The normalizer sets `aegis.span.kind`
  (`llm|embedding|retrieval|tool|agent|chain`). Detectors key on it, so when upstream
  renames `gen_ai.operation.name` values, only the mapping tables change.
- **Drift signal.** Unmapped attributes pass through and are counted in
  `traceblame_normalizer_unmapped_attrs_total{sdk,attr}`. The `UnmappedAttrsSpike` alert
  watches it.
- **Fixtures.** Real exports per SDK and version live in `collector/testdata/fixtures/`
  (dataset D4). The contract stage runs fixtures → normalizer → profile JSON Schema.
  The OTel-native fixture is captured in both default and `gen_ai_latest_experimental`
  mode, so two source versions of one SDK are covered from the start.

## Normalizer behaviour on a newer `schema_url`

A span can declare, on its scope or resource, a `schema_url` newer than anything the
mapping tables know. Examples: `https://opentelemetry.io/schemas/1.44.0`,
`.../gen-ai-dev/1.42.0-dev`, or a future GenAI release. The rule is: map what is known,
pass the rest through, and count it. The normalizer never guesses a rename and never drops
a span.

1. **Pick a mapping table.** Use the SDK's table for the newest source version at or below
   the span's `schema_url`. If the `schema_url` is newer than every known table, use the
   newest table. A span without a `schema_url` is placed by scope name and marker
   attributes, as above. The captured fixtures carry `1.30.0` and `1.37.0`, or no
   `schema_url` at all.
2. **Map.** Attributes the chosen table knows become profile attributes, including known
   renames such as `gen_ai.system` → `gen_ai.provider.name`. An attribute that already has
   a profile name is kept as it is and type-checked. So when a newer convention keeps a
   name, it keeps working with no table change.
3. **Pass through.** Every other attribute passes through unchanged, under its own name.
   OpenLLMetry's non-semconv `gen_ai.is_streaming` and `gen_ai.usage.total_tokens` are
   examples. There are two exceptions, whatever the `schema_url`:
   - forbidden content attributes are dropped or hashed (issue #7);
   - unknown `aegis.*` keys are dropped, because the namespace is closed.
4. **Count.** Every passed-through attribute that is neither a profile attribute nor in
   the chosen table increments `traceblame_normalizer_unmapped_attrs_total{sdk,attr}`.
   - The `UnmappedAttrsSpike` alert watches that counter. It is the early drift signal and
     feeds the migration trigger below.
   - Spans whose `schema_url` is newer than every known table also increment
     `traceblame_normalizer_newer_schema_spans_total{sdk,schema_url}`, listed in design
     §1.5. Its label set is bounded by the number of releases.
5. **Fail open.** A span is never dropped because of its `schema_url`. If a required
   profile attribute is still missing after mapping, the span is emitted anyway. Contract
   tests on fixtures, and the counters in production, expose the gap.

Why pass-through rather than dropping or remapping by guess:
- Dropping unknown attributes would lose evidence that detectors may need later.
- A guessed remap could put a wrong value into a profile attribute and corrupt blame.
- Pass-through plus a counter keeps the data and makes the drift visible.

## v(N) / v(N−1) coexistence

- The normalizer can emit profile v(N) and v(N−1) at the same time. The choice is per
  pipeline and lives in collector config, not in code.
- Consumers (storage, detectors, attributor) read `aegis.profile.version` and accept both
  versions during a migration window.
- A new major N+1 rolls out in the same expand → migrate → contract order as ClickHouse
  migrations:
  1. **Expand.** Ship consumers that accept N and N+1. The normalizer still emits N.
     Contract tests validate fixtures against both schemas.
  2. **Migrate.** Switch the emitter to N+1 pipeline by pipeline, starting with the
     second gateway pool.
  3. **Contract.** Drop N−1 once no N−1 span is left within the spans TTL (30 d).
- **Rollback** reverts the collector ConfigMap to emit N−1. Agents hold spans in their
  file queue during the switch.
- Within one major, the CI compatibility check rejects every major change in the table in
  "Profile identity and versioning". Minor changes pass.

## Migration trigger

**The primary trigger is the first non-dev release of
`open-telemetry/semantic-conventions-genai`:** a `vX.Y.Z` tag without `-dev`, whose
`schema_url` resolves. On 2026-10-30 the repository has no tags and no releases [S15].
When that release ships:

1. **A new ADR re-pins the profile to that release.**
   - Vendor its registry. `tools/semconv/fetch.sh` currently reads only the core
     repository and needs that second source.
   - Diff the profile's attributes against it.
2. **The compat checker decides the bump.**
   - If every profile attribute survives with the same name, type and requirements, the
     re-pin is minor: a new `revision` of `aegis-telemetry/1`.
   - It becomes major (`aegis-telemetry/2`) only if the attribute-level diff has a major
     change, for example an attribute upstream removed, renamed or retyped.
3. **A major re-pin follows the coexistence process above:**
   - the normalizer emits v1 and v2 side by side;
   - consumers accept both;
   - v1 is dropped after one spans TTL (30 d).

   A minor re-pin ships like any other minor revision.

**Secondary triggers.** Each of these also opens a new ADR:
- upstream renames or removes an attribute that the profile uses;
- an SDK we support stops emitting v1.41-era names;
- `UnmappedAttrsSpike` shows lasting drift for one SDK.

Until a trigger fires, profile v1 stays on v1.41.1. The M1 drift check against
`semantic-conventions-genai@HEAD` reports upstream name changes early.

## Alternatives considered

**A. OpenInference conventions as canonical.** Rejected.
- For: richer retrieval, reranker and document semantics; wide framework coverage;
  a mature ecosystem around Phoenix.
- Against:
  - It is governed by one vendor.
  - Its indexed keys (`llm.input_messages.0.message.content`) fit columnar storage and
    JSON Schema poorly.
  - OTel collectors and backends are converging on `gen_ai.*`.
  - OpenInference stays a first-class *source*. The adapter layer maps it, and its
    fixtures run in every contract test.

**B. Pin `semantic-conventions-genai` at a commit SHA.** Rejected for now.
- For: it is where GenAI development happens, and it has newer attributes.
- Against:
  - There is no release.
  - Its `schema_url` (`gen-ai-dev/1.42.0-dev`) returns 404 and covers many commits, so a
    span cannot say which definition it follows.
  - Definitions change under the same version string.

This becomes the re-pin target once a non-dev release ships.

**C. Pin core v1.44.0, the latest core release.** Rejected. `gen_ai.*` is deprecated
there; that release points to the GenAI repository for it.

**D. Track latest with no pin.** Rejected. It violates hard rule 1, and the contract would
drift silently.

## Consequences

**Positive**
- The pin is reproducible: an immutable tag, a published schema file, and a vendored
  registry snapshot checked in CI.
- Detectors depend on the profile, not on any SDK. An SDK or upstream change is absorbed
  in mapping tables and caught by fixtures and the unmapped-attribute metric.
- Hard rule 2 (no content) is enforced by the contract, not only by convention.

**Negative and risks**
- v1.41.1 GenAI definitions are deprecated in core ≥ v1.42.0. The attributes we rely on
  keep the same names in `semantic-conventions-genai` at `72e0d0a`. Upstream stability
  is still Development, so renames remain possible.
- Newer attributes, such as `gen_ai.retrieval.top_k`, are unavailable until a re-pin.
- `aegis.retrieval.doc_ids`, `aegis.retrieval.scores` and `aegis.tool.args_hash` exist
  because `any`-typed or content-bearing semconv attributes cannot be emitted portably
  without content. They are defined in the profile (issue #3).

**Re-pinning.** See "Migration trigger". A re-pin is a new profile major only when its
attribute-level diff is breaking under the compat checker rules; otherwise it is a minor
revision.

**Follow-ups**
- M1: a scheduled drift check that compares profile attributes against
  `semantic-conventions-genai@HEAD`.
- An ADR that moves the contracts to `aegis-platform/contracts` once that repository
  exists.

## Sources

Checked 2026-10-30.

- [S1] semantic-conventions v1.41.1 release — https://github.com/open-telemetry/semantic-conventions/releases/tag/v1.41.1
- [S2] semantic-conventions v1.42.0 release (GenAI move) — https://github.com/open-telemetry/semantic-conventions/releases/tag/v1.42.0
- [S3] semantic-conventions v1.44.0 release — https://github.com/open-telemetry/semantic-conventions/releases/tag/v1.44.0
- [S4] OpenTelemetry semantic conventions docs index — https://opentelemetry.io/docs/specs/semconv/
- [S5] semantic-conventions-genai `model/manifest.yaml` @ 72e0d0a — https://github.com/open-telemetry/semantic-conventions-genai/blob/72e0d0a097db3ba31ba3f33d0519401a044f6e23/model/manifest.yaml
- [S6] semantic-conventions-genai `RELEASING.md` @ 72e0d0a — https://github.com/open-telemetry/semantic-conventions-genai/blob/72e0d0a097db3ba31ba3f33d0519401a044f6e23/RELEASING.md
- [S7] Schema file 1.41.1 — https://opentelemetry.io/schemas/1.41.1
- [S8] v1.41.1 GenAI attribute registry — https://github.com/open-telemetry/semantic-conventions/blob/v1.41.1/docs/registry/attributes/gen-ai.md
- [S9] opentelemetry-instrumentation-openai-v2 on PyPI — https://pypi.org/project/opentelemetry-instrumentation-openai-v2/
- [S10] openinference-instrumentation-openai on PyPI — https://pypi.org/project/openinference-instrumentation-openai/
- [S11] OpenInference semantic conventions — https://github.com/Arize-ai/openinference/blob/main/spec/semantic_conventions.md
- [S12] opentelemetry-instrumentation-openai (OpenLLMetry) on PyPI — https://pypi.org/project/opentelemetry-instrumentation-openai/
- [S13] semantic-conventions `CHANGELOG.md` at v1.42.0 — https://github.com/open-telemetry/semantic-conventions/blob/v1.42.0/CHANGELOG.md
- [S14] v1.42.0 deprecated GenAI registry — https://github.com/open-telemetry/semantic-conventions/blob/v1.42.0/model/gen-ai/deprecated/registry-deprecated.yaml
- [S15] semantic-conventions-genai releases — https://github.com/open-telemetry/semantic-conventions-genai/releases
