"""Where the pipeline lag is spent, stage by stage (#57).

The bench overlays (deploy/kind/collector/*-bench-stamps.yaml) stamp every span with the wall
clock at the agent's first processor, at the gateway's first processor, right after
tail_sampling and as the last processor before the ClickHouse exporter. ClickHouse records
the arrival of each insert (trace_arrivals.ingested_at). Per trace, on its root span:

1. generator_to_agent:    root end -> agent stamp (the generator's send delay, a bench artifact);
2. agent_to_gateway:      agent normalizer, loadbalancing file queue, network/mTLS;
3. gateway_to_decided:    gateway normalizer, tail_sampling decision_wait and decision tick;
4. decided_to_enqueue:    the processors after tail_sampling (expected about 0);
5. enqueue_to_clickhouse: the exporter's sending queue, the INSERT, the async-insert buffer
                          and the materialized view, until ingested_at.

All clocks are one kernel clock (kind nodes on one Docker Desktop VM). The stamps are
nanoseconds; ingested_at has millisecond resolution, which bounds stage 5.

Two modes:
- `run`: one bench run's stage table, budget verdicts, buckets of root end time and the queue
  trends from the collectors' own metrics and ClickHouse's metric_log (Markdown and JSON);
- `summary`: the runs of one issue side by side (both profiles, the overlay's distortion,
  which stages are over budget at low load and which only under stress, the sustained load).
"""

import argparse
import json
import math
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast

from assembler_report import percentile

STAGES = (
    "generator_to_agent",
    "agent_to_gateway",
    "gateway_to_decided",
    "decided_to_enqueue",
    "enqueue_to_clickhouse",
)
GENERATOR_SATURATED = "load generator saturated — not a pipeline measurement"
NS = 1_000_000_000


# --- budget ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Budget:
    stages: dict[str, float]  # p95 budget per stage, seconds
    assembler: float
    downstream: float  # detectors and attributor, not measured by this bench
    slo: float
    distortion_p50: float
    distortion_p95: float
    distortion_p95_rel: float

    @property
    def total(self) -> float:
        return sum(self.stages.values()) + self.assembler + self.downstream

    @property
    def measured_e2e(self) -> float:
        """The budget of root end -> trace.completed: the SLO minus what is not measured."""
        return self.slo - self.downstream


def _read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def read_budget(path: Path) -> Budget:
    env = _read_env(path)
    b = Budget(
        stages={s: float(env[f"BUDGET_{s.upper()}_S"]) for s in STAGES},
        assembler=float(env["BUDGET_ASSEMBLER_S"]),
        downstream=float(env["BUDGET_DETECTORS_ATTRIBUTOR_S"]),
        slo=float(env["SLO_P95_S"]),
        distortion_p50=float(env["DISTORTION_P50_S"]),
        distortion_p95=float(env["DISTORTION_P95_S"]),
        distortion_p95_rel=float(env["DISTORTION_P95_REL"]),
    )
    if b.total > b.slo + 1e-9:
        raise ValueError(f"stage budgets sum to {b.total:.2f} s, which exceeds the SLO {b.slo} s")
    return b


# --- stages ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class RootStamps:
    trace_id: str
    root_end_ns: int
    agent_ns: int | None
    gateway_ns: int | None
    decided_ns: int | None
    export_ns: int | None
    ingested_ms: int | None  # ingested_at of the insert that holds the root


@dataclass
class StageData:
    stages: dict[str, list[float]] = field(default_factory=lambda: {s: [] for s in STAGES})
    total: list[float] = field(default_factory=list[float])
    root_end_ns: list[int] = field(default_factory=list[int])
    excluded: dict[str, int] = field(
        default_factory=lambda: {"split": 0, "missing_stamp": 0, "no_arrival": 0}
    )


def stage_data(roots: Iterable[RootStamps], split: set[str]) -> StageData:
    """Per-trace stage durations (seconds) from the root span's stamps. Traces split across
    gateways take another path and are excluded, as are roots without every stamp."""
    d = StageData()
    for r in roots:
        if r.trace_id in split:
            d.excluded["split"] += 1
            continue
        stamps = (r.agent_ns, r.gateway_ns, r.decided_ns, r.export_ns)
        if any(s is None for s in stamps):
            d.excluded["missing_stamp"] += 1
            continue
        if r.ingested_ms is None:
            d.excluded["no_arrival"] += 1
            continue
        points = [
            r.root_end_ns,
            *cast("tuple[int, int, int, int]", stamps),
            r.ingested_ms * 1_000_000,
        ]
        for name, a, b in zip(STAGES, points, points[1:], strict=False):
            d.stages[name].append((b - a) / NS)
        d.total.append((points[-1] - points[0]) / NS)
        d.root_end_ns.append(r.root_end_ns)
    return d


@dataclass(frozen=True)
class StageRow:
    stage: str
    n: int
    p50: float
    p95: float
    share: float  # mean(stage) / mean(total): additive, the shares sum to 1


def _mean(v: Sequence[float]) -> float:
    return sum(v) / len(v) if v else math.nan


def stage_table(d: StageData) -> list[StageRow]:
    total = _mean(d.total)
    return [
        StageRow(
            s,
            len(v),
            percentile(v, 0.5),
            percentile(v, 0.95),
            _mean(v) / total if total and not math.isnan(total) else math.nan,
        )
        for s, v in d.stages.items()
    ]


@dataclass(frozen=True)
class Verdict:
    stage: str
    p95: float
    budget: float
    status: str  # ok | over | saturated | n/a (no stamps)


def stage_verdicts(table: Sequence[StageRow], budget: Budget, saturated: bool) -> list[Verdict]:
    out: list[Verdict] = []
    for r in table:
        b = budget.stages[r.stage]
        if r.n == 0:
            status = "n/a"
        elif r.stage == "generator_to_agent" and saturated:
            status = "saturated"
        else:
            status = "over" if r.p95 > b else "ok"
        out.append(Verdict(r.stage, r.p95, b, status))
    return out


def generator_backlog(summaries: Sequence[Mapping[str, Any]]) -> float:
    """The worst generator's p95 backlog (span end -> send), seconds; NaN if not recorded."""
    vals: list[float] = []
    for s in summaries:
        backlog = s.get("backlog_ms")
        if not isinstance(backlog, dict):
            return math.nan
        vals.append(float(cast("dict[str, Any]", backlog)["p95"]) / 1000)
    return max(vals) if vals else math.nan


def generator_saturated(summaries: Sequence[Mapping[str, Any]], budget: Budget) -> bool:
    """Stage 1 is a pipeline measurement only if the generators kept up with their schedule:
    their p95 backlog within the stage-1 budget. Not recorded counts as saturated."""
    backlog = generator_backlog(summaries)
    return math.isnan(backlog) or backlog > budget.stages["generator_to_agent"]


def e2e_verdict(p95: float, budget: Budget) -> str:
    if math.isnan(p95):
        return "n/a"
    return "ok" if p95 <= budget.measured_e2e else "over"


@dataclass(frozen=True)
class Bucket:
    start_s: int
    n: int
    total_p95: float
    stages_p95: dict[str, float]


def buckets(d: StageData, t0_ns: int, width_s: int = 30) -> list[Bucket]:
    """Stage p95 per bucket of root end time: a queue that builds up shows as a rising row."""
    idx: dict[int, list[int]] = defaultdict(list)
    for i, end in enumerate(d.root_end_ns):
        idx[int((end - t0_ns) // (width_s * NS)) * width_s].append(i)
    out: list[Bucket] = []
    for start in sorted(idx):
        rows = idx[start]
        out.append(
            Bucket(
                start,
                len(rows),
                percentile([d.total[i] for i in rows], 0.95),
                {s: percentile([d.stages[s][i] for i in rows], 0.95) for s in STAGES},
            )
        )
    return out


# --- metrics --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Sample:
    name: str
    labels: dict[str, str]
    value: float


_LINE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{(.*)\})?\s+(\S+)")
_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')


def parse_metrics(text: str) -> list[Sample]:
    """Prometheus text exposition, the subset the collector writes."""
    out: list[Sample] = []
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = _LINE.match(line)
        if not m:
            continue
        labels = dict(_LABEL.findall(m.group(3) or ""))
        try:
            value = float(m.group(4))
        except ValueError:
            continue
        out.append(Sample(m.group(1), labels, value))
    return out


def _names(name: str) -> tuple[str, str]:
    base = name.removesuffix("_total")
    return base, base + "_total"


def metric_value(samples: Iterable[Sample], name: str, **labels: str) -> float | None:
    """The sum over every series of a metric whose labels include `labels`. A counter
    matches with or without its `_total` suffix (the exporter may add it)."""
    names = _names(name)
    hits = [
        s.value
        for s in samples
        if s.name in names and all(s.labels.get(k) == v for k, v in labels.items())
    ]
    return sum(hits) if hits else None


def accepted_spans(samples: Iterable[Sample]) -> float:
    return metric_value(samples, "otelcol_receiver_accepted_spans") or 0.0


@dataclass(frozen=True)
class Scrape:
    tick: int
    ts_ns: int
    role: str  # agent | gateway
    pod: str
    samples: list[Sample]


def read_scrapes(path: Path) -> list[Scrape]:
    """run.sh writes `# SCRAPE <tick> <unix ns> <role> <pod>` before each pod's metrics."""
    out: list[Scrape] = []
    head: list[str] | None = None
    body: list[str] = []

    def flush() -> None:
        if head is not None:
            out.append(
                Scrape(int(head[0]), int(head[1]), head[2], head[3], parse_metrics("\n".join(body)))
            )

    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# SCRAPE "):
            flush()
            head, body = line.split()[2:6], []
        else:
            body.append(line)
    flush()
    return out


@dataclass(frozen=True)
class ClickHouseTick:
    ts_ms: int
    pending_async_inserts: float
    async_insert_queries: float
    failed_async_inserts: float
    insert_queries: float


def read_clickhouse(path: Path) -> list[ClickHouseTick]:
    """TSV from system.metric_log: ms, PendingAsyncInsert, AsyncInsertQuery,
    FailedAsyncInsertQuery, InsertQuery (one row per second)."""
    rows: list[ClickHouseTick] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line:
            f = line.split("\t")
            rows.append(
                ClickHouseTick(int(f[0]), float(f[1]), float(f[2]), float(f[3]), float(f[4]))
            )
    return rows


# (row label, role, metric, labels)
GAUGES: tuple[tuple[str, str, str, dict[str, str]], ...] = (
    (
        "agent loadbalancing queue (bytes)",
        "agent",
        "otelcol_exporter_queue_size",
        {"exporter": "loadbalancing"},
    ),
    (
        "gateway traces in tail_sampling",
        "gateway",
        "otelcol_processor_tail_sampling_sampling_traces_on_memory",
        {},
    ),
    (
        "gateway exporter queue (requests)",
        "gateway",
        "otelcol_exporter_queue_size",
        {"exporter": "clickhouse"},
    ),
)
DECISION_TIMER = "otelcol_processor_tail_sampling_sampling_decision_timer_latency"

# (label, role, counter): the increase over the run, summed over pods.
COUNTERS: tuple[tuple[str, str, str], ...] = (
    ("agent accepted spans", "agent", "otelcol_receiver_accepted_spans"),
    ("agent refused spans (memory_limiter)", "agent", "otelcol_receiver_refused_spans"),
    ("agent export failed spans", "agent", "otelcol_exporter_send_failed_spans"),
    ("gateway refused spans (memory_limiter)", "gateway", "otelcol_receiver_refused_spans"),
    ("gateway exported spans", "gateway", "otelcol_exporter_sent_spans"),
    ("gateway export failed spans", "gateway", "otelcol_exporter_send_failed_spans"),
    (
        "gateway traces dropped too early",
        "gateway",
        "otelcol_processor_tail_sampling_sampling_trace_dropped_too_early",
    ),
)


@dataclass(frozen=True)
class TrendRow:
    start_s: int
    values: dict[str, float]


def queue_trend(
    scrapes: Sequence[Scrape], ch: Sequence[ClickHouseTick], t0_ns: int, width_s: int = 30
) -> list[TrendRow]:
    """Per bucket: the largest per-tick sum over pods of each queue gauge, the mean decision
    timer run, and ClickHouse's pending async inserts (max) and failed async inserts (sum)."""
    rows: dict[int, dict[str, float]] = defaultdict(dict)

    def bucket(ts_ns: int) -> int:
        return int((ts_ns - t0_ns) // (width_s * NS)) * width_s

    by_tick: dict[int, list[Scrape]] = defaultdict(list)
    for s in scrapes:
        by_tick[s.tick].append(s)
    for tick in sorted(by_tick):
        group = by_tick[tick]
        b = bucket(min(s.ts_ns for s in group))
        for label, role, name, labels in GAUGES:
            vals = [metric_value(s.samples, name, **labels) for s in group if s.role == role]
            present = [v for v in vals if v is not None]
            if present:
                rows[b][label] = max(rows[b].get(label, 0.0), sum(present))

    # Decision timer: deltas of _sum and _count between consecutive scrapes of one pod.
    timer: dict[int, list[float]] = defaultdict(lambda: [0.0, 0.0])
    last: dict[str, tuple[float, float]] = {}
    for s in sorted((s for s in scrapes if s.role == "gateway"), key=lambda s: s.tick):
        total = metric_value(s.samples, DECISION_TIMER + "_sum")
        count = metric_value(s.samples, DECISION_TIMER + "_count")
        if total is None or count is None:
            continue
        if s.pod in last:
            d_sum, d_count = total - last[s.pod][0], count - last[s.pod][1]
            if d_count > 0 and d_sum >= 0:
                acc = timer[bucket(s.ts_ns)]
                acc[0] += d_sum
                acc[1] += d_count
        last[s.pod] = (total, count)
    for b, (d_sum, d_count) in timer.items():
        rows[b]["gateway decision timer run (ms, mean)"] = d_sum / d_count

    for t in ch:
        b = bucket(t.ts_ms * 1_000_000)
        rows[b]["ClickHouse pending async inserts"] = max(
            rows[b].get("ClickHouse pending async inserts", 0.0), t.pending_async_inserts
        )
        rows[b]["ClickHouse failed async inserts"] = (
            rows[b].get("ClickHouse failed async inserts", 0.0) + t.failed_async_inserts
        )
        rows[b]["ClickHouse inserts"] = rows[b].get("ClickHouse inserts", 0.0) + t.insert_queries
    return [TrendRow(b, rows[b]) for b in sorted(rows)]


def counter_increase(scrapes: Sequence[Scrape], role: str, name: str) -> float:
    """last - first per pod, summed: pods that join or leave count for the time they lived."""
    first: dict[str, float] = {}
    latest: dict[str, float] = {}
    for s in sorted(scrapes, key=lambda s: s.tick):
        if s.role != role:
            continue
        v = metric_value(s.samples, name)
        if v is None:
            continue
        first.setdefault(s.pod, v)
        latest[s.pod] = v
    return sum(latest[p] - first[p] for p in latest)


def counters(scrapes: Sequence[Scrape]) -> dict[str, float]:
    return {label: counter_increase(scrapes, role, name) for label, role, name in COUNTERS}


# --- distortion -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Pct:
    p50: float
    p95: float


@dataclass(frozen=True)
class Distortion:
    d_p50: float
    d_p95: float
    tol_p95: float
    status: str  # ok | distorting


def distortion(base: Pct, stamped: Pct, budget: Budget) -> Distortion:
    d50 = stamped.p50 - base.p50
    d95 = stamped.p95 - base.p95
    tol95 = max(budget.distortion_p95, budget.distortion_p95_rel * base.p95)
    ok = abs(d50) <= budget.distortion_p50 and abs(d95) <= tol95
    return Distortion(d50, d95, tol95, "ok" if ok else "distorting")


# --- one run --------------------------------------------------------------------------------


@dataclass(frozen=True)
class RunReport:
    name: str
    run_id: str
    data: StageData
    budget: Budget
    generator: list[dict[str, Any]]  # the loadgen summaries
    e2e: Pct  # root end -> first trace.completed
    to_clickhouse: Pct  # root end -> first arrival in ClickHouse (all traces, as #56)
    trend: list[TrendRow]
    counters: dict[str, float]
    t0_ns: int
    events: list[dict[str, Any]]
    profile: str = ""
    stamps: bool = True
    total_rate: float = 0.0


def _s(x: float) -> str:
    return "n/a" if math.isnan(x) else f"{x:.2f} s"


def _pct(x: float) -> str:
    return "n/a" if math.isnan(x) else f"{100 * x:.1f} %"


def _num(x: float) -> str:
    return f"{x:.0f}" if abs(x) >= 100 or x == int(x) else f"{x:.1f}"


def render_run(r: RunReport) -> str:
    saturated = generator_saturated(r.generator, r.budget)
    table = stage_table(r.data)
    verdicts = {v.stage: v for v in stage_verdicts(table, r.budget, saturated)}
    backlog = generator_backlog(r.generator)
    out = [
        "## Per-stage lag (#57)",
        "",
        f"Run `{r.run_id}`. Stages on the root span of each trace (root end to the ClickHouse "
        "insert that holds it). Stamps are ns from one kernel clock; `ingested_at` has a 1 ms "
        "resolution, which bounds the last stage. Budgets: "
        "`bench/gateway-scaling/stage-budget.env` (starting hypotheses from ADR-0003).",
        "",
        "| stage | traces | p50 | p95 | share | p95 budget | verdict |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in table:
        v = verdicts[row.stage]
        status = GENERATOR_SATURATED if v.status == "saturated" else v.status
        out.append(
            f"| {row.stage} | {row.n} | {_s(row.p50)} | {_s(row.p95)} | {_pct(row.share)} "
            f"| {v.budget:g} s | {status} |"
        )
    total = Pct(percentile(r.data.total, 0.5), percentile(r.data.total, 0.95))
    out += [
        f"| **root end to ClickHouse** | {len(r.data.total)} | {_s(total.p50)} | {_s(total.p95)} "
        f"| 100 % | {sum(r.budget.stages.values()):g} s | |",
        "",
        "Per-stage p95s do not add up to the end-to-end p95 (each is the 95th percentile of its "
        "own distribution); the shares are means and do add up.",
        "",
        f"Excluded: {r.data.excluded['split']} split across gateways, "
        f"{r.data.excluded['missing_stamp']} without every stamp, "
        f"{r.data.excluded['no_arrival']} without a root arrival.",
        "",
        "End to end, checked separately:",
        "",
        "| segment | p50 | p95 | budget | verdict |",
        "|---|---|---|---|---|",
        f"| root end to first arrival in ClickHouse (all traces) | {_s(r.to_clickhouse.p50)} "
        f"| {_s(r.to_clickhouse.p95)} | | |",
        f"| end to end (root end to trace.completed) | {_s(r.e2e.p50)} | {_s(r.e2e.p95)} "
        f"| {r.budget.measured_e2e:g} s (SLO {r.budget.slo:g} s minus "
        f"{r.budget.downstream:g} s for detectors and attributor, not measured) "
        f"| {e2e_verdict(r.e2e.p95, r.budget)} |",
        "",
        f"Generator backlog (span end to send, worst generator p95): {_s(backlog)}"
        + (f": **{GENERATOR_SATURATED}** for stage 1." if saturated else "."),
        "",
    ]
    bks = buckets(r.data, r.t0_ns)
    if bks:
        out += [
            "Stage p95 by root end time (30 s buckets, T = load start):",
            "",
            "| T+ | traces | " + " | ".join(STAGES) + " | total |",
            "|---|---" + "|---" * len(STAGES) + "|---|",
        ]
        out += [
            f"| {b.start_s} s | {b.n} | "
            + " | ".join(_s(b.stages_p95[s]) for s in STAGES)
            + f" | {_s(b.total_p95)} |"
            for b in bks
        ]
        out.append("")
    if r.events:
        out.append(
            "Events: "
            + "; ".join(
                f"{e['name']} at T+{(int(e['command_ns']) - r.t0_ns) / NS:.0f} s" for e in r.events
            )
            + "."
        )
        out.append("")
    if r.trend:
        labels = sorted({k for row in r.trend for k in row.values})
        out += [
            "Queue depths (max per 30 s bucket, summed over pods; scraped every 5 s):",
            "",
            "| T+ | " + " | ".join(labels) + " |",
            "|---" + "|---" * len(labels) + "|",
        ]
        out += [
            f"| {row.start_s} s | "
            + " | ".join(_num(row.values[k]) if k in row.values else "" for k in labels)
            + " |"
            for row in r.trend
        ]
        out.append("")
    if r.counters:
        out += ["Counters over the run:", "", "| counter | increase |", "|---|---|"]
        out += [f"| {k} | {_num(v)} |" for k, v in r.counters.items()]
        out.append("")
    if r.stamps:
        out += [
            "With the stamps overlay, the normalizer counts `traceblame.bench.t_*` in "
            "`traceblame_normalizer_unmapped_attrs_total` (non-`aegis.*` keys pass through); "
            "that counter is not meaningful for this run.",
            "",
        ]
    return "\n".join(out)


def _stat(v: Sequence[float]) -> dict[str, float]:
    return {"n": len(v), "p50": percentile(v, 0.5), "p95": percentile(v, 0.95)}


def write_json(r: RunReport, path: Path) -> None:
    saturated = generator_saturated(r.generator, r.budget)
    verdicts = {v.stage: v for v in stage_verdicts(stage_table(r.data), r.budget, saturated)}
    doc = {
        "name": r.name,
        "run": r.run_id,
        "profile": r.profile,
        "stamps": r.stamps,
        "total_rate": r.total_rate,
        "stages": {
            row.stage: {
                "n": row.n,
                "p50": row.p50,
                "p95": row.p95,
                "share": row.share,
                "budget": verdicts[row.stage].budget,
                "status": verdicts[row.stage].status,
            }
            for row in stage_table(r.data)
        },
        "root_end_to_clickhouse_s": _stat(r.data.total),
        "excluded": r.data.excluded,
        "to_clickhouse_s": asdict(r.to_clickhouse),
        "e2e_s": asdict(r.e2e),
        "e2e_status": e2e_verdict(r.e2e.p95, r.budget),
        "generator_backlog_p95_s": generator_backlog(r.generator),
        "generator_saturated": saturated,
        "buckets": [asdict(b) for b in buckets(r.data, r.t0_ns)],
        "trend": [asdict(t) for t in r.trend],
        "counters": r.counters,
        "events": r.events,
    }
    path.write_text(json.dumps(doc, indent=1, allow_nan=True) + "\n", encoding="utf-8")


def load_run_json(path: Path) -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(path.read_text(encoding="utf-8")))


# --- summary --------------------------------------------------------------------------------


def _over(run: Mapping[str, Any]) -> list[str]:
    stages = cast("dict[str, dict[str, Any]]", run["stages"])
    return [s for s in STAGES if stages[s]["status"] == "over"]


def render_summary(
    runs: Sequence[Mapping[str, Any]], budget: Budget, env: Mapping[str, str]
) -> str:
    low = [r for r in runs if r["profile"] == "low" and r["stamps"]]
    low_base = [r for r in runs if r["profile"] == "low" and not r["stamps"]]
    stress = [r for r in runs if r["profile"] == "stress" and r["stamps"]]
    out = [
        "# Pipeline lag per stage (issue #57)",
        "",
        "Generated by `bench/gateway-scaling/stage_report.py summary` from the per-run JSON next "
        "to this file. Each run's own report (`gateway-scaling-<date>-<run>.md`) has the "
        "per-stage table, the buckets and the queue trends.",
        "",
        "## Environment",
        "",
        "| Key | Value |",
        "|---|---|",
        *(f"| {k} | {v} |" for k, v in sorted(env.items())),
        "",
        "## Budget (p95, starting hypotheses, ADR-0003)",
        "",
        "| stage | budget |",
        "|---|---|",
        *(f"| {s} | {b:g} s |" for s, b in budget.stages.items()),
        f"| assembler (grace + poll) | {budget.assembler:g} s |",
        f"| detectors + attributor (not measured) | {budget.downstream:g} s |",
        f"| **sum** | {budget.total:g} s (SLO {budget.slo:g} s) |",
        "",
        "## Runs",
        "",
        "| run | profile | traces/s (total) | stamps | root end to ClickHouse p50 / p95 "
        "| end to end p50 / p95 | e2e verdict | generator |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in runs:
        rc = cast("dict[str, float]", r["to_clickhouse_s"])
        e2e = cast("dict[str, float]", r["e2e_s"])
        out.append(
            f"| {r['name']} | {r['profile']} | {_num(float(r['total_rate']))} "
            f"| {'yes' if r['stamps'] else 'no'} | {_s(rc['p50'])} / {_s(rc['p95'])} "
            f"| {_s(e2e['p50'])} / {_s(e2e['p95'])} | {r['e2e_status']} "
            f"| {'saturated' if r['generator_saturated'] else 'kept up'} |"
        )
    out.append("")
    stamped = [r for r in runs if r["stamps"]]
    if stamped:
        out += [
            "## Per-stage p95 and share",
            "",
            "| stage | " + " | ".join(str(r["name"]) for r in stamped) + " | budget |",
            "|---" + "|---" * len(stamped) + "|---|",
        ]
        for s in STAGES:
            cells: list[str] = []
            for r in stamped:
                st = cast("dict[str, dict[str, Any]]", r["stages"])[s]
                mark = (
                    " **over**"
                    if st["status"] == "over"
                    else (" (saturated)" if st["status"] == "saturated" else "")
                )
                cells.append(f"{_s(float(st['p95']))} ({_pct(float(st['share']))}){mark}")
            out.append(f"| {s} | " + " | ".join(cells) + f" | {budget.stages[s]:g} s |")
        out += [
            "",
            "Per-stage p95s do not add up to the end-to-end p95; shares are means and do.",
            "",
        ]
    if low and low_base:
        out += [
            "## Distortion (low, with vs without stamps)",
            "",
            "| segment | without | with | Δp50 | Δp95 | tolerance (p50 / p95) | verdict |",
            "|---|---|---|---|---|---|---|",
        ]
        for key, label in (
            ("to_clickhouse_s", "root end to first arrival in ClickHouse"),
            ("e2e_s", "root end to trace.completed"),
        ):
            a = cast("dict[str, float]", low_base[0][key])
            b = cast("dict[str, float]", low[0][key])
            d = distortion(Pct(a["p50"], a["p95"]), Pct(b["p50"], b["p95"]), budget)
            out.append(
                f"| {label} | {_s(a['p50'])} / {_s(a['p95'])} | {_s(b['p50'])} / {_s(b['p95'])} "
                f"| {d.d_p50:+.2f} s | {d.d_p95:+.2f} s | {budget.distortion_p50:g} s / "
                f"{d.tol_p95:.2f} s | {d.status} |"
            )
        out.append("")
    low_over = sorted({s for r in low for s in _over(r)})
    stress_only = sorted({s for r in stress for s in _over(r)} - set(low_over))
    out += [
        "## Verdicts",
        "",
        f"- Over budget at low steady load: {', '.join(low_over) or 'none'}.",
        f"- Over budget only under the stress profile: {', '.join(stress_only) or 'none'}.",
    ]
    for r in runs:
        if r["generator_saturated"] and r["stamps"]:
            out.append(f"- {r['name']}: stage 1 is not judged ({GENERATOR_SATURATED}).")
    ok_rates = [float(r["total_rate"]) for r in stamped if r["e2e_status"] == "ok"]
    bad_rates = [float(r["total_rate"]) for r in stamped if r["e2e_status"] == "over"]
    if ok_rates:
        out.append(
            f"- The highest tested load with end-to-end p95 within budget: {_num(max(ok_rates))} "
            f"traces/s (total)"
            + (
                f"; the lowest tested load over it: {_num(min(bad_rates))} traces/s."
                if bad_rates
                else "."
            )
        )
    else:
        out.append("- No tested load kept the end-to-end p95 within budget.")
    out.append("")
    return "\n".join(out)


# --- CLI ------------------------------------------------------------------------------------


def _tsv(path: Path) -> list[list[str]]:
    return [line.split("\t") for line in path.read_text(encoding="utf-8").splitlines() if line]


def _opt_int(s: str) -> int | None:
    return int(s) if s not in ("", "\\N") else None


def read_roots(path: Path) -> list[RootStamps]:
    """TSV: trace_id, root end (ns), the agent, gateway, decided and export stamps (ns, empty
    without the overlay), the root's ingested_at (ms, \\N without a root arrival)."""
    return [
        RootStamps(
            f[0],
            int(f[1]),
            _opt_int(f[2]),
            _opt_int(f[3]),
            _opt_int(f[4]),
            _opt_int(f[5]),
            _opt_int(f[6]),
        )
        for f in _tsv(path)
    ]


def split_traces(stored: Path) -> set[str]:
    """Traces with stored spans from two or more gateways (run.sh's stored.tsv)."""
    gws: dict[str, set[str]] = defaultdict(set)
    for f in _tsv(stored):
        gws[f[0]].add(f[2])
    return {t for t, g in gws.items() if len(g) >= 2}


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--name", required=True)
    run.add_argument("--run-id", required=True)
    run.add_argument("--profile", required=True)
    run.add_argument("--stamps", type=int, choices=(0, 1), required=True)
    run.add_argument("--total-rate", type=float, required=True)
    run.add_argument("--budget", type=Path, required=True)
    run.add_argument("--roots", type=Path, required=True)
    run.add_argument("--stored", type=Path, required=True)
    run.add_argument("--summaries", type=Path, required=True, help="loadgen summary lines")
    run.add_argument("--assembler-json", type=Path, required=True)
    run.add_argument("--metrics", type=Path, required=True)
    run.add_argument("--clickhouse", type=Path, required=True)
    run.add_argument("--events", type=Path, required=True)
    run.add_argument("--markdown", type=Path, required=True)
    run.add_argument("--json", type=Path, required=True)
    summ = sub.add_parser("summary")
    summ.add_argument("--budget", type=Path, required=True)
    summ.add_argument("--env", type=Path, required=True, help="JSON object")
    summ.add_argument("--markdown", type=Path, required=True)
    summ.add_argument("runs", type=Path, nargs="+")
    args = ap.parse_args()
    budget = read_budget(args.budget)

    if args.cmd == "summary":
        env = cast("dict[str, str]", json.loads(args.env.read_text(encoding="utf-8")))
        runs = [load_run_json(p) for p in args.runs]
        args.markdown.write_text(render_summary(runs, budget, env), encoding="utf-8")
        return 0

    summaries = _jsonl(args.summaries)
    t0 = min((int(s["t0_ns"]) for s in summaries), default=0)
    asm = load_run_json(args.assembler_json)
    first = cast("dict[str, float]", asm["first_assembly_s"])
    arrival = cast("dict[str, float]", asm["root_end_to_first_arrival_s"])
    scrapes = read_scrapes(args.metrics) if args.metrics.exists() else []
    ch = read_clickhouse(args.clickhouse) if args.clickhouse.exists() else []
    report = RunReport(
        name=args.name,
        run_id=args.run_id,
        data=stage_data(read_roots(args.roots), split_traces(args.stored)),
        budget=budget,
        generator=summaries,
        e2e=Pct(first["p50"], first["p95"]),
        to_clickhouse=Pct(arrival["p50"], arrival["p95"]),
        trend=queue_trend(scrapes, ch, t0),
        counters=counters(scrapes),
        t0_ns=t0,
        events=_jsonl(args.events),
        profile=args.profile,
        stamps=bool(args.stamps),
        total_rate=args.total_rate,
    )
    args.markdown.write_text(render_run(report), encoding="utf-8")
    write_json(report, args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
