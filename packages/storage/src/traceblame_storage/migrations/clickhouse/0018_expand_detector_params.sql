-- The behaviour params behind every detector_version (#15). detector_version is
-- '<code semver>+p<first 8 hex of sha256(params)>', so a finding resolves to its thresholds
-- here. params is the canonical JSON of the detector's params: numbers, booleans and enum
-- values, never content. A worker refuses to start when a row of the same key holds other
-- params (a digest collision or a drifted config). No TTL: findings have none either.
CREATE TABLE IF NOT EXISTS detector_params (
    detector LowCardinality(String),
    detector_version LowCardinality(String),
    code_version LowCardinality(String),
    framework_version LowCardinality(String),
    params String,
    registered_at DateTime64(3)
) ENGINE = ReplacingMergeTree(registered_at)
ORDER BY (detector, detector_version);
