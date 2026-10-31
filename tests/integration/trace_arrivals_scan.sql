SELECT trace_id,
    max(is_root) AS has_root,
    groupUniqArray(service) AS services,
    groupUniqArray(tenant_id) AS tenants,
    max(ingested_at) AS last_arrival
FROM trace_arrivals
WHERE ingested_at > toDateTime64('2026-10-30 23:59:00.000', 3)
    AND ingested_at <= now64(3) - INTERVAL 5 SECOND
GROUP BY trace_id
ORDER BY trace_id
