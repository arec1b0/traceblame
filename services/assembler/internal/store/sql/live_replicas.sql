-- How many replicas are alive: heartbeat within $1 seconds.
SELECT count(*) FROM assembler_replicas
WHERE heartbeat_at > now() - make_interval(secs => $1::double precision)
