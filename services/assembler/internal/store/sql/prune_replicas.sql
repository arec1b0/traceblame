-- Forget replicas that have not been seen for a long time (pods come and go).
-- $1 age in seconds.
DELETE FROM assembler_replicas WHERE heartbeat_at < now() - make_interval(secs => $1::double precision)
