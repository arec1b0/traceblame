# pin: opentelemetry-instrumentation-openai-v2==2.4b0
# source: opentelemetry/instrumentation/openai_v2/__init__.py of that release, only what
#   tools/fixtures/capture_otel_genai.py uses. The package exists only in that script's own uv
#   environment (PEP 723), not in the workspace one; BaseInstrumentor is the workspace's.
from collections.abc import Collection
from typing import Any

from opentelemetry.instrumentation.instrumentor import BaseInstrumentor

class OpenAIInstrumentor(BaseInstrumentor):
    def __init__(self) -> None: ...
    def instrumentation_dependencies(self) -> Collection[str]: ...
    def _instrument(self, **kwargs: Any) -> None: ...
    def _uninstrument(self, **kwargs: Any) -> None: ...
