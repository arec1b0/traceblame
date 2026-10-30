# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#     "openai==3.41.0",
#     "opentelemetry-instrumentation-openai==0.62.4",
#     "opentelemetry-exporter-otlp-proto-http",
#     "opentelemetry-sdk",
# ]
# ///
"""Capture a chat completion through OpenLLMetry instrumentation. Run via capture.py."""

from _capture_common import run
from opentelemetry.instrumentation.openai import OpenAIInstrumentor

run(
    lambda provider: OpenAIInstrumentor().instrument(tracer_provider=provider),
    packages=(
        "opentelemetry-instrumentation-openai",
        "opentelemetry-semantic-conventions-ai",
        "openai",
        "opentelemetry-sdk",
        "opentelemetry-exporter-otlp-proto-http",
    ),
)
