INSERT INTO findings (finding_id, trace_id, span_id, detector, detector_version, component, severity, score, details, created_at)
VALUES (generateUUIDv4(), '0af7651916cd43dd8448eb211c80319c', 'b7ad6b7169203331', 'context_truncation', '0.1.0', 'orchestration', 'warn', 0.9, '{}', now64(3))
