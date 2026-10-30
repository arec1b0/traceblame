# Golden span fixtures (dataset D4)

These are real OTLP/JSON exports of a real LLM call, one directory per instrumentation SDK
and version:

```
<sdk>/<instrumentation-package>-<version>/<scenario>.otlp.json        the export, as the collector wrote it
<sdk>/<instrumentation-package>-<version>/<scenario>.provenance.yaml  how it was captured
```

Nothing here is written by hand. The provenance records `fixture_sha256`, so
`make contract` fails if a fixture is edited after capture. Pre-commit does not touch this
directory, for the same reason.

## What is captured

| SDK | Instrumentation | Scenario | Notes |
|---|---|---|---|
| `openinference` | openinference-instrumentation-openai | `chat_basic` | `OPENINFERENCE_HIDE_*`: content attributes hold `__REDACTED__` |
| `openllmetry` | opentelemetry-instrumentation-openai | `chat_basic` | `TRACELOOP_TRACE_CONTENT=false` |
| `otel-genai` | opentelemetry-instrumentation-openai-v2 | `chat_basic` | default semconv mode (schema 1.30.0, `gen_ai.system`) |
| `otel-genai` | opentelemetry-instrumentation-openai-v2 | `chat_basic_latest_experimental` | `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental` |

Every capture makes the same non-streaming `chat.completions.create` call with the OpenAI
Python client: `temperature=0`, a fixed seed and `max_tokens`. The call goes to a local
Ollama server (`qwen2.5:7b`) through its OpenAI-compatible API. That is why the spans name
`openai` as the provider and a private address as the server. Content capture is off in
every SDK, and `make contract` checks that no prompt or completion text is present.

## Re-capturing

From WSL2 or Linux, with Ollama running and `qwen2.5:7b` pulled:

```bash
make fixtures                                        # localhost, then the WSL2 host gateway
make fixtures OLLAMA_URL=http://<host>:11434         # explicit server
```

`make fixtures` installs the pinned `otelcol-contrib` binary (tools/versions.env, sha256 in
tools/checksums.txt) and runs [tools/fixtures/capture.py](../../../tools/fixtures/capture.py).
For each capture, the driver:

1. starts the collector (OTLP/HTTP on 127.0.0.1:4318, file exporter);
2. runs the SDK's PEP 723 script with its lockfile (`uv run --locked --script`);
3. stops the collector and writes the fixture and its provenance.

Re-capturing changes trace ids, timestamps and hashes, so commit the fixtures and their
provenance together.
