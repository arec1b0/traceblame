-- Attributor verdicts (design §1.1), one per (service, trace_id, attributor_version).
CREATE TABLE IF NOT EXISTS blame (
    trace_id FixedString(32),
    component LowCardinality(String),
    confidence Float32,
    status LowCardinality(String),
    evidence String,
    attributor_version LowCardinality(String),
    bundle_digest String,
    service LowCardinality(String),
    created_at DateTime64(3)
) ENGINE = ReplacingMergeTree(created_at)
ORDER BY (service, trace_id, attributor_version);
