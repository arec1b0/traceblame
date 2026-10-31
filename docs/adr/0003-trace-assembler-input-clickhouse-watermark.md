# ADR-0003: Trace-assembler input: a ClickHouse watermark instead of spans.sampled

- **Status:** Proposed
- **Date:** 2026-10-31
- **Issue:** #10 (topology), #11 (`trace_roots`); implementation #35
- **Decider:** Daniil Krizhanovskyi

## Context

Design §1.1 has the gateway export each sampled span twice: into ClickHouse `spans`, and to
the NATS JetStream stream `spans.sampled`. The trace-assembler consumes `spans.sampled`,
closes a trace at root span end + grace (30 s), writes `trace_summary` and publishes
`trace.completed`.

The collector cannot do the NATS half reliably:
- **natsexporter cannot publish to JetStream.** Contrib v0.164.1 is at development stability,
  and its README says "JetStream publishing is not yet implemented; a configured `jetstream`
  block is rejected at startup".
- **Core NATS gives no ack.** Publishing core NATS subjects into a stream configured on the
  server persists the messages, but the publisher gets no ack. The hop is then at-most-once,
  while the gateway → ClickHouse hop retries and queues.
- **Two sinks can disagree.** With two exporters, a span can reach one sink and not the other.
  The assembler would then close traces that ClickHouse lacks, or miss traces it holds.

ClickHouse is where detectors read traces anyway. The gateway's tail sampling holds a trace
until its decision (`decision_wait`, 30 s after its first span) and then releases all its
spans together. A root therefore reaches ClickHouse together with its trace, up to
`decision_wait` after the root ended. A watermark on the root's end time has to wait out that
delay; a watermark on arrival time does not.

## Decision

- **The gateway exports to ClickHouse only.** #10 already runs this way.
- **The watermark runs on arrival time.**
  - `ingested_at` is ClickHouse's clock when the exporter's insert lands (`DEFAULT now64(3)`).
  - `trace_roots` (#11, ADR-0004 section 4) is sorted and partitioned by it:
    `ORDER BY (ingested_at, trace_id)`.
  - Each poll reads the next batch in keyset order: rows after
    `(watermark_ingested_at, watermark_trace_id)` with `ingested_at <= now64(3) − grace`,
    `LIMIT batch`.
  - The upper bound uses ClickHouse's own clock, so the assembler host's clock does not
    matter.
  - The contract statement is `contract/trace_roots_read.sql`, and the integration test checks
    its `EXPLAIN`.
- **The grace is a batching margin**, start value 5 s, in config (CLAUDE.md rule 8). It covers
  two things:
  - the time between an insert's stamp and its part becoming visible;
  - one trace's release being split across consecutive exporter batches.

  The first assembly of a trace usually sees all its spans.
- **Re-assembly is the main path, not a patch.** A trace is assembled whenever spans of it
  arrive, and assembly is idempotent:
  - **Triggers.** The root's arrival triggers the first assembly. Any later arrival of the
    trace's spans triggers a re-assembly.
  - **Sources of late spans:** the tail sampler forwarding spans after a cached decision; an
    agent queue draining after a gateway outage; the second half of a trace split across
    gateways (#38), which its own gateway releases at its own decision.
  - **Arrival index.** Roots come from `trace_roots`. Later spans need an index of the same
    shape: one row per trace and insert, keyed `(ingested_at, trace_id)`, fed by a
    materialized view from every insert. It lands with the assembler change (#35), as an
    expand migration whose DDL is reviewed there.
  - **What is written:**
    - `trace_summary` is keyed by `(trace_id, assembler_version)`, with a `revision` (the
      latest `ingested_at` among the trace's spans) as the version of a `ReplacingMergeTree`.
      An identical re-assembly writes the same row; one with more spans supersedes it.
    - `trace.completed` carries `trace_id`, `revision`, the span count and the assembler
      version. It is published with JetStream publish-ack and
      `Nats-Msg-Id = trace_id/assembler_version/revision`. The stream's duplicate window drops
      a repeated revision; a new revision is a new message.
- **The watermark is persisted in Postgres.** Postgres already holds the control-plane state
  (replay_jobs).
  - There is one row per assembler partition: the two replicas are leader-free and partition
    by `cityHash64(trace_id) % N` (design §1.1 deployment).
  - The row is updated only after the batch's summaries are written and the messages acked
    (at-least-once).
  - Changing N restarts every partition from the smallest watermark. Idempotency makes the
    overlap safe.
- **NATS JetStream stays** for events our own services publish with publish-ack:
  `trace.completed`, `traceblame.finding.v1` and `traceblame.blame.v1`.
- **On acceptance, `spans.sampled` is removed** from design §1.1 and the CLAUDE.md stack line:
  - component 4: "export в ClickHouse + NATS" becomes ClickHouse only;
  - the data flow line;
  - the NATS streams in the deployment topology.

## Alternatives considered

- **A watermark on `end_ts` with a 30 s grace** (the first draft). Rejected:
  - a root arrives up to `decision_wait` after it ended, so the grace has to cover the sampling
    delay and adds it to the lag a second time;
  - late arrivals need an `ingested_at` pass anyway.
- **Core NATS publish into a server-side stream.** Rejected: at-most-once, and the sinks can
  disagree.
- **Waiting for natsexporter's JetStream support.** This is the revisit trigger below.

## Revisit trigger

Contrib `natsexporter` JetStream support reaches beta. At that point, compare a pushed
`spans.sampled` against this polling path on lag and on ClickHouse load.

## Consequences

**Query load on ClickHouse**
- **Per poll and replica:** one range scan of `trace_roots` and one of the arrival index. Both
  prune by the min-max index and the primary key, and read in order.
- **Spans:** one batched read of the batch's spans, `trace_id IN (…)`. `spans` is sorted by
  service first, so this read relies on the bloom filter index on `trace_id` (ADR-0004 D7). It
  is the dominant cost, and it grows with traces per second, not spans per second.
- **Partition filter:** each replica reads the whole range and discards the other partitions'
  rows (`cityHash64(trace_id) % N`). That is fine at demo scale; the query duration and read
  rows are metrics.

**Late spans after the watermark**
- **Re-assembly handles them**, and it costs a re-run of the detectors and the attributor for
  that trace.
  - Detectors (#15) must treat a new revision as replacing their earlier findings for the
    trace.
  - Re-assemblies and late-span delay are metrics.
- **A part that becomes visible later than the grace after its stamp is never scanned.**
  - The grace must exceed the longest insert, which the exporter's batch size bounds.
  - A reconciliation compares the traces in `trace_roots` against `trace_summary` for closed
    hours, and alerts on misses.
  - With ClickHouse replicas (the design's path to 2 replicas + Keeper), the grace must also
    cover replication lag, or the assembler reads from the inserting replica.

**Lag SLO impact**
- Pipeline lag is span end → blame written, p95 ≤ 60 s (alert PipelineLagHigh). The path is:
  1. the gateway's `decision_wait`: 30 s after the trace's first span, unchanged by this ADR;
  2. the exporter batch;
  3. the grace (5 s);
  4. the poll interval (config, start 2 s).
- The assembler sees a trace about 35–40 s after its first span ended, leaving about 20 s for
  the detectors and the attributor. The `end_ts` draft would have added another 30 s.
- Re-assembled traces are the slow tail; they are measured separately.
