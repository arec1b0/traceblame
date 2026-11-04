-- The trace-assembler replicas that are alive (ADR-0003 amendment, #35). Each replica upserts
-- its row on every poll; a replica is alive while heartbeat_at is within the lease TTL. The
-- live count sets the fair share of partitions (ceil(partitions / live)), so a replica that
-- joins late gets work and a replica that dies hands its partitions to the survivors.
CREATE TABLE IF NOT EXISTS assembler_replicas (
    owner text PRIMARY KEY,
    heartbeat_at timestamptz NOT NULL DEFAULT now()
);
