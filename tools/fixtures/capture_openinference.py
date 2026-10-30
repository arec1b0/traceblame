# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#     "openai==3.41.0",
#     "openinference-instrumentation-openai==0.1.68",
#     "opentelemetry-exporter-otlp-proto-http",
#     "opentelemetry-sdk",
# ]
# ///
"""Capture a chat completion through OpenInference instrumentation. Run via capture.py."""

from _capture_common import run
from openinference.instrumentation.openai import OpenAIInstrumentor

run(
    lambda provider: OpenAIInstrumentor().instrument(tracer_provider=provider),
    packages=(
        "openinference-instrumentation-openai",
        "openinference-instrumentation",
        "openinference-semantic-conventions",
        "openai",
        "opentelemetry-sdk",
        "opentelemetry-exporter-otlp-proto-http",
    ),
)
