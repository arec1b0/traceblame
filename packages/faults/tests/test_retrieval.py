"""Store wrappers at the retrieval seam: what the app's retriever sees, and when a fault counts
as applied."""

from collections.abc import Mapping
from typing import Any

import pytest

from traceblame_faults import retrieval


class Store:
    """An in-memory stand-in with the app store's search signature (a test double)."""

    def __init__(self, ranked: list[tuple[str, float]]) -> None:
        self.ranked = ranked
        self.calls: list[tuple[str, int]] = []
        self.name = "store"

    def search(
        self, query: str, n_results: int = 5, filter_metadata: Mapping[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append((query, n_results))
        return [{"id": i, "similarity": s} for i, s in self.ranked[:n_results]]


RANKED = [("g1", 0.9), ("d1", 0.8), ("g2", 0.7), ("d2", 0.6), ("d3", 0.5), ("d4", 0.4), ("d5", 0.3)]


def test_remove_drops_golden_chunks_and_backfills_with_the_next_nearest() -> None:
    applied: list[dict[str, Any]] = []
    store = retrieval.GoldenRemoved(
        Store(RANKED), frozenset({"g1", "g2"}), on_applied=applied.append
    )
    out = store.search("q", n_results=5)
    assert [r["id"] for r in out] == ["d1", "d2", "d3", "d4", "d5"]
    assert applied == [{"removed_n": 2}]


def test_remove_counts_only_when_a_golden_chunk_was_in_the_results() -> None:
    applied: list[dict[str, Any]] = []
    store = retrieval.GoldenRemoved(Store(RANKED), frozenset({"d5"}), on_applied=applied.append)
    assert [r["id"] for r in store.search("q", n_results=3)] == ["g1", "d1", "g2"]
    assert applied == []


def test_distractor_replaces_the_results_with_another_contract() -> None:
    applied: list[dict[str, Any]] = []
    other = Store([("x1", 0.2), ("x2", 0.1)])
    store = retrieval.GoldenRemoved(
        Store(RANKED),
        frozenset({"g1"}),
        on_applied=applied.append,
        distractor=other,
        distractor_doc="x-contract",
    )
    assert [r["id"] for r in store.search("q", n_results=2)] == ["x1", "x2"]
    assert applied == [{"distractor_doc": "x-contract"}]
    assert other.calls == [("q", 2)]


def test_the_wrapper_delegates_everything_else() -> None:
    inner = Store(RANKED)
    assert retrieval.GoldenRemoved(inner, frozenset(), on_applied=lambda _: None).name == "store"
    assert retrieval.FailOnce(inner).name == "store"


def test_fail_once_raises_the_armed_error_on_the_next_search_only() -> None:
    inner = Store(RANKED)
    store = retrieval.FailOnce(inner)
    assert len(store.search("q", n_results=1)) == 1
    store.arm("backend_error")
    with pytest.raises(ConnectionError, match="injected"):
        store.search("q")
    assert len(store.search("q", n_results=1)) == 1
    store.arm("backend_timeout")
    with pytest.raises(TimeoutError, match="injected"):
        store.search("q")


def test_fail_once_refuses_an_unknown_mode() -> None:
    with pytest.raises(ValueError, match="mode"):
        retrieval.FailOnce(Store(RANKED)).arm("explode")
