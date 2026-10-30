"""Shared body of the per-SDK capture scripts: one real chat completion, exported over OTLP.

Each capture_<sdk>.py script pins its own dependencies (PEP 723 + lockfile) and imports this
module from its own directory. tools/fixtures/capture.py sets the CAPTURE_* variables and
the OTLP endpoint.
"""

import json
import os
import platform
from collections.abc import Callable
from importlib.metadata import version

from openai import OpenAI
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor


def run(instrument: Callable[[TracerProvider], object], packages: tuple[str, ...]) -> None:
    provider = TracerProvider(
        resource=Resource.create({"service.name": "traceblame-fixture-capture"})
    )
    # The endpoint comes from OTEL_EXPORTER_OTLP_ENDPOINT, set by capture.py.
    provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter()))
    instrument(provider)

    # Ollama's OpenAI-compatible API ignores the key, but the client requires one.
    client = OpenAI(base_url=os.environ["CAPTURE_OPENAI_BASE_URL"], api_key="ollama")
    client.chat.completions.create(
        model=os.environ["CAPTURE_MODEL"],
        messages=[{"role": "user", "content": os.environ["CAPTURE_PROMPT"]}],
        temperature=0,
        seed=int(os.environ["CAPTURE_SEED"]),
        max_tokens=int(os.environ["CAPTURE_MAX_TOKENS"]),
    )
    provider.shutdown()

    report = {
        "python": platform.python_version(),
        "packages": {name: version(name) for name in packages},
    }
    print(json.dumps(report))
