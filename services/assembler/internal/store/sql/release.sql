-- Give a partition up so another replica can take it: no owner, no lease, the watermark stays.
-- Only for the owner's own epoch: a replica that was fenced releases nothing.
-- $1 partition count, $2 partition, $3 owner, $4 epoch.
UPDATE assembler_partitions
SET owner = NULL,
    lease_until = NULL,
    updated_at = now()
WHERE partition_count = $1
    AND partition = $2
    AND owner = $3
    AND epoch = $4
