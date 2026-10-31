SELECT trace_id, root_span_id, service, tenant_id, end_ts
FROM trace_roots FINAL
WHERE end_ts > toDateTime64('2026-10-30 23:59:00', 6) AND end_ts <= now64(6) - INTERVAL 30 SECOND
ORDER BY end_ts, trace_id
