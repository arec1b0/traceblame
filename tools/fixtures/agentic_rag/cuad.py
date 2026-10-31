"""CUAD, the real queries of the agentic-rag capture (#12): pin, loader, sampler, chunk ids.

The archive is pinned like cuad-doc-qa's (same commit and sha256), so both projects read one
snapshot. Queries are CUAD questions, verbatim, from the official test split; each runs over
the contract it is about, as in CUAD's own task.

Standard library only: the driver and the capture environment both import it.
"""

import hashlib
import json
import math
import shutil
import urllib.request
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

CUAD_COMMIT = "67faa0e6023b04fcaae6cc09497ab00e5d63a2a2"
CUAD_URL = f"https://raw.githubusercontent.com/TheAtticusProject/cuad/{CUAD_COMMIT}/data.zip"
CUAD_SHA256 = "f8161d18bea4e9c05e78fa6dda61c19c846fb8087ea969c172753bc2f45b999a"
SPLIT_MEMBER = "test.json"
LICENSE = "CC BY 4.0"


@dataclass(frozen=True)
class Item:
    qid: str
    title: str
    question: str
    answerable: bool
    context: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def ensure_archive(cache_dir: Path) -> Path:
    """The pinned archive, downloaded once into cache_dir and verified every time."""
    path = cache_dir / "data.zip"
    if not path.is_file():
        cache_dir.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(".part")
        with urllib.request.urlopen(CUAD_URL, timeout=60) as resp, partial.open("wb") as out:
            shutil.copyfileobj(resp, out)
        partial.replace(path)
    actual = _sha256(path)
    if actual != CUAD_SHA256:
        raise SystemExit(f"cuad: {path} has sha256 {actual}, pinned {CUAD_SHA256}")
    return path


def load_test_split(archive: Path) -> list[Item]:
    """Every question of CUAD's official test split, with its contract."""
    with zipfile.ZipFile(archive) as z:
        data = json.loads(z.read(SPLIT_MEMBER))
    return [
        Item(
            qid=qa["id"],
            title=entry["title"],
            question=qa["question"],
            answerable=bool(qa["answers"]),
            context=paragraph["context"],
        )
        for entry in data["data"]
        for paragraph in entry["paragraphs"]
        for qa in paragraph["qas"]
    ]


def _rank(seed: int, qid: str) -> str:
    return hashlib.sha256(f"{seed}:{qid}".encode()).hexdigest()


def sample(items: Sequence[Item], *, n: int, seed: int, ratio: float = 0.5) -> list[Item]:
    """ceil(n * ratio) answerable and the rest unanswerable items, answerable first.

    Each stratum is ranked by sha256(f"{seed}:{qid}") and cut at a prefix whose length only
    grows with n, so the same seed with a larger n returns a superset.
    """
    answerable = sorted((i for i in items if i.answerable), key=lambda i: _rank(seed, i.qid))
    unanswerable = sorted((i for i in items if not i.answerable), key=lambda i: _rank(seed, i.qid))
    k = math.ceil(n * ratio)
    if k > len(answerable):
        raise ValueError(f"{k} answerable items wanted, {len(answerable)} available")
    if n - k > len(unanswerable):
        raise ValueError(f"{n - k} unanswerable items wanted, {len(unanswerable)} available")
    return answerable[:k] + unanswerable[: n - k]


def doc_id(title: str, chunk: int) -> str:
    """Id of a contract chunk: a hash of the title, never its text."""
    return f"cuad-{hashlib.sha256(title.encode()).hexdigest()[:12]}-{chunk}"
