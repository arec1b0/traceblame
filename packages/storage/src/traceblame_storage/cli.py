"""traceblame-storage: check, apply and inspect the forward-only ClickHouse migrations."""

import argparse
import sys
from collections.abc import Sequence

from traceblame_storage.client import ClickHouseClient, parse_url
from traceblame_storage.migrations import at_revision, check, packaged
from traceblame_storage.runner import migrate, status


def _check(args: argparse.Namespace) -> int:
    migrations = packaged()
    base = at_revision(args.base_ref) if args.base_ref else []
    problems = check(migrations, base)
    for p in problems:
        print(p, file=sys.stderr)
    if problems:
        return 1
    against = f" against {args.base_ref}" if args.base_ref else ""
    print(f"{len(migrations)} migration(s) ok{against}")
    return 0


def _migrate(args: argparse.Namespace) -> int:
    client = ClickHouseClient(parse_url(args.url))
    try:
        done = migrate(client, packaged())
    finally:
        client.close()
    for m in done:
        print(f"applied {m.filename}")
    print(f"{len(done)} migration(s) applied")
    return 0


def _status(args: argparse.Namespace) -> int:
    client = ClickHouseClient(parse_url(args.url))
    try:
        rows = status(client, packaged())
    finally:
        client.close()
    for m, state in rows:
        print(f"{state:8} {m.filename}")
    return 1 if any(state == "changed" for _, state in rows) else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="traceblame-storage", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    chk = sub.add_parser("check", help="static rules; with --base-ref, applied files unchanged")
    chk.add_argument("--base-ref", help="git revision whose migrations count as applied")
    chk.set_defaults(func=_check)

    for name, func, text in (
        ("migrate", _migrate, "apply pending migrations"),
        ("status", _status, "list applied, pending and changed migrations"),
    ):
        p = sub.add_parser(name, help=text)
        p.add_argument("--url", required=True, help="http(s)://user:pass@host:port/database")
        p.set_defaults(func=func)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
