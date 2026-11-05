"""The injection plan: a pure function of the seed, the queries, the counts and the scenarios.

Per kind, the eligible queries are ranked by sha256("seed:kind:query_ref") and the first n
taken; the clean ones by sha256("seed:clean:query_ref"). A query gets at most one entry per
kind (and one clean), so every trace of a source query stays on one side of the D1 split by
query (design §1.6). Entries are grouped by query, in the order of sha256("seed:query_ref").
Each entry's params come from entry_rng(seed, query_ref, kind) alone.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from traceblame_faults import ids, kinds, scenario

GOLDEN_STATUSES = ("mapped", "unmapped", "none")


class PlanError(ValueError):
    pass


@dataclass(frozen=True)
class Query:
    """One source query: a CUAD question or a runbook task. ref is the dataset's own id."""

    ref: str
    app: str
    source: str
    stratum: str
    golden_status: str
    golden_doc_ids: tuple[str, ...] = ()

    def with_(self, **changes: Any) -> "Query":
        return replace(self, **changes)


@dataclass(frozen=True)
class Entry:
    query_ref: str
    app: str
    kind: str  # "" = clean
    scenario: str
    params: Mapping[str, Any] = field(default_factory=dict[str, Any])
    injection_id: str = ""
    seed: int = 0


def plan(
    *,
    seed: int,
    eval_run_id: str,
    app: str,
    queries: Sequence[Query],
    counts: Mapping[str, int],
    clean: int,
    scenarios: Mapping[tuple[str, str], scenario.Scenario],
    exclusive: bool = False,
) -> list[Entry]:
    """exclusive: every query gets at most one entry in all (runbook tasks, whose sandbox
    workloads cannot run twice cheaply); clean first, then the kinds in name order."""
    ids.check(eval_run_id, "eval_run_id")
    if any(q.app != app for q in queries):
        raise PlanError(f"every query must be for app {app}")
    refs = [q.ref for q in queries]
    if len(set(refs)) != len(refs):
        raise PlanError("duplicate query refs")
    entries: list[Entry] = []
    pool = sorted(queries, key=lambda q: ids.rank(seed, "clean", q.ref))
    if clean > len(pool):
        raise PlanError(f"clean: {clean} wanted, {len(pool)} queries")
    entries += [Entry(q.ref, app, "", "", {}, "", seed) for q in pool[:clean]]
    used = {e.query_ref for e in entries} if exclusive else set[str]()
    for kind in sorted(counts):
        n = counts[kind]
        if kind not in kinds.KINDS or app not in kinds.KINDS[kind].apps:
            raise PlanError(f"{kind}: not a fault kind of {app}")
        sc = scenarios.get((kind, app))
        if sc is None:
            raise PlanError(f"{kind}: no scenario for {app}")
        eligible = sorted(
            (q for q in queries if scenario.eligible(sc, q) and q.ref not in used),
            key=lambda q: ids.rank(seed, kind, q.ref),
        )
        if n > len(eligible):
            raise PlanError(f"{kind}: {n} wanted, {len(eligible)} eligible queries")
        for q in eligible[:n]:
            if exclusive:
                used.add(q.ref)
            params = scenario.draw(sc, ids.entry_rng(seed, q.ref, kind))
            kinds.check_params(kind, params)
            entries.append(
                Entry(
                    q.ref,
                    app,
                    kind,
                    sc.name,
                    params,
                    ids.injection_id(eval_run_id, kind, q.ref),
                    seed,
                )
            )
    entries.sort(key=lambda e: (ids.rank(seed, e.query_ref), e.kind))
    return entries


def dumps(entries: Sequence[Entry]) -> str:
    return "".join(
        json.dumps(asdict(e), sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
        for e in entries
    )


def loads(text: str) -> list[Entry]:
    out: list[Entry] = []
    for line in text.splitlines():
        if line.strip():
            d = json.loads(line)
            out.append(Entry(**d))
    return out


def dump_queries(queries: Sequence[Query]) -> str:
    return "".join(
        json.dumps(asdict(q), sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
        for q in queries
    )


def load_queries(text: str) -> list[Query]:
    out: list[Query] = []
    for line in text.splitlines():
        if line.strip():
            d = json.loads(line)
            d["golden_doc_ids"] = tuple(d.get("golden_doc_ids", ()))
            q = Query(**d)
            if q.golden_status not in GOLDEN_STATUSES:
                raise PlanError(f"query {q.ref}: golden_status {q.golden_status!r}")
            out.append(q)
    return out
