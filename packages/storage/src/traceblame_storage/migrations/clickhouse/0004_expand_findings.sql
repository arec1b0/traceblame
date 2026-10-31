-- Detector findings (design §1.1), idempotent by (trace_id, detector, detector_version)
-- per span (CLAUDE.md hard rule 3).
CREATE TABLE IF NOT EXISTS findings (
    finding_id UUID,
    trace_id FixedString(32),
    span_id FixedString(16),
    detector LowCardinality(String),
    detector_version LowCardinality(String),
    component LowCardinality(String),
    severity Enum8('info' = 1, 'warn' = 2, 'error' = 3),
    score Float32,
    details String,
    created_at DateTime64(3)
) ENGINE = ReplacingMergeTree(created_at)
ORDER BY (trace_id, detector, detector_version, span_id);
