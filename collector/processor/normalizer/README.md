# normalizer processor

Maps vendor GenAI span attributes to the `aegis-telemetry` profile (ADR-0001, adapter
layer). Component type `normalizer`, traces only, stability `development`.

```
SDK span ─▶ detect SDK ─▶ mapping table ─▶ redact content ─▶ check attributes ─▶ stamp ─▶ span
```

## What it does to every span

1. **Detect.** Pick a mapping table (`mappings/`, format in
   [mappings/README.md](mappings/README.md)):
   - by instrumentation scope name, exact or prefix;
   - else by marker attribute, e.g. `openinference.span.kind`;
   - among an SDK's tables, by the scope's `schema_url`, or else the resource's. The newest
     table at or below it wins. A `schema_url` newer than every table uses the newest table
     and counts `traceblame_normalizer_newer_schema_spans_total`.
   - No table matched: `aegis.source.sdk=unknown`. Nothing is renamed and nothing is counted
     as unmapped, which keeps non-GenAI spans (HTTP, DB) out of the drift signal.
2. **Map.** Each rule fills one profile attribute from the first source that has a value.
   A source is an attribute, a field of a JSON attribute, an `exception` event, or the status
   message, optionally narrowed by a one-group regex. On failed calls this reads
   `http.response.status_code` out of the client's "Error code: NNN" (100–599 only) before
   the message is dropped, and `error.type` is the class name alone in every SDK (#29). A profile name the SDK already emits wins over sources. Attribute sources are
   consumed (removed) unless marked `keep`.
3. **Redact** (issue #9, [ADR-0002](../../../docs/adr/0002-content-redaction-and-opt-in-capture.md)).
   - Content attributes are dropped from every span, its events, links and resource,
     whatever the SDK. They are the profile's forbidden list plus every table's `content:`
     list.
   - First, `gen_ai.tool.call.arguments` becomes `aegis.tool.args_hash`: RFC 8785 + SHA-256,
     the first 16 hex characters. A hash the shim set wins.
   - On GenAI spans (a table matched, or `gen_ai.*` present), the status message goes, every
     event but `exception` goes, and every `exception` attribute but `exception.type` goes.
4. **Kind.** `aegis.span.kind` comes from the table's kind attribute, else from
   `gen_ai.operation.name` through the profile's span kinds, else `chain`. An operation
   outside the profile is kept: the span becomes `chain` and the attribute is counted as
   invalid. The normalizer never guesses a rename.
5. **Check.** Every profile attribute is coerced to its profile type when that loses
   nothing, e.g. `"16"` → `16`, or a scalar → a one-element array. Otherwise it is dropped
   and counted. `aegis.*` keys outside the profile, or in the wrong scope, are dropped and
   counted: the namespace is closed. Everything else passes through. Attributes that
   neither the profile nor the table knows are counted as unmapped. The exception is on
   GenAI spans: those longer than `max_unmapped_value_bytes` are dropped and counted
   instead, failing closed on content no rule names yet.
6. **Stamp.** Sets `aegis.profile.version`, `aegis.span.kind`, `aegis.source.sdk` and
   `aegis.normalizer.version`, and fills a missing resource `aegis.tenant.id` from
   `tenant_id`.

A span is never dropped. The output is deterministic: attributes keep their order, and
mapped ones follow in rule order.

## Configuration

```yaml
processors:
  normalizer:
    profile: aegis-telemetry/1   # one profile per processor; v(N)/v(N-1) = two pipelines
    tenant_id: ${env:AEGIS_TENANT_ID}   # fills aegis.tenant.id when missing; empty: never
    max_label_values: 1000       # distinct label sets per counter; then attr="_other"
    max_unmapped_value_bytes: 256   # GenAI spans: longer unknown strings are dropped
    content_capture: false       # opt-in content store (ADR-0002); true is rejected for now
```

## Self-telemetry

| Metric | Labels | Meaning |
|---|---|---|
| `traceblame_normalizer_unmapped_attrs_total` | `sdk`, `attr` | passed through, unknown to profile and table: the semconv-drift signal (`UnmappedAttrsSpike`) |
| `traceblame_normalizer_newer_schema_spans_total` | `sdk`, `schema_url` | `schema_url` newer than every table of the SDK |
| `traceblame_normalizer_invalid_attrs_total` | `sdk`, `attr` | profile attributes it could not repair (see Check) |
| `traceblame_normalizer_dropped_large_values_total` | `sdk`, `attr` | unknown attributes dropped from GenAI spans for their size (see Check) |

The names follow design §1.5 exactly; the end-to-end smoke tests scrape them from the built
collector.
- Array indexes in `attr` are collapsed (`llm.tools.*.tool.json_schema`).
- Resource-level drops use `sdk="unknown"`.

The first sighting of each label set is logged once at `info`, as JSON with `trace_id`,
`sdk`, the label and `normalizer_version`. Logs carry attribute keys only, never values.

## Versioning

`Version` in `embed.go` is stamped on every span as `aegis.normalizer.version`. Bump it with
every change to a mapping table or rule, so blame stays reproducible.

The profile under `profiles/` is a byte-identical copy of `schemas/profile/`: run
`uv run traceblame-contracts gen-schema` after editing the profile, and `make contract`
checks the copy.

## Tests

- Go, in this package:
  - one expectation per golden fixture: a new fixture without an expectation fails;
  - `content_test.go`, the no-content proof. Over every fixture, including the content-on
    ones, no capture word appears in the export, no content-shaped key survives, and the
    logs stay clean. Canaries go into content attributes, events, links, status and
    resource. Real error fixtures keep only `exception.type`. Also covered:
    `aegis.tool.args_hash`, the size guard, and a fuzz target;
  - single mutations of real fixtures for unmapped, newer-schema, invalid and cardinality
    cases;
  - unit tests of the loader and the rules (`internal/`).
- `make contract`:
  - runs every fixture through `collector/cmd/normalize` and validates the result against
    the profile JSON Schema. This gate has no xfails;
  - writes `.contract/gap-report.md` with what each SDK lacks before and after.
- `make collector-smoke`: the built distribution normalizes exactly like this package, and
  exposes the counters under the names above.
