# Architecture decision records

Each ADR records one decision: its context, the decision itself, the alternatives
considered and the consequences. Files are named `NNNN-short-title.md` and numbered in
order. A number is never reused.

## Status lifecycle

| Status | Meaning |
|---|---|
| Proposed | Drafted, under review. |
| Accepted | Merged and in force. |
| Superseded by ADR-NNNN | Replaced. The file stays, and its status points to the new ADR. |

An accepted ADR is not rewritten. To change a decision, write a new ADR that supersedes
it.

CLAUDE.md requires an ADR draft here whenever a design decision is missing, or the design
contradicts reality (a library API, a semconv change). Write the ADR instead of
improvising.

## Index

| ADR | Title | Status |
|---|---|---|
| [0001](0001-telemetry-profile-v1.md) | Telemetry profile v1 on pinned OTel GenAI semantic conventions | Accepted |
| [0002](0002-content-redaction-and-opt-in-capture.md) | Content redaction in the normalizer, and opt-in content capture | Proposed |
| [0003](0003-trace-assembler-input-clickhouse-watermark.md) | Trace-assembler input: a ClickHouse watermark instead of spans.sampled | Proposed |
| [0004](0004-clickhouse-schema-management.md) | ClickHouse schema management and the span ingest path | Proposed |
| [0005](0005-tenant-identity-at-the-first-authenticated-hop.md) | Tenant identity at the first authenticated hop | Proposed |
