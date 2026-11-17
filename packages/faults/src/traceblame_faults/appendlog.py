"""Append-only JSON-lines files that survive a kill (#70): the ledger and the attempt log.

A line is written in one write() on a descriptor opened with O_APPEND, then fsynced, so a
reader sees whole lines or, after a crash in the middle of a write, a torn tail: bytes after
the last newline. Nothing here rewrites or unlinks a file; repair() only cuts a torn tail off,
after copying it aside, and only a resume calls it.
"""

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path


class TornTailError(ValueError):
    """The file does not end in a newline: its last line may be half a record."""


@dataclass(frozen=True)
class Tail:
    offset: int
    length: int
    sha256: str


def tail(path: Path) -> Tail | None:
    """The bytes after the last newline, if any."""
    if not path.is_file():
        return None
    data = path.read_bytes()
    cut = data.rfind(b"\n") + 1
    if cut == len(data):
        return None
    return Tail(cut, len(data) - cut, hashlib.sha256(data[cut:]).hexdigest())


def lines(path: Path) -> list[str]:
    """The file's lines. A torn tail is refused, even one that parses: whoever wrote it never
    finished the write, so the record behind it is not complete."""
    if not path.is_file():
        return []
    found = tail(path)
    if found is not None:
        raise TornTailError(
            f"{path}: {found.length} bytes after the last newline (a torn tail): resume the run"
            " first, which copies them aside and cuts them off"
        )
    return path.read_text(encoding="utf-8").splitlines()


def append(path: Path, line: str) -> None:
    """One line, durably. Refuses a line with a newline in it and a file with a torn tail."""
    if "\n" in line or "\r" in line:
        raise ValueError("a record is one line")
    found = tail(path)
    if found is not None:
        raise TornTailError(f"{path}: a torn tail of {found.length} bytes; resume repairs it")
    created = not path.exists()
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        data = (line + "\n").encode("utf-8")
        written = os.write(fd, data)
        if written != len(data):  # pragma: no cover - a short write to a regular file
            raise OSError(f"{path}: wrote {written} of {len(data)} bytes")
        os.fsync(fd)
    finally:
        os.close(fd)
    if created:
        _fsync_dir(path.parent)


def repair(path: Path, copy: Path) -> Tail | None:
    """Cut a torn tail off after copying its bytes to `copy`; what was cut, or None."""
    found = tail(path)
    if found is None:
        return None
    data = path.read_bytes()
    fd = os.open(copy, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        os.write(fd, data[found.offset :])
        os.fsync(fd)
    finally:
        os.close(fd)
    fd = os.open(path, os.O_WRONLY)
    try:
        os.ftruncate(fd, found.offset)
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_dir(path.parent)
    return found


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
