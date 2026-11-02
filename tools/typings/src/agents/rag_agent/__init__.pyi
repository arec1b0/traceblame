# source: arec1b0/agentic-rag at 0e3780d (the commit of the #12 fixtures): src/agents/rag_agent.py
#   and src/agents/base_agent.py, only what tools/fixtures/agentic_rag/run.py uses. The app
#   checkout is on sys.path only when the driver runs (CAPTURE_APP_DIR).
from typing import Any

class AgentStep:
    action: str | None

class AgentResponse:
    answer: str
    steps: list[AgentStep]

class _ToolSelector:
    # The app returns BaseTool | None; the driver relies on the retriever being registered.
    def get_tool(self, tool_name: str) -> Any: ...

class RAGAgent:
    vector_store: Any
    tool_selector: _ToolSelector
    def __init__(
        self,
        vector_store: Any,
        name: str = ...,
        max_iterations: int = ...,
        enable_tracing: bool = ...,
        enable_evaluation: bool = ...,
        **kwargs: Any,
    ) -> None: ...
    async def run(self, query: str, context: dict[str, Any] | None = ...) -> AgentResponse: ...
