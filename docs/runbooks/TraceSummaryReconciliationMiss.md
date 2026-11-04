# TraceSummaryReconciliationMiss

**Alert.** `max(traceblame_assembler_reconcile_missing_traces) > 0` for 15 minutes
(`deploy/alerts/traceblame-assembler.rules.yml`).

**Meaning.** The trace-assembler reconciles the last closed hour of `trace_arrivals` against
`trace_summary` (ADR-0003). A *miss* is a trace with a root arrival and no summary row. Detectors
read `trace.completed`, so a missed trace is never analysed. `reconcile_orphan_traces` (arrivals,
no root, no summary) is a different signal: the root never arrived, which is a sampling or
routing issue (#38), not an assembler fault.

## Steps

1. **Which traces, which hour.** The gauge's hour is in the assembler log
   (`"msg":"traces with a root arrival have no summary"`, field `hour_start_ms`). List the
   misses (run in ClickHouse; `h0`, `h1` and `since` are epoch milliseconds):
   ```sql
   SELECT toString(trace_id) AS trace_id, max(ingested_at) AS last_arrival
   FROM trace_arrivals
   WHERE is_root
     AND ingested_at >= fromUnixTimestamp64Milli(h0) AND ingested_at < fromUnixTimestamp64Milli(h1)
     AND trace_id NOT IN (
       SELECT trace_id FROM trace_summary WHERE start_ts >= fromUnixTimestamp64Milli(since))
   GROUP BY trace_id ORDER BY last_arrival
   ```
2. **Is the pipeline healthy now?** Check `AssemblerWatermarkLagHigh` and
   `traceblame_assembler_step_errors_total{reason}`. A lag or errors during that hour explain it:
   fix the cause first (see that runbook).
3. **Why did the watermark pass the trace?**
   - `traceblame_assembler_skipped_traces_total{reason="root_missing"}`: the arrival said "root"
     but no root span was readable in `spans`. Look for the trace id in `spans`; if the span is
     there now, the part became visible after the scan.
   - The part became visible later than the grace after its insert stamp (ADR-0003, "late-visible
     parts"). Compare the arrival's `ingested_at` with the part's `modification_time` in
     `system.parts`. If the gap exceeds `ASSEMBLER_GRACE`, raise the grace (ConfigMap
     `assembler-config`) and roll the Deployment.
4. **Repair.** Re-assembly is idempotent. Rewind the partition's watermark to before the hour
   and let the assembler redo it; no duplicate events leave the stream inside the duplicate
   window, and consumers treat a repeated revision as a no-op:
   ```sql
   -- Postgres: the partition_count is ASSEMBLER_PARTITIONS; use the hour's start in ms
   UPDATE assembler_partitions SET watermark_ms = <h0>, epoch = epoch + 1
   WHERE partition_count = <N>;
   ```
   Bumping `epoch` fences a replica that is mid-window. The gauge returns to 0 at the next
   reconciliation after the hour is re-assembled.
5. **Close.** If the cause was the grace, note the new value in the config and the ADR-0003
   amendment. If it was a bug, open an issue with the trace ids (ids only; never span content).
