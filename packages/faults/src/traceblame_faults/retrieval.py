"""Store wrappers at the retrieval seam of agentic-rag (its ChromaStore.search signature).

GoldenRemoved (golden_context_removed, ground truth retrieval): the retriever gets results
without the golden chunks, backfilled with the next nearest of the same contract ("remove"),
or the nearest chunks of another contract ("distractor"). on_applied is called when the
results differ from what the store returned: the marker goes on that retriever span.

FailOnce (tool_failure, ground truth tool): the next search raises a backend error, which the
app's retriever turns into a FAILURE result fed back to the model.
"""

from collections.abc import Callable, Mapping
from typing import Any, Protocol

ERRORS: Mapping[str, Callable[[], BaseException]] = {
    "backend_error": lambda: ConnectionError("vector store unavailable (injected)"),
    "backend_timeout": lambda: TimeoutError("vector store timed out (injected)"),
}


class Store(Protocol):
    def search(
        self, query: str, n_results: int = 5, filter_metadata: Mapping[str, Any] | None = None
    ) -> list[dict[str, Any]]: ...


class GoldenRemoved:
    def __init__(
        self,
        inner: Store,
        golden: frozenset[str],
        *,
        on_applied: Callable[[dict[str, Any]], None],
        distractor: Store | None = None,
        distractor_doc: str = "",
    ) -> None:
        self._inner = inner
        self._golden = golden
        self._on_applied = on_applied
        self._distractor = distractor
        self._distractor_doc = distractor_doc

    def search(
        self, query: str, n_results: int = 5, filter_metadata: Mapping[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        if self._distractor is not None:
            out = self._distractor.search(query, n_results, filter_metadata)
            self._on_applied({"distractor_doc": self._distractor_doc})
            return out
        wide = self._inner.search(query, n_results + len(self._golden), filter_metadata)
        removed = sum(1 for r in wide[:n_results] if r["id"] in self._golden)
        if removed:
            self._on_applied({"removed_n": removed})
        return [r for r in wide if r["id"] not in self._golden][:n_results]

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class FailOnce:
    def __init__(self, inner: Store) -> None:
        self._inner = inner
        self._armed: str | None = None

    def arm(self, mode: str) -> None:
        if mode not in ERRORS:
            raise ValueError(f"mode {mode!r} is not one of {sorted(ERRORS)}")
        self._armed = mode

    def search(
        self, query: str, n_results: int = 5, filter_metadata: Mapping[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        if self._armed is not None:
            mode, self._armed = self._armed, None
            raise ERRORS[mode]()
        return self._inner.search(query, n_results, filter_metadata)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)
