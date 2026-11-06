-- The detectors' commit marker of a trace revision (#15): one row per detector run, written
-- after the findings. detector_runs is new in this schema, so this is not a contract statement
-- yet (embedded copy: traceblame_detectors/sql/).
-- param trace_id = "0af7651916cd43dd8448eb211c80319c"
-- param revision_ms = 1793700000123
-- param assembler_version = "1.0.0"
-- param framework_version = "1.0.0"
-- param detectors = ["context_truncation"]
-- param detector_versions = ["0.1.0+p0123abcd"]
-- param input_digests = ["e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"]
-- param context_digests = ["4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"]
-- param statuses = ["ok"]
-- param error_kinds = [""]
-- param finding_counts = [1]
-- param findings_digests = ["e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"]
INSERT INTO detector_runs (trace_id, detector, detector_version, revision, input_digest, context_digest, framework_version, assembler_version, status, error_kind, finding_count, findings_digest, ran_at)
SELECT {trace_id:FixedString(32)}, r.1, r.2, fromUnixTimestamp64Milli({revision_ms:Int64}), r.3, r.4, {framework_version:String}, {assembler_version:String}, r.5, r.6, r.7, r.8, now64(3)
FROM (
    SELECT arrayJoin(arrayZip({detectors:Array(String)}, {detector_versions:Array(String)}, {input_digests:Array(String)}, {context_digests:Array(String)}, {statuses:Array(String)}, {error_kinds:Array(String)}, {finding_counts:Array(UInt16)}, {findings_digests:Array(String)})) AS r
)
