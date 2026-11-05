"""Golden chunks of a CUAD answer: the chunk ids that hold the answer text."""

from traceblame_faults import golden

CHUNKS = [
    ("c-0", "This Agreement is made between the parties.\n\nIt starts today."),
    ("c-1", "The term of this Agreement shall be five (5) years from the Effective Date."),
    ("c-2", "Either party may terminate this   Agreement upon sixty days notice."),
    (
        "c-3",
        "Governing law. This Agreement shall be governed by the laws of the State of New York, "
        "without regard to conflicts",
    ),
    (
        "c-4",
        "of law principles and any rule that would cause the application of other laws. "
        "The courts of New York shall have jurisdiction.",
    ),
]


def test_an_answer_inside_one_chunk_maps_to_it_across_whitespace() -> None:
    assert golden.golden_chunks(["five (5) years"], CHUNKS) == ["c-1"]
    assert golden.golden_chunks(["terminate this Agreement\nupon sixty days"], CHUNKS) == ["c-2"]


def test_an_answer_across_a_chunk_boundary_maps_to_each_chunk_holding_a_window() -> None:
    answer = (
        "This Agreement shall be governed by the laws of the State of New York, without regard "
        "to conflicts of law principles and any rule that would cause the application"
    )
    assert golden.golden_chunks([answer], CHUNKS, window=32) == ["c-3", "c-4"]


def test_several_answers_union_in_chunk_order() -> None:
    assert golden.golden_chunks(["sixty days", "made between"], CHUNKS) == ["c-0", "c-2"]


def test_unmapped_and_empty() -> None:
    assert golden.golden_chunks(["not in the contract"], CHUNKS) == []
    assert golden.golden_chunks([], CHUNKS) == []
    assert golden.golden_chunks(["  "], CHUNKS) == []


def test_status() -> None:
    assert golden.status(answerable=True, chunk_ids=["c-1"]) == "mapped"
    assert golden.status(answerable=True, chunk_ids=[]) == "unmapped"
    assert golden.status(answerable=False, chunk_ids=[]) == "none"
