-- The params behind a detector_version (#15), written by a worker or an offline run to
-- ClickHouse before the first finding. detector_params is new in this schema (embedded copy:
-- traceblame_detectors/sql/).
-- param detector = "context_truncation"
-- param detector_version = "0.1.0+p0123abcd"
-- param code_version = "0.1.0"
-- param framework_version = "1.0.0"
-- param params = "{\"min_ratio\":0.95}"
INSERT INTO detector_params (detector, detector_version, code_version, framework_version, params, registered_at)
SELECT {detector:String}, {detector_version:String}, {code_version:String}, {framework_version:String}, {params:String}, now64(3)
