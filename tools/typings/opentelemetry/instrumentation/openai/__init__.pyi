# pin: opentelemetry-instrumentation-openai==0.62.4
# source: opentelemetry/instrumentation/openai/__init__.py of that release (OpenLLMetry), only
#   what tools/fixtures/capture_openllmetry.py uses. The package exists only in that script's
#   own uv environment (PEP 723), not in the workspace one; BaseInstrumentor is the workspace's.
from collections.abc import Callable, Collection
from typing import Any

from opentelemetry.instrumentation.instrumentor import BaseInstrumentor

class OpenAIInstrumentor(BaseInstrumentor):
    def __init__(
        self,
        enrich_assistant: bool = ...,
        exception_logger: Any = ...,
        get_common_metrics_attributes: Callable[[], dict[str, Any]] = ...,
        upload_base64_image: Any = ...,
        enable_trace_context_propagation: bool = ...,
        use_attributes: bool | None = ...,
        use_legacy_attributes: bool | None = ...,
    ) -> None: ...
    def instrumentation_dependencies(self) -> Collection[str]: ...
    def _instrument(self, **kwargs: Any) -> None: ...
    def _uninstrument(self, **kwargs: Any) -> None: ...
