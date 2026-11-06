-- The params registered for detector versions (#15): a worker compares them with its own
-- before it writes, and fails on a difference. Embedded copy: traceblame_detectors/sql/.
-- param detector_versions = ["0.1.0+p0123abcd"]
SELECT detector, detector_version, code_version, framework_version, params
FROM detector_params FINAL
WHERE detector_version IN {detector_versions:Array(String)}
