# ADR-0003: Trace-assembler input: a ClickHouse watermark instead of spans.sampled

- **Status:** Proposed
- **Date:** 2026-10-31
- **Issue:** #10 (topology), #11 (`trace_roots`), #39 (`trace_arrivals`); implementation #35
- **Decider:** Daniil Krizhanovskyi

## Context

Design §1.1 has the gateway export each sampled span twice: into ClickHouse `spans`, and to
the NATS JetStream stream `spans.sampled`. The trace-assembler consumes `spans.sampled`. It
closes a trace at root span end + grace (30 s), writes `trace_summary` and publishes
`trace.completed`.

The collector cannot do the NATS half reliably:
- **No JetStream in natsexporter.** Contrib v0.164.1 is at development stability, and its
  README says "JetStream publishing is not yet implemented; a configured `jetstream` block is
  rejected at startup".
- **Core NATS has no ack.** Publishing core NATS subjects into a stream configured on the
  server persists the messages, but the publisher gets no ack. That makes the hop
  at-most-once, while the gateway → ClickHouse hop retries and queues.
- **Two sinks can disagree.** With two exporters, a span can reach one sink and not the other.
  The assembler would then close traces that ClickHouse lacks, or miss traces that it holds.

ClickHouse is where detectors read traces anyway. The gateway's tail sampling holds a trace
until its decision (`decision_wait`, 30 s after its first span), then releases all its spans
together. So a root reaches ClickHouse with its trace, up to `decision_wait` after it ended.
A watermark on end time would wait that out a second time; a watermark on arrival time does
not.

## Decision

- **The gateway exports to ClickHouse only.** #10 already runs this way.
- **Arrivals are recorded in `trace_arrivals`** (#39, ADR-0004 section 4).
  - A materialized view writes one row per trace, service and tenant of every exporter insert.
  - Columns: `trace_id`, `ingested_at`, `is_root` (the insert holds the trace's root span),
    `service` and `tenant_id`. Nothing else of a span.
  - `ingested_at` is ClickHouse's clock when the insert lands.
  - The table is sorted and partitioned by `ingested_at`.
- **One watermark scan per poll.**
  - `GROUP BY trace_id` over the window `(watermark, now64(3) − grace]`, returning `has_root`,
    the trace's services and tenants, and its last arrival. The integration test checks its
    `EXPLAIN` with a root and a late non-root arrival.
  - It lives in `tests/integration/trace_arrivals_scan.sql` until the assembler (#35) makes it
    a contract statement. Then the table is on main, so code N still runs on schema N-1.
  - The upper bound uses ClickHouse's clock, so the assembler host's clock does not matter.
  - The watermark advances to the window's upper bound. The window is capped (max window,
    config, start 60 s), so a backlog is worked off in steps.
- **The grace is a batching margin**, start 5 s, in config (CLAUDE.md rule 8). It covers:
  - the time between an insert's stamp and its part becoming visible;
  - one trace's release being split across consecutive exporter batches.

  So the first assembly of a trace usually sees all of it.
- **Re-assembly is the main path, not a patch.** Per trace in the window:
  - **Root arrival in the window:** assemble the trace (again, if it was assembled before).
  - **No root in the window, already assembled:** the arrivals are late spans, so
    re-assemble. Sources of late spans:
    - the tail sampler forwarding spans after a cached decision;
    - an agent queue draining after a gateway outage;
    - the other half of a trace split across gateways (#38), released at its own gateway's
      decision.
  - **No root in the window, not yet assembled:** wait. The root's arrival triggers the
    assembly, which reads all of the trace's spans. A trace whose root never arrives, such as
    a split trace whose root half was dropped, is counted as an orphan.
  - **Assembly reads the trace's spans** from `spans` with `service IN (…) AND trace_id IN (…)`,
    the leading key of `spans`.
- **Idempotent writes.**
  - `trace_summary` is keyed by `(trace_id, assembler_version)`. Its `revision` is the trace's
    last arrival, and serves as the version of a `ReplacingMergeTree`: an identical
    re-assembly writes the same row, and one with more spans supersedes it.
  - `trace.completed` carries `trace_id`, `revision`, the span count and the assembler
    version. It is published with JetStream publish-ack and
    `Nats-Msg-Id = trace_id/assembler_version/revision`. The stream's duplicate window drops a
    repeated revision; a new revision is a new message.
- **The watermark is persisted in Postgres**, which already holds control-plane state
  (replay_jobs).
  - There is one row per assembler partition. The two replicas are leader-free and partition
    by `cityHash64(trace_id) % N` (design §1.1 deployment).
  - A row is updated only after the window's summaries are written and the messages acked
    (at-least-once).
  - Changing N restarts every partition from the smallest watermark; idempotency makes the
    overlap safe.
- **`trace_arrivals` lives only for the assembly window.** The TTL starts at 2 days, by
  arrival day, not the 30 days of `spans`.
  - It must exceed the longest watermark lag (an assembler outage).
  - Changing it takes a migration (`MODIFY TTL`).
- **NATS JetStream stays** for events our own services publish with publish-ack:
  `trace.completed`, `traceblame.finding.v1` and `traceblame.blame.v1`.
- **On acceptance, `spans.sampled` is removed** from design §1.1:
  - component 4: "export в ClickHouse + NATS" becomes ClickHouse only;
  - the data flow line;
  - the NATS streams in the deployment topology;
  - the CLAUDE.md stack line.

## Alternatives considered

- **A watermark on `end_ts` with a 30 s grace** (the first draft). Rejected: a root arrives up
  to `decision_wait` after it ended. The grace would have to cover the sampling delay, adding
  it to the lag a second time, and late arrivals would need a second pass anyway.
- **Root spans only (`trace_roots`, sorted by arrival) plus a separate index for late spans.**
  Rejected: two tables and two scans for one question, "which traces got spans in this
  window". #11 shipped `trace_roots` before this ADR settled. #39 adds `trace_arrivals`, and
  the contract phase (#41) dropped `trace_roots` and its view; nothing read them.
- **Core NATS publish into a server-side stream.** Rejected: at-most-once, and the sinks can
  disagree.
- **Waiting for natsexporter's JetStream support.** Kept as the revisit trigger.

## Revisit trigger

Contrib `natsexporter` JetStream support reaches beta. Then compare a pushed `spans.sampled`
against this polling path, on lag and on ClickHouse load.

## Consequences

**Query load on ClickHouse**
- **The watermark scan.** Each poll and replica runs one range scan of `trace_arrivals` and
  aggregates it by `trace_id`. The min-max index and a binary search on the primary key prune
  it, and the rows are a few bytes each.
- **The spans read.** Each poll also does one batched read of the window's spans. `service` is
  the leading key of `spans`, so this read uses the primary key, with the bloom filter on
  `trace_id` (ADR-0004 D7) on top.
- **Partitioning.** Each replica scans the whole window and drops other partitions' traces
  (`cityHash64(trace_id) % N`). That is fine at demo scale; query duration and rows read are
  metrics.

**Late spans after the watermark**
- **Cost of re-assembly.** Re-assembly handles late spans, at the cost of a detector and
  attributor re-run for that trace. Detectors (#15) must treat a new revision as replacing
  their earlier findings for the trace. Re-assemblies, orphans and late-span delay are
  metrics.
- **Late-visible parts are never scanned.** A part that becomes visible later than the grace
  after its stamp falls behind the watermark.
  - The grace must exceed the longest insert, which the exporter's batch size bounds.
  - A reconciliation compares the traces in `trace_arrivals` against `trace_summary` for
    closed hours, and alerts on misses.
  - With ClickHouse replicas (the design's path to 2 replicas + Keeper), the grace must also
    cover replication lag, or the assembler reads from the inserting replica.

**Lag SLO impact**
- Pipeline lag is measured from span end to blame written, with p95 ≤ 60 s (alert
  PipelineLagHigh). The path is:
  1. the gateway's `decision_wait`: 30 s after the trace's first span, unchanged by this ADR;
  2. the exporter batch;
  3. the grace (5 s);
  4. the poll interval (config, start 2 s).
- The assembler sees a trace about 35–40 s after its first span ended, which leaves about 20 s
  for the detectors and the attributor. The `end_ts` draft would have added another 30 s.
- Re-assembled traces are the slow tail and are measured separately.
