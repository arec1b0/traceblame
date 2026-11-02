"""tools/fixtures/agentic_rag/cuad.py: the CUAD pin, the stratified sampler and the chunk ids.

The sampler must be deterministic and extensible: the same seed with a larger N yields a
superset, so #19 can grow the query set to the D1 size without re-sampling (#12).
"""

import json
import zipfile
from pathlib import Path
from typing import Any

import cuad  # tools/fixtures/agentic_rag, on pytest's pythonpath as at the driver's run time
import pytest


def _items(answerable: int, unanswerable: int) -> list[cuad.Item]:
    """Items shaped like CUAD's, for the sampler's arithmetic; never used as eval data."""
    out = [
        cuad.Item(qid=f"c{i}__a", title=f"c{i}", question="q", answerable=True, context="x")
        for i in range(answerable)
    ]
    out += [
        cuad.Item(qid=f"c{i}__u", title=f"c{i}", question="q", answerable=False, context="x")
        for i in range(unanswerable)
    ]
    return out


@pytest.mark.parametrize(
    ("n", "ratio", "answerable"), [(24, 0.5, 12), (4, 0.5, 2), (5, 0.5, 3), (3, 0.25, 1)]
)
def test_sample_takes_the_ratio_of_answerable_items(n: int, ratio: float, answerable: int) -> None:
    picked = cuad.sample(_items(40, 40), n=n, seed=7, ratio=ratio)
    assert len(picked) == n
    assert sum(i.answerable for i in picked) == answerable
    assert [i.answerable for i in picked] == sorted((i.answerable for i in picked), reverse=True)


def test_larger_n_is_a_superset_with_the_same_seed() -> None:
    items = _items(60, 60)
    previous: set[str] = set()
    for n in range(2, 80, 3):
        current = {i.qid for i in cuad.sample(items, n=n, seed=7, ratio=0.5)}
        assert previous <= current, n
        previous = current


def test_sample_is_deterministic_and_seed_dependent() -> None:
    items = _items(30, 30)
    first = [i.qid for i in cuad.sample(items, n=10, seed=7)]
    assert first == [i.qid for i in cuad.sample(list(reversed(items)), n=10, seed=7)]
    assert first != [i.qid for i in cuad.sample(items, n=10, seed=8)]


def test_sample_refuses_more_than_a_stratum_has() -> None:
    with pytest.raises(ValueError, match="answerable"):
        cuad.sample(_items(2, 10), n=8, seed=7, ratio=0.5)


def test_doc_ids_carry_no_text() -> None:
    doc_id = cuad.doc_id("Some Contract Title, Inc.", 3)
    assert doc_id.startswith("cuad-")
    assert doc_id.endswith("-3")
    assert "Contract" not in doc_id
    assert len(doc_id.split("-")[1]) == 12


def test_load_test_split_reads_squad_shaped_json(tmp_path: Path) -> None:
    data: dict[str, Any] = {
        "data": [
            {
                "title": "T1",
                "paragraphs": [
                    {
                        "context": "ctx",
                        "qas": [
                            {
                                "id": "T1__A",
                                "question": "qa",
                                "answers": [{"text": "x", "answer_start": 0}],
                                "is_impossible": False,
                            },
                            {"id": "T1__B", "question": "qb", "answers": [], "is_impossible": True},
                        ],
                    }
                ],
            }
        ]
    }
    archive = tmp_path / "data.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("test.json", json.dumps(data))
    items = cuad.load_test_split(archive)
    assert [(i.qid, i.answerable, i.context) for i in items] == [
        ("T1__A", True, "ctx"),
        ("T1__B", False, "ctx"),
    ]


def test_the_pin_is_cuad_doc_qa_s() -> None:
    assert cuad.CUAD_COMMIT == "67faa0e6023b04fcaae6cc09497ab00e5d63a2a2"
    assert cuad.CUAD_SHA256 == "f8161d18bea4e9c05e78fa6dda61c19c846fb8087ea969c172753bc2f45b999a"
    assert cuad.CUAD_COMMIT in cuad.CUAD_URL
