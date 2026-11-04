-- The trace-assembler's watermark and partition leases (ADR-0003 amendment, #35).
-- One row per (partition_count, partition): the partition of a trace is
-- cityHash64(trace_id) % partition_count. A replica owns a partition while lease_until is in
-- the future; epoch grows on every claim and fences the watermark advance, so a replica
-- that lost its lease cannot move the watermark. watermark_ms is ClickHouse's
-- DateTime64(3) as epoch milliseconds: the window (watermark, now - grace] is exact.
CREATE TABLE IF NOT EXISTS assembler_partitions (
    partition_count integer NOT NULL CHECK (partition_count > 0),
    partition integer NOT NULL,
    watermark_ms bigint NOT NULL,
    owner text,
    epoch bigint NOT NULL DEFAULT 0,
    lease_until timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (partition_count, partition),
    CHECK (partition >= 0 AND partition < partition_count)
);
