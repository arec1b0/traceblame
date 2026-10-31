SELECT ingested_at, trace_id, root_span_id, service, tenant_id, start_ts, end_ts, status_code
FROM trace_roots
WHERE ingested_at >= toDateTime64('2026-10-30 23:59:00.000', 3)
    AND (ingested_at > toDateTime64('2026-10-30 23:59:00.000', 3)
        OR trace_id > '00000000000000000000000000000000')
    AND ingested_at <= now64(3) - INTERVAL 5 SECOND
ORDER BY ingested_at, trace_id
LIMIT 1000
