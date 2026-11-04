# trace-assembler

Go service (design §1.1, [ADR-0003](../../docs/adr/0003-trace-assembler-input-clickhouse-watermark.md)).
It polls the ClickHouse arrival watermark, writes `trace_summary`, publishes `trace.completed`
and keeps the watermark in Postgres.

| Path | What |
|---|---|
| `cmd/assembler` | The binary: config, wiring, `/metrics`, `/healthz`, `/readyz`. |
| `internal/poll` | The loop: one window of one partition per step, the pure summary builder. Tests use fakes of ClickHouse, JetStream and the watermark table. |
| `internal/ch` | ClickHouse: the watermark scan, the spans read, `trace_summary` writes, reconciliation. `sql/` holds copies of the storage contract's statements (`scripts/sync-assembler-sql.sh`). |
| `internal/store` | Postgres: `assembler_partitions` (watermark, lease, epoch). `sql/` is the source of those statements. |
| `internal/natsx` | JetStream: the stream with its duplicate window, publish-ack with `Nats-Msg-Id`. |
| `internal/config` | Every threshold from the environment (`ASSEMBLER_*`, rule 8), validated at startup. |
| `internal/metrics`, `internal/reconcile` | Self-telemetry (`traceblame_assembler_*`) and the hourly reconciliation of arrivals against summaries. |
| `testdata/trace_completed.golden.json` | The event as published; validated against `schemas/events/trace.completed.v1.schema.json`. |

**One step**, per partition: claim the lease; scan `(w0, w1]`; read the spans; write the
summaries; publish with `Nats-Msg-Id = trace_id/assembler_version/revision`; advance the
watermark, fenced by the lease epoch. The advance is the commit point; every earlier step is
idempotent, so a crash or a second owner repeats work and nothing else.

```bash
make assembler                                  # services/assembler/_build/assembler
cd services/assembler && GOWORK=off go test -race ./...
make integration                                # with ClickHouse, Postgres and NATS containers
```
