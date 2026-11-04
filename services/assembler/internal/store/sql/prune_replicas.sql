-- Forget replicas that have not been seen for a long time (pods come and go): the oldest rows
-- first, at most $2 per call, so a heartbeat never does an unbounded delete and the next one
-- finishes the job. Idempotent. Compared with the database clock, like the heartbeat itself.
-- $1 age in seconds (ten lease TTLs), $2 most rows to delete.
-- The outer DELETE checks the heartbeat again: a replica that heartbeats between the subquery
-- and the delete (the delete waits for its upsert's row lock) is not stale any more, and
-- READ COMMITTED re-evaluates this predicate on the updated row, not the subquery.
DELETE FROM assembler_replicas
WHERE heartbeat_at < now() - make_interval(secs => $1::double precision)
    AND owner IN (
        SELECT owner FROM assembler_replicas
        WHERE heartbeat_at < now() - make_interval(secs => $1::double precision)
        ORDER BY heartbeat_at
        LIMIT $2::integer
    )
