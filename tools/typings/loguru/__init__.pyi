# source: loguru>=0.7.2 (arec1b0/agentic-rag requirements.txt at 0e3780d), only what
#   tools/fixtures/agentic_rag/run.py uses. run.py runs in the app's environment, where loguru
#   is installed and typed; the workspace environment does not have it.
from typing import Any

class Logger:
    def remove(self, handler_id: int | None = ...) -> None: ...
    def add(
        self, sink: Any, *, level: int | str = ..., format: str = ..., **kwargs: Any
    ) -> int: ...

logger: Logger
