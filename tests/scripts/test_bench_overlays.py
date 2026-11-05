"""The bench stamps (#57) never leave the bench.

The stamp attributes (`traceblame.bench.t_*`) and the transform processor that writes them
live only in the `*-bench*.yaml` overlays of deploy/kind/collector. No other collector
config, manifest, smoke config, profile, schema or SDK allowlist may name them, and each
overlay's pipeline is its base pipeline with only the bench processors added.
"""

import re
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COLLECTOR = ROOT / "deploy" / "kind" / "collector"
STAMP = "traceblame.bench.t_"
TRANSFORM = re.compile(r"\btransform(/[\w.-]+)?\s*:|\btransform(/[\w.-]+)?\b\s*[,\]]")


def is_bench_overlay(path: Path) -> bool:
    return (
        path.parent == COLLECTOR
        and re.fullmatch(r".+-bench(-[\w-]+)?\.yaml", path.name) is not None
    )


def guarded_files() -> list[Path]:
    """Everything that configures a collector or names attributes, outside the overlays."""
    globs = [
        "deploy/**/*.yaml",
        "deploy/**/*.yml",
        "collector/config/*.yaml",
        "collector/test/smoke/testdata/*.yaml",
        "collector/processor/normalizer/profiles/*",
        "collector/processor/normalizer/mappings/**/*",
        "schemas/**/*",
        "sdk/python/src/traceblame_py/_allowlist.py",
        "sdk/ts/src/**/*.ts",
        "scripts/*.sh",
    ]
    files = {p for g in globs for p in ROOT.glob(g) if p.is_file()}
    return sorted(p for p in files if not is_bench_overlay(p))


def test_the_guard_covers_the_configs_it_names() -> None:
    names = {p.relative_to(ROOT).as_posix() for p in guarded_files()}
    for must in (
        "deploy/kind/collector/agent.yaml",
        "deploy/kind/collector/gateway.yaml",
        "deploy/kind/manifests/20-agent.yaml",
        "deploy/kind/manifests/30-gateway.yaml",
        "deploy/kind/runbook/sandbox/tee.yaml",
        "collector/config/otelcol.yaml",
        "collector/test/smoke/testdata/memory-limiter-mib.yaml",
        "collector/processor/normalizer/profiles/aegis-telemetry.v1.yaml",
        "schemas/profile/aegis-telemetry.v1.schema.json",
        "sdk/python/src/traceblame_py/_allowlist.py",
    ):
        assert must in names
    assert "deploy/kind/collector/gateway-bench.yaml" not in names
    assert "deploy/kind/collector/agent-bench-stamps.yaml" not in names


@pytest.mark.parametrize("path", guarded_files(), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_no_stamp_or_transform_outside_the_bench_overlays(path: Path) -> None:
    text = path.read_text(encoding="utf-8", errors="replace")
    assert STAMP not in text
    if path.suffix in {".yaml", ".yml"}:
        assert TRANSFORM.search(text) is None, "transform processor outside a bench overlay"


def load(name: str) -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load((COLLECTOR / name).read_text(encoding="utf-8")))


def processors(cfg: dict[str, Any]) -> list[str]:
    return cast("list[str]", cfg["service"]["pipelines"]["traces"]["processors"])


def test_the_kind_deploy_ships_the_overlays_but_starts_without_them() -> None:
    deploy = (ROOT / "scripts" / "kind-deploy.sh").read_text(encoding="utf-8")
    assert "agent-bench-stamps.yaml" in deploy
    assert "gateway-bench-stamps.yaml" in deploy
    for manifest in ("20-agent.yaml", "30-gateway.yaml"):
        text = (ROOT / "deploy" / "kind" / "manifests" / manifest).read_text(encoding="utf-8")
        assert "bench" not in text


@pytest.mark.parametrize(
    ("base", "overlays", "added"),
    [
        (["agent.yaml"], ["agent-bench-stamps.yaml"], ["transform/bench_agent"]),
        (["gateway.yaml"], ["gateway-bench.yaml"], ["resource/bench"]),
        (
            ["gateway.yaml"],
            ["gateway-bench.yaml", "gateway-bench-stamps.yaml"],
            [
                "transform/bench_gateway",
                "transform/bench_decided",
                "resource/bench",
                "transform/bench_export",
            ],
        ),
    ],
)
def test_an_overlay_only_adds_bench_processors_to_its_base(
    base: list[str], overlays: list[str], added: list[str]
) -> None:
    want = processors(load(base[0]))
    got = processors(load(overlays[-1]))  # a list in an overlay replaces the base's list
    assert [p for p in got if p not in added] == want
    assert [p for p in got if p in added] == added


def test_each_stamp_sits_where_the_report_expects_it() -> None:
    agent = processors(load("agent-bench-stamps.yaml"))
    gateway = processors(load("gateway-bench-stamps.yaml"))
    assert agent[0] == "transform/bench_agent"
    assert gateway[0] == "transform/bench_gateway"
    assert gateway[gateway.index("tail_sampling") + 1] == "transform/bench_decided"
    assert gateway[-1] == "transform/bench_export"


@pytest.mark.parametrize(
    ("overlay", "name", "attr"),
    [
        ("agent-bench-stamps.yaml", "transform/bench_agent", "t_agent_ns"),
        ("gateway-bench-stamps.yaml", "transform/bench_gateway", "t_gateway_ns"),
        ("gateway-bench-stamps.yaml", "transform/bench_decided", "t_decided_ns"),
        ("gateway-bench-stamps.yaml", "transform/bench_export", "t_export_ns"),
    ],
)
def test_each_stamp_is_one_wall_clock_statement_in_the_bench_namespace(
    overlay: str, name: str, attr: str
) -> None:
    proc = load(overlay)["processors"][name]
    assert proc["error_mode"] == "ignore"
    assert proc["trace_statements"] == [
        f'set(span.attributes["traceblame.bench.{attr}"], UnixNano(Now()))'
    ]


def test_the_guard_would_catch_a_transform_or_a_stamp() -> None:
    for overlay in ("agent-bench-stamps.yaml", "gateway-bench-stamps.yaml"):
        text = (COLLECTOR / overlay).read_text(encoding="utf-8")
        assert TRANSFORM.search(text) is not None
        assert STAMP in text
    assert TRANSFORM.search("processors: [memory_limiter, transform]") is not None
    assert TRANSFORM.search("processors:\n  transform/x:\n    error_mode: ignore") is not None
    assert TRANSFORM.search("# transformation of the profile happens elsewhere") is None
