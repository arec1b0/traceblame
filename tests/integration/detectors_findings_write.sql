-- The detectors' write of a trace revision's findings (#15), one INSERT for all of them as
-- parallel arrays. findings.revision and input_digest are new in this schema, so this is not a
-- contract statement yet; traceblame-detectors embeds a byte-equal copy
-- (traceblame_detectors/sql/, tests/contract/test_detectors_sql_copies.py). details is the
-- canonical evidence JSON (ids, numbers, enum values; never content).
-- param trace_id = "0af7651916cd43dd8448eb211c80319c"
-- param revision_ms = 1793700000123
-- param finding_ids = ["6f1c1f1e-8a43-5f1c-9a4e-0d1f2a3b4c5d"]
-- param span_ids = ["b7ad6b7169203331"]
-- param detectors = ["context_truncation"]
-- param detector_versions = ["0.1.0+p0123abcd"]
-- param input_digests = ["e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"]
-- param components = ["orchestration"]
-- param severities = ["warn"]
-- param scores = [0.9]
-- param details = ["{}"]
INSERT INTO findings (finding_id, trace_id, span_id, detector, detector_version, revision, input_digest, component, severity, score, details, created_at)
SELECT toUUID(f.1), {trace_id:FixedString(32)}, f.2, f.3, f.4, fromUnixTimestamp64Milli({revision_ms:Int64}), f.5, f.6, f.7, f.8, f.9, now64(3)
FROM (
    SELECT arrayJoin(arrayZip({finding_ids:Array(String)}, {span_ids:Array(String)}, {detectors:Array(String)}, {detector_versions:Array(String)}, {input_digests:Array(String)}, {components:Array(String)}, {severities:Array(String)}, {scores:Array(Float32)}, {details:Array(String)})) AS f
)
