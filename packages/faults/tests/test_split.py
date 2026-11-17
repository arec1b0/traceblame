"""split.assign(): calibration/test by source query, half each, balanced per fault kind (#70)."""

from collections import Counter

import pytest

from traceblame_faults import split

KINDS = ("a", "b", "c")


def _items(n: int, *, seed: int = 3) -> list[split.Item]:
    """n queries, every one drawn clean, and each kind drawing about two thirds of them."""
    out: list[split.Item] = []
    for i in range(n):
        drawn = tuple(k for j, k in enumerate(KINDS) if (i * 7 + j * seed) % 3 != 0)
        out.append(split.Item(ref=f"q{i}", key=f"q{i}", stratum="+".join(drawn), kinds=drawn))
    return out


def _sides(items: list[split.Item], sides: dict[str, str]) -> dict[str, Counter[str]]:
    out: dict[str, Counter[str]] = {"calib": Counter(), "test": Counter()}
    for item in items:
        out[sides[item.ref]].update(item.kinds)
        out[sides[item.ref]]["*"] += 1
    return out


def test_every_query_gets_a_side_and_each_side_gets_half() -> None:
    items = _items(110)
    sides = split.assign(19, items, max_kind_deviation=2)
    assert set(sides) == {i.ref for i in items}
    assert set(sides.values()) == {"calib", "test"}
    assert Counter(sides.values()) == {"calib": 55, "test": 55}


def test_every_kind_is_within_the_deviation_of_half_on_the_test_side() -> None:
    items = _items(110)
    sides = split.assign(19, items, max_kind_deviation=2)
    by_side = _sides(items, sides)
    totals = Counter(k for i in items for k in i.kinds)
    assert len(totals) == len(KINDS)
    for kind, n in totals.items():
        assert abs(by_side["test"][kind] - n / 2) <= 2, kind


def test_the_same_seed_gives_the_same_split_whatever_the_input_order() -> None:
    items = _items(40)
    a = split.assign(19, items, max_kind_deviation=2)
    assert a == split.assign(19, list(reversed(items)), max_kind_deviation=2)
    assert a != split.assign(20, items, max_kind_deviation=2)


def test_queries_with_one_key_share_a_side() -> None:
    items = [
        split.Item(ref=f"t{i}", key=f"tuple-{i // 2}", stratum="s", kinds=("a",)) for i in range(20)
    ]
    sides = split.assign(5, items, max_kind_deviation=2)
    for i in range(0, 20, 2):
        assert sides[f"t{i}"] == sides[f"t{i + 1}"]
    assert Counter(sides.values()) == {"calib": 10, "test": 10}


def test_an_odd_total_differs_by_one() -> None:
    items = [split.Item(ref=f"q{i}", key=f"q{i}", stratum="s", kinds=("a",)) for i in range(7)]
    counts = Counter(split.assign(1, items, max_kind_deviation=2).values())
    assert sorted(counts.values()) == [3, 4]


def test_a_split_that_cannot_be_balanced_is_refused_with_its_numbers() -> None:
    # One key holds 9 of the 10 queries of kind "a": no side can hold half of them.
    items = [split.Item(ref=f"big{i}", key="big", stratum="s", kinds=("a",)) for i in range(9)]
    items.append(split.Item(ref="small", key="small", stratum="s", kinds=("a",)))
    with pytest.raises(split.SplitError, match=r"a: test \d+ of 10"):
        split.assign(1, items, max_kind_deviation=2)


def test_nothing_to_split_and_duplicate_refs_are_errors() -> None:
    with pytest.raises(split.SplitError, match="no queries"):
        split.assign(1, [], max_kind_deviation=2)
    item = split.Item(ref="q", key="q", stratum="s", kinds=("a",))
    with pytest.raises(split.SplitError, match="duplicate"):
        split.assign(1, [item, item], max_kind_deviation=2)


def test_counts_report_queries_and_kinds_per_side() -> None:
    items = _items(12)
    sides = split.assign(19, items, max_kind_deviation=2)
    counts = split.counts(items, sides)
    assert counts["queries"] == {"calib": 6, "test": 6}
    assert set(counts["kinds"]) == set(KINDS)
    for kind in KINDS:
        assert sum(counts["kinds"][kind].values()) == sum(kind in i.kinds for i in items)
