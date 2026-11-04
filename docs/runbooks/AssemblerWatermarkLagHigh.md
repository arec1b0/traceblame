# AssemblerWatermarkLagHigh

**Alert.** `traceblame_assembler_watermark_lag_seconds > 120` (per partition) for 5 minutes
(`deploy/alerts/traceblame-assembler.rules.yml`).

**Meaning.** A partition's watermark trails ClickHouse's clock by more than 2 minutes. The
grace (5 s by default) is part of the lag by design. Traces arriving now are assembled late, so
`PipelineLagHigh` (p95 > 60 s) is next. The `partition` label says which partition.

## Steps

1. **Is anyone working on it?**
   ```promql
   sum by (outcome) (rate(traceblame_assembler_lease_claims_total[5m]))
   ```
   No `owned` at all means no replica is working: check
   `kubectl -n traceblame get pods -l app=trace-assembler`. A dead replica's lease expires after
   `ASSEMBLER_LEASE_TTL` (30 s), then a live replica takes its partitions over. In Postgres,
   `SELECT partition, owner, lease_until FROM assembler_partitions` shows who holds what.
2. **Are steps failing?**
   ```promql
   sum by (reason) (rate(traceblame_assembler_step_errors_total[5m]))
   ```
   `error`: read the assembler log (JSON, `"msg":"step failed"`, field `error`). A publish
   error means JetStream is down (`kubectl -n observability get pods -l app=nats`); the step
   retries for the publish retry budget and then replays the window, so nothing is lost.
   `lease_lost` in bursts: two replicas contest a partition, usually after a pause longer than the
   lease; harmless, but frequent bursts mean the lease TTL is too short for the load.
3. **Is it slow rather than broken?**
   ```promql
   histogram_quantile(0.95, sum by (le, query) (rate(traceblame_assembler_query_duration_seconds_bucket[5m])))
   ```
   A slow `spans` query is the look-back read with no service filter (ADR-0003 amendment):
   check `system.query_log` for rows read, and lower `ASSEMBLER_LOOKBACK` or the batch size
   `ASSEMBLER_BATCH_SIZE`. After a failed step the window is halved down to `ASSEMBLER_MIN_WINDOW`
   (`traceblame_assembler_window_seconds`, `..._window_shrinks_total`): a partition that stays at
   the floor and still fails has a window ClickHouse cannot answer even when small. A backlog is worked off in steps of `ASSEMBLER_MAX_WINDOW` without
   sleeping, so the lag shrinks by itself once the cause is gone.
4. **Is Postgres reachable?** The watermark and leases live there. If the assembler cannot claim
   or advance, steps error with `store:`. Restore Postgres; the assembler resumes from the stored
   watermark without a gap.
5. **Scale or tune.** Replicas share the partitions evenly (`ceil(partitions / live replicas)` each;
   `traceblame_assembler_live_replicas` and `..._owned_partitions` show it), so more replicas help
   only up to `ASSEMBLER_PARTITIONS`. Changing
   the count restarts every partition from the smallest watermark of the previous count
   (ADR-0003 amendment); the overlap is idempotent.
