"""The trace-assembler's part of the gateway-scaling report (#35, ADR-0003).

Two questions on the traffic of one bench run, both answered from what the cluster stored:
- how long from a trace's root span end to its trace.completed message (p50/p95), for the first
  assembly of a trace and, separately, for re-assemblies (the slow tail);
- of the traces that were split across gateways (#38) and kept on both, how many were
  re-assembled into one trace: a trace_summary row whose span count equals the spans stored.

Inputs are files the bench script dumped (TSV) and the JetStream stream itself (the stored
message's server timestamp is the publish time). Prints Markdown; --json writes the numbers.
"""

import argparse
import asyncio
import json
import math
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import nats as nats_client


@dataclass(frozen=True)
class Event:
    trace_id: str
    revision: int
    span_count: int
    published_ms: int


@dataclass(frozen=True)
class Lags:
    first: list[float]  # seconds, root end to the first trace.completed of a trace
    again: list[float]  # seconds, root end to every later revision


@dataclass(frozen=True)
class Rejoin:
    split_kept: int  # traces with stored spans from two or more gateways
    rejoined: int  # one summary whose span count equals the spans stored
    no_root: int  # no root span stored: the assembler waits for it (orphan)
    unassembled: int  # a root is stored but no summary exists
    mismatched: int  # a summary exists with another span count


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile (q in 0..1); NaN for no values."""
    if not values:
        return math.nan
    ordered = sorted(values)
    rank = max(1, math.ceil(q * len(ordered)))
    return ordered[rank - 1]


def completion_lags(events: Iterable[Event], root_end_ms: dict[str, int]) -> Lags:
    by_trace: dict[str, list[Event]] = defaultdict(list)
    for e in events:
        if e.trace_id in root_end_ms:
            by_trace[e.trace_id].append(e)
    first: list[float] = []
    again: list[float] = []
    for trace, evs in by_trace.items():
        for i, e in enumerate(sorted(evs, key=lambda e: e.revision)):
            lag = (e.published_ms - root_end_ms[trace]) / 1000
            (first if i == 0 else again).append(lag)
    return Lags(first, again)


def rejoin_report(
    stored: Iterable[tuple[str, str]],
    roots: set[str],
    summary_span_count: dict[str, int],
) -> Rejoin:
    """stored: (trace_id, gateway) per stored span; roots: traces with a stored root span."""
    gateways: dict[str, set[str]] = defaultdict(set)
    spans: dict[str, int] = defaultdict(int)
    for trace, gateway in stored:
        gateways[trace].add(gateway)
        spans[trace] += 1
    split = [t for t, g in gateways.items() if len(g) >= 2]
    rejoined = no_root = unassembled = mismatched = 0
    for trace in split:
        if trace not in roots:
            no_root += 1
        elif trace not in summary_span_count:
            unassembled += 1
        elif summary_span_count[trace] != spans[trace]:
            mismatched += 1
        else:
            rejoined += 1
    return Rejoin(len(split), rejoined, no_root, unassembled, mismatched)


def _fmt(x: float) -> str:
    return "n/a" if math.isnan(x) else f"{x:.2f} s"


def render(lags: Lags, rejoin: Rejoin, prefix: str) -> str:
    rows = [
        ("first assembly", lags.first),
        ("re-assembly (late spans)", lags.again),
    ]
    out = [
        "## Trace-assembler (#35, ADR-0003)",
        "",
        f"Run `{prefix}`. Lag = root span end to the trace.completed message's server timestamp.",
        "",
        "| | traces | p50 | p95 |",
        "|---|---|---|---|",
        *(
            f"| {name} | {len(v)} | {_fmt(percentile(v, 0.5))} | {_fmt(percentile(v, 0.95))} |"
            for name, v in rows
        ),
        "",
        "Traces split across gateways (#38) and kept on both:",
        "",
        "| split traces kept | re-assembled into one | no root stored | root, no summary "
        "| span count differs |",
        "|---|---|---|---|---|",
        f"| {rejoin.split_kept} | {rejoin.rejoined} | {rejoin.no_root} | {rejoin.unassembled} "
        f"| {rejoin.mismatched} |",
        "",
    ]
    return "\n".join(out)


def _tsv(path: Path) -> list[list[str]]:
    return [line.split("\t") for line in path.read_text(encoding="utf-8").splitlines() if line]


async def read_events(url: str, stream: str, prefix: str) -> list[Event]:
    # nats-py leaves its option and message types open; the client is used as Any, once.
    connect = cast("Callable[[str], Awaitable[Any]]", nats_client.connect)  # pyright: ignore[reportUnknownMemberType]  # nats-py leaves connect's options untyped
    nc: Any = await connect(url)
    try:
        js: Any = nc.jetstream()
        info = await js.stream_info(stream)
        events: list[Event] = []
        if info.state.messages:
            for seq in range(info.state.first_seq, info.state.last_seq + 1):
                msg: Any = await js.get_msg(stream, seq=seq)
                payload = json.loads(msg.data)
                if not str(payload["trace_id"]).startswith(prefix):
                    continue
                stamp = cast("datetime", msg.time)
                events.append(
                    Event(
                        payload["trace_id"],
                        int(payload["revision"]),
                        int(payload["span_count"]),
                        int(stamp.timestamp() * 1000),
                    )
                )
        return events
    finally:
        await nc.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--nats", required=True)
    ap.add_argument("--stream", default="TRACE_COMPLETED")
    ap.add_argument("--prefix", required=True, help="trace id prefix of the bench run")
    ap.add_argument("--roots", type=Path, required=True, help="TSV: trace_id, root end (ms)")
    ap.add_argument("--summaries", type=Path, required=True, help="TSV: trace_id, span_count")
    ap.add_argument("--stored", type=Path, required=True, help="TSV of stored spans (run.sh)")
    ap.add_argument("--markdown", type=Path, required=True)
    ap.add_argument("--json", type=Path, required=True)
    args = ap.parse_args()

    root_end = {r[0]: int(r[1]) for r in _tsv(args.roots)}
    summaries = {r[0]: int(r[1]) for r in _tsv(args.summaries)}
    stored = [(r[0], r[2]) for r in _tsv(args.stored)]
    events = asyncio.run(read_events(args.nats, args.stream, args.prefix))
    lags = completion_lags(events, root_end)
    rejoin = rejoin_report(stored, set(root_end), summaries)

    args.markdown.write_text(render(lags, rejoin, args.prefix), encoding="utf-8")
    args.json.write_text(
        json.dumps(
            {
                "run": args.prefix,
                "events": len(events),
                "first_assembly_s": {
                    "n": len(lags.first),
                    "p50": percentile(lags.first, 0.5),
                    "p95": percentile(lags.first, 0.95),
                },
                "re_assembly_s": {
                    "n": len(lags.again),
                    "p50": percentile(lags.again, 0.5),
                    "p95": percentile(lags.again, 0.95),
                },
                "split_traces": rejoin.__dict__,
            },
            indent=1,
            allow_nan=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
