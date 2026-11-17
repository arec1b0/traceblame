"""The committed query list (#70): ids only, canonical, compared with what a driver computes."""

import json

import pytest

from traceblame_faults import plan, querylist, scenario

RUN = "faults-20261117-s19-abc1234"


def _entries(n: int = 4) -> list[plan.Entry]:
    queries = [
        plan.Query(f"Contract {i}__Governing Law", "agentic-rag", "cuad", "answerable", "mapped")
        for i in range(n)
    ]
    return plan.plan(
        seed=19,
        eval_run_id=RUN,
        app="agentic-rag",
        queries=queries,
        counts={"tool_failure": 2, "prompt_variant": 3},
        clean=n,
        scenarios=scenario.load_all(),
    )


def _sides(n: int = 4) -> dict[str, str]:
    return {f"Contract {i}__Governing Law": "test" if i % 2 else "calib" for i in range(n)}


def test_a_list_holds_each_query_once_with_its_side_and_the_kinds_that_drew_it() -> None:
    rows = querylist.build("agentic-rag", _entries(), _sides())
    assert [r.query_ref for r in rows] == sorted(_sides())
    assert all("clean" in r.kinds for r in rows)
    assert sum("tool_failure" in r.kinds for r in rows) == 2
    assert sum("prompt_variant" in r.kinds for r in rows) == 3
    assert querylist.sides(rows, "agentic-rag") == _sides()
    assert querylist.sides(rows, "runbook-agent") == {}


def test_the_file_is_canonical_and_holds_ids_only() -> None:
    rows = querylist.build("agentic-rag", _entries(), _sides())
    text = querylist.dumps(list(reversed(rows)))
    assert text == querylist.dumps(rows)
    assert querylist.loads(text) == rows
    for line in text.splitlines():
        assert sorted(json.loads(line)) == ["app", "kinds", "query_ref", "split"]
    assert querylist.build("agentic-rag", _entries(), {})[0].split == ""


@pytest.mark.parametrize(
    ("line", "match"),
    [
        ('{"app":"agentic-rag","kinds":["clean"],"query_ref":"q","split":"test","text":"hi"}',
         "want exactly"),
        ('{"app":"other","kinds":["clean"],"query_ref":"q","split":"test"}', "app"),
        ('{"app":"agentic-rag","kinds":["clean"],"query_ref":"q","split":"dev"}', "split"),
        ('{"app":"agentic-rag","kinds":["flood"],"query_ref":"q","split":"test"}', "kinds"),
        ('{"app":"agentic-rag","kinds":["tool_failure","clean"],"query_ref":"q","split":"test"}',
         "sorted"),
        ('{"app":"agentic-rag","kinds":["clean"],"query_ref":"","split":"test"}', "query_ref"),
        ('{"split":"test","app":"agentic-rag","kinds":["clean"],"query_ref":"q"}', "canonical"),
        ("not json", "line 1"),
    ],
)  # fmt: skip
def test_a_malformed_list_is_refused(line: str, match: str) -> None:
    with pytest.raises(querylist.QueryListError, match=match):
        querylist.loads(line + "\n")


def test_a_query_listed_twice_and_an_entry_of_another_app_are_refused() -> None:
    rows = querylist.build("agentic-rag", _entries(), _sides())
    with pytest.raises(querylist.QueryListError, match="listed twice"):
        querylist.dumps([*rows, rows[0]])
    with pytest.raises(querylist.QueryListError, match="an entry of agentic-rag"):
        querylist.build("runbook-agent", _entries(), {})


def test_verify_counts_what_it_compared_and_names_every_kind_of_difference() -> None:
    rows = querylist.build("agentic-rag", _entries(), _sides())
    assert querylist.verify(rows, rows, "agentic-rag") == 4
    flipped = [querylist.Row(r.app, r.query_ref, "calib", r.kinds) for r in rows]
    with pytest.raises(querylist.QueryListError, match="2 with another split or other kinds"):
        querylist.verify(rows, flipped, "agentic-rag")
    with pytest.raises(querylist.QueryListError, match="1 committed queries not computed, 0 comp"):
        querylist.verify(rows, rows[1:], "agentic-rag")
    with pytest.raises(querylist.QueryListError, match="1 computed ones not committed"):
        querylist.verify(rows[1:], rows, "agentic-rag")
    with pytest.raises(querylist.QueryListError, match="nothing to compare"):
        querylist.verify(rows, rows, "runbook-agent")
    with pytest.raises(querylist.QueryListError, match="nothing to compare"):
        querylist.verify([], rows, "agentic-rag")
