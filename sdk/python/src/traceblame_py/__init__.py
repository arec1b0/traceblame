"""TraceBlame Python shim: adds aegis.* attributes on top of OpenInference/OpenLLMetry.

Content never leaves the process: the shim computes content-free features in-process and
exports spans rebuilt from a positive allowlist (docs/adr/0006-shim-side-content-handling.md).
"""

from importlib.metadata import version

from traceblame_py._provider import Shim, ShimError, instrument
from traceblame_py.args_hash import ALGORITHM_VERSION, args_hash
from traceblame_py.config import ShimConfig
from traceblame_py.markers import bundle, decision, session, set_fault_injection_id
from traceblame_py.spans import agent_span, retriever_span, tool_span

__version__ = version("traceblame-py")

__all__ = [
    "ALGORITHM_VERSION",
    "Shim",
    "ShimConfig",
    "ShimError",
    "__version__",
    "agent_span",
    "args_hash",
    "bundle",
    "decision",
    "instrument",
    "retriever_span",
    "session",
    "set_fault_injection_id",
    "tool_span",
]
