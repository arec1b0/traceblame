"""The D1 calibration/test split by source query (design §1.6, #70): a pure function of the seed.

Queries with one grouping key share a side (CUAD: the question id; runbook: the task's
(scenario, parameters) tuple), so every trace of a source query is on one side. Each side gets
half the queries, and for every fault kind the number of its queries on the test side stays
within `max_kind_deviation` of half:

- groups are paired within a stratum, in the order of sha256("seed:split:key"); one group of
  a pair goes to each side, which one by sha256("seed:split-side:key");
- a stratum's leftover group goes to the side that leaves the smallest largest imbalance over
  the kinds, then the smallest imbalance of the totals.

A split that misses either bound is refused with its numbers, never returned.
"""

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from traceblame_faults import ids

SIDES = ("calib", "test")


class SplitError(ValueError):
    pass


@dataclass(frozen=True)
class Item:
    """One source query: its ref, its grouping key, its pairing stratum and the kinds (and
    "clean") that drew it."""

    ref: str
    key: str
    stratum: str
    kinds: tuple[str, ...]


@dataclass(frozen=True)
class _Group:
    key: str
    stratum: str
    kinds: Counter[str]
    size: int


def _groups(items: Sequence[Item]) -> list[_Group]:
    by_key: dict[str, list[Item]] = {}
    for item in items:
        by_key.setdefault(item.key, []).append(item)
    return [
        _Group(
            key,
            "|".join(sorted({m.stratum for m in members})),
            Counter(k for m in members for k in m.kinds),
            len(members),
        )
        for key, members in by_key.items()
    ]


def _bit(seed: int, key: str) -> int:
    return int(ids.rank(seed, "split-side", key)[:2], 16) & 1


def assign(seed: int, items: Sequence[Item], *, max_kind_deviation: int) -> dict[str, str]:
    """ref -> "calib" | "test"."""
    if not items:
        raise SplitError("no queries to split")
    refs = [i.ref for i in items]
    if len(set(refs)) != len(refs):
        raise SplitError("duplicate query refs")
    by_stratum: dict[str, list[_Group]] = {}
    for g in _groups(items):
        by_stratum.setdefault(g.stratum, []).append(g)
    side: dict[str, str] = {}
    # test minus calib, per kind and in all ("*").
    balance: Counter[str] = Counter()

    def put(g: _Group, where: str) -> None:
        side[g.key] = where
        sign = 1 if where == "test" else -1
        for kind, n in g.kinds.items():
            balance[kind] += sign * n
        balance["*"] += sign * g.size

    leftovers: list[_Group] = []
    for stratum in sorted(by_stratum):
        ranked = sorted(by_stratum[stratum], key=lambda g: ids.rank(seed, "split", g.key))
        for a, b in zip(ranked[0::2], ranked[1::2], strict=False):
            first = SIDES[_bit(seed, a.key)]
            put(a, first)
            put(b, SIDES[1 - SIDES.index(first)])
        if len(ranked) % 2:
            leftovers.append(ranked[-1])

    def cost(g: _Group, where: str) -> tuple[int, int, int]:
        sign = 1 if where == "test" else -1
        after = Counter(balance)
        for kind, n in g.kinds.items():
            after[kind] += sign * n
        after["*"] += sign * g.size
        per_kind = [abs(v) for k, v in after.items() if k != "*"]
        return (max(per_kind, default=0), abs(after["*"]), sum(per_kind))

    # The largest leftovers first: they are the hardest to offset.
    for g in sorted(leftovers, key=lambda g: (-g.size, ids.rank(seed, "split", g.key))):
        costs = {where: cost(g, where) for where in SIDES}
        if costs["calib"] == costs["test"]:
            put(g, SIDES[_bit(seed, g.key)])
        else:
            put(g, min(SIDES, key=lambda where: costs[where]))

    out = {i.ref: side[i.key] for i in items}
    found = counts(items, out)
    problems: list[str] = []
    n_test, n = found["queries"]["test"], len(items)
    if abs(n_test - n / 2) > 0.5:
        problems.append(f"queries: test {n_test} of {n}")
    for kind, per in sorted(found["kinds"].items()):
        total = per["calib"] + per["test"]
        if abs(per["test"] - total / 2) > max_kind_deviation:
            problems.append(f"{kind}: test {per['test']} of {total}")
    if problems:
        raise SplitError(
            f"no balanced split within {max_kind_deviation} of half: " + "; ".join(problems)
        )
    return out


def counts(items: Sequence[Item], sides: Mapping[str, str]) -> dict[str, Any]:
    """Queries per side, and per kind the queries on each side."""
    queries: dict[str, int] = dict.fromkeys(SIDES, 0)
    by_kind: dict[str, dict[str, int]] = {}
    for item in items:
        where = sides[item.ref]
        queries[where] += 1
        for kind in item.kinds:
            by_kind.setdefault(kind, dict.fromkeys(SIDES, 0))[where] += 1
    return {"queries": queries, "kinds": by_kind}
