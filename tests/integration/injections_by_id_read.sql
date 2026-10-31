SELECT i.injection_id, i.fault_kind, s.span_id
FROM injections AS i
INNER JOIN spans AS s
    ON s.trace_id = i.trace_id AND s.attrs['aegis.fault.injection.id'] = i.injection_id
WHERE i.trace_id = '0af7651916cd43dd8448eb211c80319c'
