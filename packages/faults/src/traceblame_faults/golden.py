"""Golden chunks of a CUAD question: the ids of the chunks that hold its answer text.

The app's chunker strips, joins and drops text, so answer offsets do not carry over; the text
does. A chunk is golden when, after whitespace normalisation, it contains a whole answer, or a
window of `window` characters of a longer one (an answer across a chunk boundary maps to each
chunk that holds a window of it). Only for eval traffic: the eval-only feature golden_in_topk
reads these ids, never the production retrieval_miss path (#16).
"""

from collections.abc import Sequence

WINDOW = 64


def _norm(text: str) -> str:
    return " ".join(text.split())


def _windows(answer: str, window: int) -> list[str]:
    if len(answer) <= window:
        return [answer]
    step = max(1, window // 4)
    starts = [*range(0, len(answer) - window + 1, step), len(answer) - window]
    return [answer[s : s + window] for s in dict.fromkeys(starts)]


def golden_chunks(
    answers: Sequence[str], chunks: Sequence[tuple[str, str]], *, window: int = WINDOW
) -> list[str]:
    """The chunk ids, in chunk order, that hold an answer or a window of it."""
    needles = [w for a in map(_norm, answers) if a for w in _windows(a, window)]
    if not needles:
        return []
    return [cid for cid, text in chunks if any(n in _norm(text) for n in needles)]


def status(*, answerable: bool, chunk_ids: Sequence[str]) -> str:
    if not answerable:
        return "none"
    return "mapped" if chunk_ids else "unmapped"
