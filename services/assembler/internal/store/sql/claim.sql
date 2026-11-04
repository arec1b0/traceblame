-- Take or renew the lease of a partition. $1 partition count, $2 partition, $3 owner,
-- $4 lease TTL in seconds. The epoch grows on every takeover (and when an owner's own lease
-- had expired) and fences the watermark advance; renewing a live lease keeps it. No row comes
-- back while another owner's lease is live.
UPDATE assembler_partitions
SET owner = $3,
    epoch = CASE WHEN owner = $3 AND lease_until > now() THEN epoch ELSE epoch + 1 END,
    lease_until = now() + make_interval(secs => $4::double precision),
    updated_at = now()
WHERE partition_count = $1
    AND partition = $2
    AND (owner = $3 OR owner IS NULL OR lease_until IS NULL OR lease_until <= now())
RETURNING epoch, watermark_ms
