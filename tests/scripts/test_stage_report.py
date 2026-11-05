import math
from pathlib import Path

import pytest
import stage_report as sr

S = 1_000_000_000  # ns per second
BUDGET_ENV = """\
# test budget
BUDGET_GENERATOR_TO_AGENT_S=1
BUDGET_AGENT_TO_GATEWAY_S=1
BUDGET_GATEWAY_TO_DECIDED_S=31
BUDGET_DECIDED_TO_ENQUEUE_S=0.1
BUDGET_ENQUEUE_TO_CLICKHOUSE_S=1
BUDGET_ASSEMBLER_S=6
BUDGET_DETECTORS_ATTRIBUTOR_S=19.9
SLO_P95_S=60
DISTORTION_P50_S=0.5
DISTORTION_P95_S=1
DISTORTION_P95_REL=0.05
"""


@pytest.fixture
def budget(tmp_path: Path) -> sr.Budget:
    p = tmp_path / "budget.env"
    p.write_text(BUDGET_ENV, encoding="utf-8")
    return sr.read_budget(p)


def root(
    trace: str,
    end: int,
    agent: float,
    gateway: float,
    decided: float,
    export: float,
    ingested: float,
) -> sr.RootStamps:
    """Stamps as offsets in seconds after the root end (end in ns)."""
    return sr.RootStamps(
        trace,
        end,
        end + int(agent * S),
        end + int(gateway * S),
        end + int(decided * S),
        end + int(export * S),
        (end + int(ingested * S)) // 1_000_000,
    )


def test_the_budget_file_sums_to_the_slo_and_names_every_stage(budget: sr.Budget) -> None:
    assert budget.stages == {
        "generator_to_agent": 1.0,
        "agent_to_gateway": 1.0,
        "gateway_to_decided": 31.0,
        "decided_to_enqueue": 0.1,
        "enqueue_to_clickhouse": 1.0,
    }
    assert budget.total == pytest.approx(60.0)
    # Measured end to end is root end -> trace.completed: the SLO minus what is not measured.
    assert budget.measured_e2e == pytest.approx(40.1)


def test_a_budget_over_the_slo_is_refused(tmp_path: Path) -> None:
    p = tmp_path / "b.env"
    p.write_text(BUDGET_ENV.replace("BUDGET_ASSEMBLER_S=6", "BUDGET_ASSEMBLER_S=7"), "utf-8")
    with pytest.raises(ValueError, match="exceeds"):
        sr.read_budget(p)


def test_a_budget_without_a_stage_is_refused(tmp_path: Path) -> None:
    p = tmp_path / "b.env"
    p.write_text(BUDGET_ENV.replace("BUDGET_AGENT_TO_GATEWAY_S=1\n", ""), "utf-8")
    with pytest.raises(KeyError):
        sr.read_budget(p)


def test_stages_are_the_deltas_of_the_root_span_and_add_up_to_the_total() -> None:
    data = sr.stage_data([root("a", 10 * S, 0.5, 1.0, 30.0, 30.0, 32.0)], split=set())
    assert data.stages["generator_to_agent"] == [0.5]
    assert data.stages["agent_to_gateway"] == [0.5]
    assert data.stages["gateway_to_decided"] == [29.0]
    assert data.stages["decided_to_enqueue"] == [0.0]
    assert data.stages["enqueue_to_clickhouse"] == pytest.approx([2.0])
    assert data.total == pytest.approx([32.0])
    assert sum(v[0] for v in data.stages.values()) == pytest.approx(data.total[0])


def test_split_traces_and_missing_stamps_are_excluded_and_counted() -> None:
    full = root("ok", 10 * S, 0.1, 0.2, 30.0, 30.0, 30.5)
    split = root("split", 10 * S, 0.1, 0.2, 30.0, 30.0, 30.5)
    missing = sr.RootStamps("nostamp", 10 * S, None, 10 * S, 10 * S, 10 * S, 11_000)
    unarrived = sr.RootStamps("noarrival", 10 * S, 10 * S, 10 * S, 10 * S, 10 * S, None)
    data = sr.stage_data([full, split, missing, unarrived], split={"split"})
    assert data.total == pytest.approx([30.5])
    assert data.excluded == {"split": 1, "missing_stamp": 1, "no_arrival": 1}


def test_the_share_is_the_stage_mean_over_the_total_mean() -> None:
    data = sr.stage_data(
        [
            root("a", 10 * S, 1, 2, 32, 32, 33),
            root("b", 11 * S, 1, 2, 30, 30, 31),
        ],
        split=set(),
    )
    table = sr.stage_table(data)
    shares = {r.stage: r.share for r in table}
    assert shares["gateway_to_decided"] == pytest.approx(29 / 32)
    assert sum(shares.values()) == pytest.approx(1.0)
    assert {r.stage: r.p95 for r in table}["gateway_to_decided"] == pytest.approx(30.0)


def test_stage_verdicts_compare_each_p95_with_its_budget(budget: sr.Budget) -> None:
    data = sr.stage_data([root("a", 10 * S, 0.1, 0.2, 40.0, 40.0, 140.0)], split=set())
    verdicts = {v.stage: v.status for v in sr.stage_verdicts(sr.stage_table(data), budget, False)}
    assert verdicts == {
        "generator_to_agent": "ok",
        "agent_to_gateway": "ok",
        "gateway_to_decided": "over",
        "decided_to_enqueue": "ok",
        "enqueue_to_clickhouse": "over",
    }


def test_a_saturated_generator_replaces_the_stage_one_verdict(budget: sr.Budget) -> None:
    data = sr.stage_data([root("a", 10 * S, 5.0, 5.1, 30.0, 30.0, 30.5)], split=set())
    verdicts = {v.stage: v.status for v in sr.stage_verdicts(sr.stage_table(data), budget, True)}
    assert verdicts["generator_to_agent"] == "saturated"


def test_generator_backlog_over_the_stage_one_budget_is_saturation(budget: sr.Budget) -> None:
    ok = [{"pod": 0, "backlog_ms": {"p50": 5, "p95": 40, "max": 80}}]
    bad = [*ok, {"pod": 1, "backlog_ms": {"p50": 300, "p95": 1500, "max": 9000}}]
    assert sr.generator_backlog(ok) == pytest.approx(0.04)
    assert not sr.generator_saturated(ok, budget)
    assert sr.generator_saturated(bad, budget)
    # A summary from a loadgen without the field cannot prove the generator kept up.
    assert math.isnan(sr.generator_backlog([{"pod": 0}]))
    assert sr.generator_saturated([{"pod": 0}], budget)


def test_end_to_end_is_checked_separately_against_the_measured_budget(budget: sr.Budget) -> None:
    assert sr.e2e_verdict(39.0, budget) == "ok"
    assert sr.e2e_verdict(41.0, budget) == "over"
    assert sr.e2e_verdict(math.nan, budget) == "n/a"


def test_buckets_group_the_stages_by_root_end_time() -> None:
    t0 = 1000 * S
    data = sr.stage_data(
        [
            root("a", t0 + 5 * S, 0.1, 0.2, 30, 30, 31),
            root("b", t0 + 25 * S, 0.1, 0.2, 30, 30, 33),
            root("c", t0 + 35 * S, 0.1, 0.2, 30, 30, 60),
        ],
        split=set(),
    )
    buckets = sr.buckets(data, t0, width_s=30)
    assert [(b.start_s, b.n) for b in buckets] == [(0, 2), (30, 1)]
    assert buckets[0].total_p95 == pytest.approx(33.0)
    assert buckets[1].stages_p95["enqueue_to_clickhouse"] == pytest.approx(30.0)


METRICS_GW = """\
# HELP otelcol_exporter_queue_size Current size of the retry queue
# TYPE otelcol_exporter_queue_size gauge
otelcol_exporter_queue_size{data_type="traces",exporter="clickhouse"} 120
otelcol_processor_tail_sampling_sampling_traces_on_memory{otel_scope_name="x"} 6000
otelcol_receiver_refused_spans_total{receiver="otlp",transport="grpc"} 3
otelcol_processor_tail_sampling_sampling_decision_timer_latency_sum{a="b"} 900
otelcol_processor_tail_sampling_sampling_decision_timer_latency_count{a="b"} 3
"""
METRICS_AGENT = """\
otelcol_exporter_queue_size{data_type="traces",exporter="loadbalancing"} 2048
otelcol_receiver_accepted_spans_total{receiver="otlp",transport="grpc"} 100
"""


def test_the_prometheus_text_parser_keeps_names_labels_and_values() -> None:
    samples = sr.parse_metrics(METRICS_GW)
    assert (
        sr.Sample(
            "otelcol_exporter_queue_size", {"data_type": "traces", "exporter": "clickhouse"}, 120.0
        )
        in samples
    )
    assert all(not s.name.startswith("#") for s in samples)


def test_a_counter_matches_with_or_without_the_total_suffix() -> None:
    samples = sr.parse_metrics(METRICS_GW)
    assert sr.metric_value(samples, "otelcol_receiver_refused_spans") == 3
    assert sr.metric_value(samples, "otelcol_receiver_refused_spans_total") == 3
    assert sr.metric_value(samples, "otelcol_exporter_queue_size", exporter="clickhouse") == 120
    assert sr.metric_value(samples, "otelcol_exporter_queue_size", exporter="other") is None


def scrapes_file(tmp_path: Path) -> Path:
    p = tmp_path / "metrics.txt"
    blocks: list[str] = []
    for tick, ts in ((0, 1000 * S), (1, 1005 * S), (2, 1040 * S)):
        gw = METRICS_GW.replace(" 120", f" {120 * (tick + 1)}")
        gw = gw.replace('latency_sum{a="b"} 900', f'latency_sum{{a="b"}} {900 * (tick + 1)}')
        gw = gw.replace('latency_count{a="b"} 3', f'latency_count{{a="b"}} {3 * (tick + 1)}')
        blocks.append(f"# SCRAPE {tick} {ts} gateway otel-gateway-0\n{gw}")
        blocks.append(f"# SCRAPE {tick} {ts} agent otel-agent-0\n{METRICS_AGENT}")
    p.write_text("".join(blocks), encoding="utf-8")
    return p


def test_scrapes_become_queue_trends_per_bucket(tmp_path: Path) -> None:
    scrapes = sr.read_scrapes(scrapes_file(tmp_path))
    assert len(scrapes) == 6
    trend = sr.queue_trend(scrapes, [], t0_ns=1000 * S, width_s=30)
    assert [r.start_s for r in trend] == [0, 30]
    first, second = trend
    assert first.values["gateway exporter queue (requests)"] == 240
    assert second.values["gateway exporter queue (requests)"] == 360
    assert first.values["agent loadbalancing queue (bytes)"] == 2048
    assert first.values["gateway traces in tail_sampling"] == 6000
    # Mean decision-timer run from the deltas of sum and count: 900 ms / 3 runs.
    assert first.values["gateway decision timer run (ms, mean)"] == pytest.approx(300)


def test_clickhouse_pending_async_inserts_join_the_trend(tmp_path: Path) -> None:
    scrapes = sr.read_scrapes(scrapes_file(tmp_path))
    ch = [sr.ClickHouseTick(1_000_500, 4, 10, 0, 10), sr.ClickHouseTick(1_031_000, 9, 20, 1, 20)]
    trend = sr.queue_trend(scrapes, ch, t0_ns=1000 * S, width_s=30)
    assert trend[0].values["ClickHouse pending async inserts"] == 4
    assert trend[1].values["ClickHouse pending async inserts"] == 9
    assert trend[1].values["ClickHouse failed async inserts"] == 1


def test_counter_increase_survives_pods_that_come_and_go(tmp_path: Path) -> None:
    scrapes = sr.read_scrapes(scrapes_file(tmp_path))
    inc = sr.counter_increase(scrapes, "gateway", "otelcol_receiver_refused_spans")
    assert inc == 0  # 3 at every scrape


def test_idle_check_needs_no_growth_of_accepted_spans() -> None:
    before = sr.parse_metrics(METRICS_AGENT)
    after = sr.parse_metrics(METRICS_AGENT.replace(" 100\n", " 140\n"))
    assert sr.accepted_spans(before) == 100
    assert sr.accepted_spans(after) - sr.accepted_spans(before) == 40


def test_distortion_compares_the_two_low_runs(budget: sr.Budget) -> None:
    same = sr.distortion(sr.Pct(30.0, 34.0), sr.Pct(30.2, 34.5), budget)
    assert same.status == "ok"
    far = sr.distortion(sr.Pct(30.0, 34.0), sr.Pct(31.0, 34.5), budget)
    assert far.status == "distorting"
    assert far.d_p50 == pytest.approx(1.0)
    # 5 % of a 100 s p95 is above the 1 s floor.
    rel = sr.distortion(sr.Pct(90.0, 100.0), sr.Pct(90.0, 104.0), budget)
    assert rel.status == "ok"


def test_render_run_lists_stages_verdicts_exclusions_and_trends(budget: sr.Budget) -> None:
    t0 = 1000 * S
    data = sr.stage_data(
        [
            root("a", t0 + 5 * S, 0.1, 0.2, 30, 30, 31),
            root("b", t0 + 6 * S, 0.1, 0.2, 30, 30, 140),
        ],
        split={"x"},
    )
    md = sr.render_run(
        sr.RunReport(
            name="low",
            run_id="abcd1234",
            data=data,
            budget=budget,
            generator=[{"pod": 0, "backlog_ms": {"p50": 1, "p95": 2, "max": 3}}],
            e2e=sr.Pct(32.0, 141.0),
            to_clickhouse=sr.Pct(31.0, 140.0),
            trend=[sr.TrendRow(0, {"gateway exporter queue (requests)": 5.0})],
            counters={"agent refused spans": 0.0},
            t0_ns=t0,
            events=[{"name": "scale 2->4", "command_ns": t0 + 120 * S}],
        )
    )
    assert "| enqueue_to_clickhouse |" in md
    assert "over" in md
    assert "do not add up" in md
    assert "scale 2->4 at T+120 s" in md
    assert "1 ms" in md  # the resolution floor of ingested_at
    assert "end to end (root end to trace.completed)" in md


def test_render_run_marks_a_saturated_generator(budget: sr.Budget) -> None:
    data = sr.stage_data([root("a", 10 * S, 4, 4.1, 30, 30, 31)], split=set())
    md = sr.render_run(
        sr.RunReport(
            name="stress",
            run_id="r",
            data=data,
            budget=budget,
            generator=[{"pod": 0, "backlog_ms": {"p50": 900, "p95": 4000, "max": 9000}}],
            e2e=sr.Pct(40.0, 50.0),
            to_clickhouse=sr.Pct(31.0, 31.0),
            trend=[],
            counters={},
            t0_ns=10 * S,
            events=[],
        )
    )
    assert "load generator saturated — not a pipeline measurement" in md


def test_run_report_round_trips_through_json(budget: sr.Budget, tmp_path: Path) -> None:
    data = sr.stage_data([root("a", 10 * S, 0.1, 0.2, 30, 30, 31)], split=set())
    rep = sr.RunReport("low", "r", data, budget, [], sr.Pct(32, 33), sr.Pct(31, 31), [], {}, 0, [])
    out = tmp_path / "r.json"
    sr.write_json(rep, out)
    loaded = sr.load_run_json(out)
    assert loaded["name"] == "low"
    assert loaded["stages"]["gateway_to_decided"]["p95"] == pytest.approx(29.8)
    assert loaded["e2e_s"]["p95"] == 33
    assert loaded["generator_saturated"] is True  # no backlog recorded


def test_summary_names_over_budget_stages_per_profile_and_the_sustained_load(
    budget: sr.Budget, tmp_path: Path
) -> None:
    def run(name: str, profile: str, stamps: bool, rate: int, ch_p95: float, e2e95: float) -> Path:
        data = sr.stage_data([root("a", 10 * S, 0.1, 0.2, 30, 30, 30 + ch_p95)], split=set())
        rep = sr.RunReport(
            name,
            "r",
            data,
            budget,
            [{"pod": 0, "backlog_ms": {"p50": 1, "p95": 2, "max": 3}}],
            sr.Pct(e2e95 - 1, e2e95),
            sr.Pct(30 + ch_p95, 30 + ch_p95),
            [],
            {},
            0,
            [],
            profile=profile,
            stamps=stamps,
            total_rate=rate,
        )
        p = tmp_path / f"{name}.json"
        sr.write_json(rep, p)
        return p

    runs = [
        run("low-nostamps", "low", False, 20, 0.5, 37.0),
        run("low", "low", True, 20, 0.6, 37.2),
        run("stress", "stress", True, 200, 150.0, 190.0),
        run("mid", "steady", True, 100, 0.9, 38.0),
    ]
    md = sr.render_summary([sr.load_run_json(p) for p in runs], budget, {"commit": "abc"})
    assert "Over budget at low steady load: none" in md
    assert "Over budget only under the stress profile: enqueue_to_clickhouse" in md
    assert "highest tested load with end-to-end p95 within budget: 100 traces/s" in md
    assert "Distortion (low, with vs without stamps)" in md
    assert "| commit | abc |" in md


def test_the_committed_budget_sums_to_the_slo() -> None:
    path = Path(__file__).resolve().parents[2] / "bench" / "gateway-scaling" / "stage-budget.env"
    b = sr.read_budget(path)
    assert b.total == pytest.approx(60.0)
    assert b.slo == 60


def test_the_cli_reads_what_run_sh_dumps_and_writes_both_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    import sys

    budget = tmp_path / "budget.env"
    budget.write_text(BUDGET_ENV, encoding="utf-8")
    t0 = 1000 * S
    end = t0 + 5 * S
    ns = [end + int(x * S) for x in (0.1, 0.2, 30.0, 30.0)]
    (tmp_path / "stamps.tsv").write_text(
        f"aa\t{end}\t" + "\t".join(map(str, ns)) + f"\t{(end + 31 * S) // 1_000_000}\n"
        f"bb\t{end}\t\t\t\t\t\\N\n",  # no overlay, no root arrival
        encoding="utf-8",
    )
    (tmp_path / "stored.tsv").write_text("aa\ts1\tgw-0\ttenant-a\t1\n", encoding="utf-8")
    summary = {"pod": 0, "t0_ns": t0, "backlog_ms": {"p50": 1, "p95": 2, "max": 3}}
    (tmp_path / "summaries.jsonl").write_text(json.dumps(summary) + "\n", encoding="utf-8")
    (tmp_path / "assembler.json").write_text(
        json.dumps(
            {
                "first_assembly_s": {"p50": 37.0, "p95": 38.0},
                "root_end_to_first_arrival_s": {"p50": 31.0, "p95": 31.0},
            }
        ),
        encoding="utf-8",
    )
    scrapes_file(tmp_path)
    (tmp_path / "clickhouse.tsv").write_text("1000500\t4\t10\t0\t10\n", encoding="utf-8")
    (tmp_path / "events.jsonl").write_text("", encoding="utf-8")
    args = ["run", "--name", "low", "--run-id", "r1", "--profile", "low", "--stamps", "1"]
    args += ["--total-rate", "20", "--budget", str(budget)]
    for flag, name in (
        ("--roots", "stamps.tsv"),
        ("--stored", "stored.tsv"),
        ("--summaries", "summaries.jsonl"),
        ("--assembler-json", "assembler.json"),
        ("--metrics", "metrics.txt"),
        ("--clickhouse", "clickhouse.tsv"),
        ("--events", "events.jsonl"),
        ("--markdown", "stages.md"),
        ("--json", "stages.json"),
    ):
        args += [flag, str(tmp_path / name)]
    monkeypatch.setattr(sys, "argv", ["stage_report.py", *args])
    assert sr.main() == 0
    run = sr.load_run_json(tmp_path / "stages.json")
    assert run["excluded"] == {"split": 0, "missing_stamp": 1, "no_arrival": 0}
    assert run["stages"]["gateway_to_decided"]["p95"] == pytest.approx(29.8)
    assert run["e2e_status"] == "ok"
    assert run["trend"][0]["values"]["ClickHouse pending async inserts"] == 4
    assert "| gateway_to_decided |" in (tmp_path / "stages.md").read_text(encoding="utf-8")

    (tmp_path / "env.json").write_text(json.dumps({"commit": "abc"}), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["stage_report.py", "summary", "--budget", str(budget), "--env", str(tmp_path / "env.json"),
         "--markdown", str(tmp_path / "summary.md"), str(tmp_path / "stages.json")],
    )  # fmt: skip
    assert sr.main() == 0
    md = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "Over budget at low steady load: none" in md
    assert "20 traces/s" in md


def test_split_traces_are_read_from_the_stored_spans(tmp_path: Path) -> None:
    p = tmp_path / "stored.tsv"
    p.write_text("a\t1\tgw-0\tt\t1\na\t2\tgw-1\tt\t1\nb\t1\tgw-0\tt\t1\n", encoding="utf-8")
    assert sr.split_traces(p) == {"a"}
