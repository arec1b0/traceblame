"""The injection guard: faults fire only when switched on explicitly AND the process runs as an
eval tenant (ADR-0005: only eval tenants may mark traces; anything else would pollute D1).

The TypeScript preload (packages/faults/preload/faults-preload.mjs) applies the same rule.
"""

from collections.abc import Mapping

FAULTS = "TRACEBLAME_FAULTS"
TENANT = "TRACEBLAME_TENANT_ID"
EVAL_TENANTS = "TRACEBLAME_EVAL_TENANTS"


class GuardError(RuntimeError):
    pass


def eval_tenants(value: str) -> list[str]:
    """The eval-tenants ConfigMap value, `[a, b]` (YAML flow list), or `a,b`."""
    inner = value.strip().removeprefix("[").removesuffix("]")
    return [t.strip() for t in inner.split(",") if t.strip()]


def require(env: Mapping[str, str]) -> str:
    """The eval tenant, or GuardError naming what is missing (never a tenant list)."""
    if env.get(FAULTS) != "1":
        raise GuardError(f"faults are off: {FAULTS} is not 1")
    tenant = env.get(TENANT, "")
    if not tenant or tenant not in eval_tenants(env.get(EVAL_TENANTS, "")):
        raise GuardError(f"faults are off: {TENANT} is not an eval tenant ({EVAL_TENANTS})")
    return tenant


def enabled(env: Mapping[str, str]) -> bool:
    try:
        require(env)
    except GuardError:
        return False
    return True
