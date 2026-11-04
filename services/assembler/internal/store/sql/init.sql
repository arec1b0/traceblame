-- Create the partitions of a partition count that do not exist yet (ADR-0003 amendment).
-- $1 partition count, $2 initial watermark in epoch ms. A new count starts every partition at
-- the smallest watermark of the count that was updated last; the overlap is safe because every
-- output of a window is idempotent. With no other count the start is $2.
INSERT INTO assembler_partitions (partition_count, partition, watermark_ms)
SELECT $1::integer,
    p,
    COALESCE(
        (SELECT min(watermark_ms)
         FROM assembler_partitions
         WHERE partition_count = (
             SELECT partition_count FROM assembler_partitions
             WHERE partition_count <> $1::integer
             ORDER BY updated_at DESC
             LIMIT 1)),
        $2::bigint)
FROM generate_series(0, $1::integer - 1) AS p
ON CONFLICT (partition_count, partition) DO NOTHING
