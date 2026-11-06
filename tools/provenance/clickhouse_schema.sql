SELECT arrayStringConcat(arraySort(groupArray(concat(leftPad(toString(version), 4, '0'), ' ', name,
        ' (', phase, ', ', substring(sha256, 1, 12), ', ', formatDateTime(applied_at, '%FT%TZ', 'UTC'), ')'))), '; ')
      FROM schema_migrations
