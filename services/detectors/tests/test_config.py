"""Config, specs and versions: params-derived versions, production refuses eval_only."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from traceblame_detectors.api import Detector, DetectorSpec
from traceblame_detectors.config import (
    ConfigError,
    DetectorConfig,
    Mode,
    detector_version,
    load_config,
    load_config_file,
)
from traceblame_detectors.model import ContextNeed, Reads, SpanKind

Make = Callable[..., DetectorConfig]


def _version(config: DetectorConfig, name: str) -> str:
    return next(d.detector_version for d in config.detectors if d.spec.name == name)


def test_the_version_is_code_semver_plus_a_params_digest(make_config: Make) -> None:
    config = make_config(reference_root={})
    d = config.detectors[0]
    assert d.params_json == '{"error_score":0.5,"min_spans":1}'
    assert d.detector_version == detector_version("0.1.0", d.params_json)
    assert d.detector_version.startswith("0.1.0+p")
    assert len(d.detector_version) == 15


def test_defaults_written_out_give_the_same_version(make_config: Make) -> None:
    implicit = make_config(reference_root={})
    explicit = make_config(reference_root={"params": {"min_spans": 1, "error_score": 0.5}})
    as_int = make_config(reference_root={"params": {"error_score": 0.5, "min_spans": 1}})
    assert _version(implicit, "reference_root") == _version(explicit, "reference_root")
    assert _version(implicit, "reference_root") == _version(as_int, "reference_root")


def test_an_int_for_a_float_param_is_the_same_value(make_config: Make) -> None:
    a = make_config(reference_root={"params": {"error_score": 1}})
    b = make_config(reference_root={"params": {"error_score": 1.0}})
    assert _version(a, "reference_root") == _version(b, "reference_root")


def test_a_threshold_change_changes_the_version(make_config: Make) -> None:
    a = make_config(reference_root={"params": {"error_score": 0.5}})
    b = make_config(reference_root={"params": {"error_score": 0.6}})
    assert _version(a, "reference_root") != _version(b, "reference_root")


def test_operational_settings_keep_the_version(registry: dict[str, type[Detector]]) -> None:
    base = {"mode": "production", "detectors": {"reference_root": {"params": {"min_spans": 2}}}}
    tuned = {
        **base,
        "worker": {"batch_size": 64, "log_level": "debug", "ack_wait": "60s", "max_deliver": 9},
        "offline": {"log_level": "warning"},
    }
    a, b = load_config(base, registry), load_config(tuned, registry)
    assert _version(a, "reference_root") == _version(b, "reference_root")
    assert b.worker["batch_size"] == 64


def test_production_refuses_an_eval_only_detector(make_config: Make) -> None:
    with pytest.raises(ConfigError, match="eval_only"):
        make_config(reference_golden={})
    config = make_config("eval", reference_golden={})
    assert config.mode is Mode.EVAL
    assert config.detectors[0].spec.eval_only


def test_mode_defaults_to_production(registry: dict[str, type[Detector]]) -> None:
    assert load_config({}, registry).mode is Mode.PRODUCTION
    with pytest.raises(ConfigError, match="mode"):
        load_config({"mode": "shadow"}, registry)


@pytest.mark.parametrize(
    ("data", "match"),
    [
        ({"detectors": {"nope": {}}}, "not in the registry"),
        ({"detectors": {"reference_root": {"params": {"min_spans": "2"}}}}, "must be int"),
        ({"detectors": {"reference_root": {"params": {"min_spans": True}}}}, "must be int"),
        ({"detectors": {"reference_root": {"params": {"x": 1}}}}, "unknown params"),
        ({"detectors": {"reference_root": {"timeout": 1}}}, "unknown keys"),
        ({"thresholds": {}}, "unknown top-level"),
    ],
)
def test_bad_configs_are_refused(
    registry: dict[str, type[Detector]], data: dict[str, Any], match: str
) -> None:
    with pytest.raises(ConfigError, match=match):
        load_config(data, registry)


def _with_spec(base: type[Detector], **changes: Any) -> type[Detector]:
    spec = replace(base.spec, **changes)
    return type("Patched", (base,), {"spec": spec})


@pytest.mark.parametrize(
    "key",
    [
        "aegis.eval.run.id",
        "aegis.fault.injection.id",
        "aegis.content.ref",
        "exception.message",
        "exception.stacktrace",
        "exception.escaped",
        "aegis.session.id",
        "gen_ai.input.messages",
        "llm.model_name",
        "gen_ai.request.model",
    ],
)
def test_a_detector_may_not_declare_a_denied_or_unknown_attribute(
    registry: dict[str, type[Detector]], key: str
) -> None:
    root = registry["reference_root"]
    bad = _with_spec(root, reads=Reads(frozenset({SpanKind.LLM}), frozenset({key})))
    with pytest.raises(ConfigError, match="may not be declared"):
        load_config({"detectors": {"reference_root": {}}}, {"reference_root": bad})


@dataclass
class _Mutable:
    x: int = 1


@dataclass(frozen=True)
class _ListParam:
    xs: list[int] | None = None


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"needs": frozenset({ContextNeed.EVAL_GOLDEN})}, "only an eval_only"),
        ({"params_type": _Mutable}, "frozen dataclass"),
        ({"params_type": _ListParam}, "not one of"),
        ({"code_version": "1.0"}, "not semver"),
        ({"reads": Reads(frozenset())}, "span_kinds is empty"),
        ({"name": "Bad-Name"}, "does not match"),
    ],
)
def test_bad_specs_are_refused(
    registry: dict[str, type[Detector]], changes: dict[str, Any], match: str
) -> None:
    bad = _with_spec(registry["reference_root"], **changes)
    name = changes.get("name", "reference_root")
    with pytest.raises(ConfigError, match=match):
        load_config({"detectors": {name: {}}}, {name: bad})


def test_a_detector_registered_under_another_name_is_refused(
    registry: dict[str, type[Detector]],
) -> None:
    with pytest.raises(ConfigError, match="another name"):
        load_config({"detectors": {"alias": {}}}, {"alias": registry["reference_root"]})


def test_a_required_param_without_a_value_is_refused(registry: dict[str, type[Detector]]) -> None:
    @dataclass(frozen=True)
    class Required:
        x: int

    bad = _with_spec(registry["reference_root"], params_type=Required)
    with pytest.raises(ConfigError, match="missing"):
        load_config({"detectors": {"reference_root": {}}}, {"reference_root": bad})


def test_the_config_file_is_yaml(tmp_path: Path, registry: dict[str, type[Detector]]) -> None:
    path = tmp_path / "detectors.yaml"
    path.write_text("mode: production\ndetectors:\n  reference_root: {params: {min_spans: 3}}\n")
    config = load_config_file(path, registry)
    assert config.detectors[0].params.min_spans == 3
    assert config.attr_keys == registry["reference_root"].spec.reads.attrs
    path.write_text("- not a mapping\n")
    with pytest.raises(ConfigError, match="mapping"):
        load_config_file(path, registry)


def test_specs_are_frozen(registry: dict[str, type[Detector]]) -> None:
    assert isinstance(registry["reference_root"].spec, DetectorSpec)
