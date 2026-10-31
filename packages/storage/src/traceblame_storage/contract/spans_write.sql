INSERT INTO otel_traces_ingest (Timestamp, TraceId, SpanId, ParentSpanId, TraceState, SpanName, SpanKind, ServiceName, ResourceAttributes, ScopeName, ScopeVersion, SpanAttributes, Duration, StatusCode, StatusMessage, Events.Timestamp, Events.Name, Events.Attributes, Links.TraceId, Links.SpanId, Links.TraceState, Links.Attributes)
VALUES ('2026-10-31 00:00:00.000000000', '0af7651916cd43dd8448eb211c80319c', 'b7ad6b7169203331', '', '', 'chat qwen2.5:7b', 'Client', 'contract-check',
        map('service.name', 'contract-check', 'service.version', '1.0.0', 'aegis.tenant.id', 'contract'),
        'opentelemetry.util.genai.handler', '0.4b0',
        map('aegis.profile.version', 'aegis-telemetry/1', 'aegis.span.kind', 'llm', 'aegis.source.sdk', 'otel-genai',
            'aegis.normalizer.version', '0.3.0', 'gen_ai.operation.name', 'chat', 'gen_ai.provider.name', 'openai',
            'gen_ai.request.model', 'qwen2.5:7b', 'gen_ai.response.finish_reasons', '["stop"]',
            'gen_ai.usage.input_tokens', '38', 'gen_ai.usage.output_tokens', '2', 'error.type', 'BadRequestError',
            'http.response.status_code', '400', 'server.port', '11434'),
        1000000, 'Error', '', [], [], [], [], [], [], [])
