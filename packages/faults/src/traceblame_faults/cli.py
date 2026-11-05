"""traceblame-faults: scenarios, plans, ledgers and ground truth (#14).

scenarios check   load and validate every packaged scenario
plan              the injection plan of one app from a queries file (JSON lines)
check-ledger      validate a ledger; counts by status
record            a ledger into eval_traces and injections (needs the store extra)
digest            aegis.bundle.digest of prompt templates
"""

import argparse
import hashlib
import json
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from traceblame_faults import bundle, kinds, ledger, plan, scenario


def _count(value: str) -> tuple[str, int]:
    kind, sep, n = value.partition("=")
    if not sep or kind not in kinds.KINDS or not n.isdigit():
        raise argparse.ArgumentTypeError(f"--count {value!r}: want <kind>=<n>")
    return kind, int(n)


def _template(value: str) -> tuple[str, str]:
    name, sep, path = value.partition("=")
    if not sep or not name:
        raise argparse.ArgumentTypeError(f"--template {value!r}: want <name>=<file>")
    return name, Path(path).read_text(encoding="utf-8")


def _scenarios_check() -> int:
    loaded = scenario.load_all()
    out = {
        "scenarios": [
            {
                "name": sc.name,
                "version": sc.version,
                "kind": kind,
                "app": app,
                "component": kinds.KINDS[kind].component,
                "variants": sorted(sc.variants),
            }
            for (kind, app), sc in sorted(loaded.items())
        ]
    }
    print(json.dumps(out, indent=2))
    return 0


def _plan(args: argparse.Namespace) -> int:
    queries = plan.load_queries(Path(args.queries).read_text(encoding="utf-8"))
    try:
        entries = plan.plan(
            seed=args.seed,
            eval_run_id=args.eval_run_id,
            app=args.app,
            queries=queries,
            counts=dict(args.count or []),
            clean=args.clean,
            scenarios=scenario.load_all(),
        )
    except (plan.PlanError, ValueError) as exc:
        raise SystemExit(f"plan: {exc}") from exc
    text = plan.dumps(entries)
    Path(args.out).write_text(text, encoding="utf-8")
    summary = {
        "entries": len(entries),
        "by_kind": dict(Counter(e.kind or "clean" for e in entries)),
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
    }
    print(json.dumps(summary))
    return 0


def _check_ledger(args: argparse.Namespace) -> int:
    records = ledger.read(Path(args.ledger))
    counts = Counter(ledger.status(r) for r in records)
    print(
        json.dumps(
            {
                "records": len(records),
                "clean": counts["clean"],
                "applied": counts["applied"],
                "not_reached": counts["not_reached"],
            }
        )
    )
    return 0


def _record(args: argparse.Namespace) -> int:  # pragma: no cover - live ClickHouse (integration)
    from traceblame_faults import record
    from traceblame_storage.client import ClickHouseClient, parse_url

    records = ledger.read(Path(args.ledger))
    client = ClickHouseClient(parse_url(args.url))
    try:
        print(json.dumps(record.record(client, records)))
    finally:
        client.close()
    return 0


def _digest(args: argparse.Namespace) -> int:
    print(bundle.digest(args.app, dict(args.template)))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="traceblame-faults", description=cast("str", __doc__).splitlines()[0]
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sc = sub.add_parser("scenarios")
    sc.add_argument("action", choices=["check"])
    p = sub.add_parser("plan")
    p.add_argument("--app", choices=kinds.APPS, required=True)
    p.add_argument("--queries", required=True, help="JSON lines of plan.Query")
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--eval-run-id", required=True)
    p.add_argument("--count", type=_count, action="append", help="<kind>=<n>, repeatable")
    p.add_argument("--clean", type=int, default=0)
    p.add_argument("--out", required=True)
    c = sub.add_parser("check-ledger")
    c.add_argument("--ledger", required=True)
    r = sub.add_parser("record")
    r.add_argument("--ledger", required=True)
    r.add_argument("--url", required=True, help="http(s)://user:pass@host:port/database")
    d = sub.add_parser("digest")
    d.add_argument("--app", choices=kinds.APPS, required=True)
    d.add_argument("--template", type=_template, action="append", required=True)
    args = parser.parse_args(argv)
    if args.command == "scenarios":
        return _scenarios_check()
    if args.command == "plan":
        return _plan(args)
    if args.command == "check-ledger":
        return _check_ledger(args)
    if args.command == "record":
        return _record(args)
    return _digest(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
