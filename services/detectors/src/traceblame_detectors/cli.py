"""traceblame-detectors: offline runs, snapshot export and versions (#15). The online worker
command arrives with PR-B.

  run-offline --snapshot DIR --config FILE (--out FILE | --clickhouse-url URL) [--baselines FILE]
  snapshot-export --clickhouse-url URL --assembler-version V --out DIR (--trace-id ID ... |
                  --trace-ids-file FILE) [--postgres-url URL]
  versions --config FILE

Exit codes: 0 done, 1 a run or export failed, 2 bad usage or configuration.
"""

import argparse
import json
import logging
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from traceblame_detectors.api import Detector
from traceblame_detectors.config import ConfigError, Mode, load_config_file
from traceblame_detectors.context import StaticBaselineProvider
from traceblame_detectors.export import export_snapshot, postgres_migrations
from traceblame_detectors.logs import setup
from traceblame_detectors.offline import run_offline
from traceblame_detectors.plugins import REGISTRY
from traceblame_detectors.sinks import ClickHouseSink, JsonlSink, Sink, params_record
from traceblame_detectors.sources.snapshot import SnapshotError, SnapshotTraceSource


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="traceblame-detectors")
    sub = p.add_subparsers(dest="command", required=True)

    off = sub.add_parser("run-offline", help="run the detectors over a frozen snapshot")
    off.add_argument("--snapshot", type=Path, required=True)
    off.add_argument("--config", type=Path, required=True)
    sink = off.add_mutually_exclusive_group(required=True)
    sink.add_argument("--out", type=Path, help="JSON lines of runs (+ .params.jsonl sidecar)")
    sink.add_argument(
        "--clickhouse-url", help="write findings to ClickHouse (production mode only)"
    )
    off.add_argument("--baselines", type=Path, help="JSON list of service baselines")

    exp = sub.add_parser("snapshot-export", help="export traces into a snapshot directory")
    exp.add_argument("--clickhouse-url", required=True)
    exp.add_argument("--assembler-version", required=True)
    exp.add_argument("--out", type=Path, required=True)
    ids = exp.add_mutually_exclusive_group(required=True)
    ids.add_argument("--trace-id", action="append", default=[])
    ids.add_argument("--trace-ids-file", type=Path)
    exp.add_argument("--postgres-url", help="record the applied Postgres migrations too")
    exp.add_argument("--repo", type=Path, default=Path.cwd(), help="checkout for commit/dirty")

    ver = sub.add_parser("versions", help="print each enabled detector's version and params")
    ver.add_argument("--config", type=Path, required=True)
    return p


def main(
    argv: Sequence[str] | None = None, registry: Mapping[str, type[Detector]] = REGISTRY
) -> int:
    args = _parser().parse_args(argv)
    logger = setup()
    try:
        if args.command == "versions":
            config = load_config_file(args.config, registry)
            for d in config.detectors:
                print(json.dumps(params_record(d), sort_keys=True))
            return 0
        if args.command == "run-offline":
            return _run_offline(args, registry, logger)
        return _export(args)
    except (ConfigError, SnapshotError) as e:
        print(f"traceblame-detectors: {e}", file=sys.stderr)
        return 2


def _run_offline(
    args: argparse.Namespace, registry: Mapping[str, type[Detector]], logger: logging.Logger
) -> int:
    config = load_config_file(args.config, registry)
    if config.mode is Mode.EVAL and args.clickhouse_url:
        raise ConfigError("mode: eval writes JSON lines only (--out), never ClickHouse")
    snapshot = SnapshotTraceSource(args.snapshot)
    baseline = StaticBaselineProvider.from_file(args.baselines) if args.baselines else None
    sink: Sink
    client = None
    if args.clickhouse_url:
        from traceblame_storage.client import ClickHouseClient, parse_url

        client = ClickHouseClient(parse_url(args.clickhouse_url))
        sink = ClickHouseSink(client)
    else:
        sink = JsonlSink(args.out)
    try:
        report = run_offline(
            snapshot,
            config,
            sink,
            baseline=baseline,
            logger=logger,
        )
    finally:
        if client is not None:
            client.close()
    print(json.dumps(report.__dict__, sort_keys=True))
    return 0


def _export(args: argparse.Namespace) -> int:
    from traceblame_storage.client import ClickHouseClient, parse_url

    ids: list[str] = list(args.trace_id)
    if args.trace_ids_file:
        ids = [
            line.strip()
            for line in args.trace_ids_file.read_text("utf-8").splitlines()
            if line.strip()
        ]
    if not ids:
        print("traceblame-detectors: no trace ids", file=sys.stderr)
        return 2
    pg = postgres_migrations(args.postgres_url) if args.postgres_url else None
    client = ClickHouseClient(parse_url(args.clickhouse_url))
    try:
        manifest = export_snapshot(
            client, ids, args.assembler_version, args.out, repo=args.repo, postgres_migrations=pg
        )
    finally:
        client.close()
    print(
        json.dumps({k: manifest[k] for k in ("trace_count", "span_count", "files")}, sort_keys=True)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
