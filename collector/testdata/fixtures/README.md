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

Every SDK mode captures every request:

| SDK | Instrumentation | Mode | Notes |
|---|---|---|---|
| `openinference` | openinference-instrumentation-openai | default | `OPENINFERENCE_HIDE_*`: content attributes hold `__REDACTED__` |
| `openllmetry` | opentelemetry-instrumentation-openai | default | `TRACELOOP_TRACE_CONTENT=false` |
| `otel-genai` | opentelemetry-instrumentation-openai-v2 | default | semconv 1.30.0 (`gen_ai.system`); scenario without suffix |
| `otel-genai` | opentelemetry-instrumentation-openai-v2 | `_latest_experimental` | `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental` |

| Request (scenario) | What the OpenAI Python client does | Model |
|---|---|---|
| `chat_basic` | `chat.completions.create`, one user message, `temperature=0`, fixed seed, `max_tokens` | `qwen2.5:7b` |
| `chat_tool_call` | the same with one function tool (`get_weather`); the model answers with a tool call | `qwen2.5:7b` |
| `chat_error` | `chat.completions.create` with `messages=[]`: HTTP 400 `invalid_request_error` | `qwen2.5:7b` |
| `embeddings_basic` | `embeddings.create` with one input | `nomic-embed-text` |

The calls go to a local Ollama server through its OpenAI-compatible API. That is why the
spans name `openai` as the provider and a local address as the server.

- **Prompts.** They are harmless public facts, and content capture is off in every SDK.
  `make contract` checks that no prompt or completion text is present.
- **Models.** Each request pins the Ollama model digest it must run on, in
  `tools/fixtures/capture.py`. The capture fails on any other model build, and the
  provenance records the model name and digest.
- **Outcomes.** The capture also fails when a request does not produce its expected outcome,
  e.g. a tool call or an HTTP 400.

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
