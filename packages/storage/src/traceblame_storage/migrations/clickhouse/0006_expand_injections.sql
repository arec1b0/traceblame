-- Ground truth of the fault injector (design §1.1).
CREATE TABLE IF NOT EXISTS injections (
    trace_id FixedString(32),
    fault_kind LowCardinality(String),
    target_component LowCardinality(String),
    params String,
    seed UInt32,
    injected_at DateTime64(3)
) ENGINE = MergeTree
ORDER BY trace_id;
