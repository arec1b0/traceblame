-- Register a replica as alive. $1 owner.
INSERT INTO assembler_replicas (owner, heartbeat_at) VALUES ($1, now())
ON CONFLICT (owner) DO UPDATE SET heartbeat_at = now()
