# Mapping tables

One YAML file per SDK and source convention version: `<sdk>/<version>.yaml` (ADR-0001). The
files are data: changing a mapping never needs a code change. They are embedded in the
binary, loaded strictly (unknown keys fail), and checked against the profile at startup and
in the tests.

**Rows exist only for what a golden fixture shows.** To map a new attribute, first capture a
fixture that carries it (`make fixtures`, see `collector/testdata/fixtures/README.md`), then
add the row and the fixture's expectation in `processor_test.go`. Bump `Version` in
`embed.go`.

## Format

```yaml
sdk: openinference             # a value of aegis.source.sdk in the profile
version: "0"                   # table id within the SDK
schema_version: 1.30.0         # optional: the OTel schema this table maps; required when an
                               # SDK has several tables, which are then picked by schema_url
source: >-                     # what the rows were written against
  openinference-semantic-conventions 0.1.43 (collector/testdata/fixtures/openinference/)

match:                         # at least one of:
  scopes: [opentelemetry.instrumentation.openai.v1]   # exact scope names
  scope_prefixes: [openinference.instrumentation.]    # scope name prefixes
  markers: [openinference.span.kind]                  # any span carrying this attribute

kind:                          # optional: aegis.span.kind from a vendor attribute
  from: openinference.span.kind
  values:
    LLM: {kind: llm, operation: chat, operation_by_span_name: {Completion: text_completion}}
  default: {kind: chain}       # every other value
  # `operation` fills gen_ai.operation.name when the span has none; it must be one of the
  # kind's operations in the profile.

map:                           # profile attribute <- ordered sources; the first value wins
  - to: gen_ai.request.model   # a span attribute of the profile, not set by the normalizer
    from:
      - {json: llm.invocation_parameters, field: model}   # top-level field of a JSON string
      - attr: llm.model_name                              # an attribute, consumed (removed)
      - {event: exception, field: exception.type, on_error: true}  # span event attribute
  - to: server.address
    from:
      - attr: gen_ai.openai.api_base
        convert: url_host      # the only converter: host of a URL
        keep: true             # do not consume the source
  - to: gen_ai.response.finish_reasons
    from:
      - attr: gen_ai.response.finish_reasons   # a source may be the target: rewrite values
        values: {tool_call: tool_calls}

known:                         # passed through, not counted as unmapped
  - llm.token_count.total
  - pattern: '^embedding\.embeddings\.\d+\.embedding\.(text|vector)$'
```

Semantics:
- **Native names win.** When the span already carries the target, the sources are only
  consumed. The exception is a rule whose source is the target itself.
- **Types.** The processor coerces every mapped value to its profile type afterwards, so
  tables never convert types.
- **Profile names are always known.** Listing them in `known` is an error.
