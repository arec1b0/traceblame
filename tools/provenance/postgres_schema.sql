SELECT string_agg(format('%s %s (%s, %s, %s)', lpad(version::text, 4, '0'), name, phase, left(sha256, 12),
        to_char(applied_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')), '; ' ORDER BY version)
      FROM schema_migrations
