-- The commit point of a window: move the watermark, only for the current epoch of the owner
-- and only from the value the window started at. One row updated means committed; none means
-- another owner took the partition (or the watermark moved), and the caller must drop it.
-- $1 partition count, $2 partition, $3 owner, $4 epoch, $5 new watermark ms,
-- $6 lease TTL in seconds, $7 watermark ms the window started at.
UPDATE assembler_partitions
SET watermark_ms = $5::bigint,
    lease_until = now() + make_interval(secs => $6::double precision),
    updated_at = now()
WHERE partition_count = $1
    AND partition = $2
    AND owner = $3
    AND epoch = $4
    AND watermark_ms = $7::bigint
