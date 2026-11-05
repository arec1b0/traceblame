"""Faults run only when switched on explicitly and only for an eval tenant."""

import pytest

from traceblame_faults import guard

ON = {
    "TRACEBLAME_FAULTS": "1",
    "TRACEBLAME_TENANT_ID": "tenant-eval",
    "TRACEBLAME_EVAL_TENANTS": "[tenant-eval]",
}


@pytest.mark.parametrize(
    ("value", "tenants"),
    [
        ("[tenant-eval]", ["tenant-eval"]),
        ("[tenant-eval, tenant-x]", ["tenant-eval", "tenant-x"]),
        ("tenant-eval,tenant-x", ["tenant-eval", "tenant-x"]),
        ("[]", []),
        ("", []),
    ],
)
def test_eval_tenants_reads_the_configmap_format(value: str, tenants: list[str]) -> None:
    assert guard.eval_tenants(value) == tenants


def test_enabled_only_with_the_switch_and_an_eval_tenant() -> None:
    assert guard.enabled(ON)
    assert guard.require(ON) == "tenant-eval"


@pytest.mark.parametrize(
    "env",
    [
        {},
        ON | {"TRACEBLAME_FAULTS": "0"},
        ON | {"TRACEBLAME_FAULTS": "true"},
        ON | {"TRACEBLAME_TENANT_ID": "tenant-a"},
        ON | {"TRACEBLAME_EVAL_TENANTS": "[]"},
        {k: v for k, v in ON.items() if k != "TRACEBLAME_EVAL_TENANTS"},
        {k: v for k, v in ON.items() if k != "TRACEBLAME_TENANT_ID"},
    ],
)
def test_anything_else_is_off(env: dict[str, str]) -> None:
    assert not guard.enabled(env)
    with pytest.raises(guard.GuardError):
        guard.require(env)
