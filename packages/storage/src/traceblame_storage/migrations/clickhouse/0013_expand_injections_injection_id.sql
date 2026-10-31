-- The fault injector's id for one injection: the value of aegis.fault.injection.id on the
-- span it was applied at (#36). Spans keep it in attrs; an injection joins its span on
-- (trace_id, injection_id). '' for rows written before this column (code N-1).
ALTER TABLE injections ADD COLUMN IF NOT EXISTS injection_id String DEFAULT '' AFTER trace_id;
