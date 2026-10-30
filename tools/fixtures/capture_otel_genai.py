# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#     "openai==2.54.0",
#     "opentelemetry-instrumentation-openai-v2==2.4b0",
#     "opentelemetry-util-genai==0.4b0",
#     "opentelemetry-exporter-otlp-proto-http",
#     "opentelemetry-sdk",
# ]
# ///
"""Capture a chat completion through the OTel-native openai-v2 instrumentation.

Run via capture.py, once in default semconv mode and once with
OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental.

openai is pinned to 2.x: instrumentation 2.4b0 imports httpx, which openai 3.x replaced
with httpx2, so it cannot load next to openai 3. opentelemetry-util-genai is pinned to 0.4b0,
its release-train sibling: 2.4b0 allows >=0.4b0.dev, but 1.3b0 removed a module it imports.
"""

from _capture_common import run
from opentelemetry.instrumentation.openai_v2 import OpenAIInstrumentor

run(
    lambda provider: OpenAIInstrumentor().instrument(tracer_provider=provider),
    packages=(
        "opentelemetry-instrumentation-openai-v2",
        "opentelemetry-util-genai",
        "opentelemetry-instrumentation",
        "opentelemetry-semantic-conventions",
        "openai",
        "opentelemetry-sdk",
        "opentelemetry-exporter-otlp-proto-http",
    ),
)
