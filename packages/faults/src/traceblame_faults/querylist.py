"""A plan's committed query list (#70): every source query with its app, its split side and the
kinds (and "clean") that drew it. Ids only, never a question's or an alert's text.

Generated once from the plan file (tools/faults/d1_queries.py) and committed, so the calibration
/ test split is fixed before any trace exists; the drivers recompute it at prepare/build and
refuse a difference. One JSON object per line, sorted by (app, query_ref), keys sorted.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from traceblame_faults import kinds, plan

FIELDS = ("app", "kinds", "query_ref", "split")
SIDES = ("", "calib", "test")
_MAX_REF = 512


class QueryListError(ValueError):
    pass


@dataclass(frozen=True)
class Row:
    app: str
    query_ref: str
    split: str
    kinds: tuple[str, ...]


def _check(row: Row) -> Row:
    if row.app not in kinds.APPS:
        raise QueryListError(f"app {row.app!r}")
    if not row.query_ref or len(row.query_ref) > _MAX_REF or not row.query_ref.isprintable():
        raise QueryListError("query_ref must be a printable dataset id")
    if row.split not in SIDES:
        raise QueryListError(f"{row.query_ref}: split {row.split!r}")
    if any(k != "clean" and k not in kinds.KINDS for k in row.kinds):
        raise QueryListError(f"{row.query_ref}: kinds {row.kinds}")
    if list(row.kinds) != sorted(set(row.kinds)):
        raise QueryListError(f"{row.query_ref}: kinds are not sorted and unique")
    return row


def build(app: str, entries: Sequence[plan.Entry], sides: Mapping[str, str]) -> list[Row]:
    """The app's rows from its plan entries and its queries' sides ({} = no split)."""
    drawn: dict[str, set[str]] = {}
    for e in entries:
        if e.app != app:
            raise QueryListError(f"an entry of {e.app} in the list of {app}")
        drawn.setdefault(e.query_ref, set()).add(e.kind or "clean")
    return [
        _check(Row(app, ref, sides.get(ref, ""), tuple(sorted(drawn[ref]))))
        for ref in sorted(drawn)
    ]


def dumps(rows: Sequence[Row]) -> str:
    ordered = sorted(rows, key=lambda r: (r.app, r.query_ref))
    keys = [(r.app, r.query_ref) for r in ordered]
    if len(set(keys)) != len(keys):
        raise QueryListError("a query is listed twice")
    return "".join(
        json.dumps(
            {"app": r.app, "kinds": list(r.kinds), "query_ref": r.query_ref, "split": r.split},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + "\n"
        for r in map(_check, ordered)
    )


def loads(text: str) -> list[Row]:
    out: list[Row] = []
    for n, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            parsed: Any = json.loads(line)
            if not isinstance(parsed, dict):
                raise QueryListError(f"want exactly {FIELDS}")
            doc = cast("dict[str, Any]", parsed)
            if sorted(doc) != list(FIELDS):
                raise QueryListError(f"want exactly {FIELDS}")
            row = _check(Row(doc["app"], doc["query_ref"], doc["split"], tuple(doc["kinds"])))
        except (QueryListError, TypeError, ValueError) as exc:
            raise QueryListError(f"line {n}: {exc}") from exc
        out.append(row)
    if dumps(out) != "".join(line + "\n" for line in text.splitlines() if line.strip()):
        raise QueryListError("not in canonical form (sorted by app and query_ref, keys sorted)")
    return out


def of_app(rows: Sequence[Row], app: str) -> list[Row]:
    return [r for r in rows if r.app == app]


def sides(rows: Sequence[Row], app: str) -> dict[str, str]:
    """query_ref -> split side, of one app."""
    return {r.query_ref: r.split for r in rows if r.app == app}


def verify(committed: Sequence[Row], computed: Sequence[Row], app: str) -> int:
    """The app's computed rows equal its committed ones; how many were compared. A difference,
    and nothing to compare, are errors."""
    want = {r.query_ref: r for r in of_app(committed, app)}
    got = {r.query_ref: r for r in of_app(computed, app)}
    if not want or not got:
        raise QueryListError(
            f"{app}: nothing to compare ({len(want)} committed, {len(got)} computed)"
        )
    missing, extra = sorted(set(want) - set(got)), sorted(set(got) - set(want))
    different = sorted(ref for ref in set(want) & set(got) if want[ref] != got[ref])
    if missing or extra or different:
        raise QueryListError(
            f"{app}: the computed query list differs from the committed one: {len(missing)}"
            f" committed queries not computed, {len(extra)} computed ones not committed,"
            f" {len(different)} with another split or other kinds (of {len(want)} committed)"
        )
    return len(want)
