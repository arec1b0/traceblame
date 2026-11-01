"""Shim configuration, from arguments or the environment."""

import os
from collections.abc import Mapping
from dataclasses import dataclass

from traceblame_py._attributes import BUNDLE_DIGEST, EVAL_RUN_ID, TENANT_ID, checked

DEFAULT_MAX_TRACES = 1024


@dataclass(frozen=True)
class ShimConfig:
    """What the shim stamps on telemetry. Every value is checked against the profile.

    - tenant_id: resource aegis.tenant.id. Behind an authenticating agent the agent replaces
      it with the authenticated tenant (ADR-0005).
    - eval_run_id: resource aegis.eval.run.id, for every trace of an eval run.
    - bundle_digest: span aegis.bundle.digest on every span, when a promptops bundle is in use.
    - max_traces: traces whose retrieved ids are held for aegis.output.cited_doc_ids.
    """

    tenant_id: str | None = None
    eval_run_id: str | None = None
    bundle_digest: str | None = None
    max_traces: int = DEFAULT_MAX_TRACES

    def __post_init__(self) -> None:
        for name, value in (
            (TENANT_ID, self.tenant_id),
            (EVAL_RUN_ID, self.eval_run_id),
            (BUNDLE_DIGEST, self.bundle_digest),
        ):
            if value is not None:
                checked(name, value)
        if self.max_traces < 1:
            raise ValueError("max_traces must be at least 1")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ShimConfig":
        """TRACEBLAME_TENANT_ID, TRACEBLAME_EVAL_RUN_ID, TRACEBLAME_BUNDLE_DIGEST,
        TRACEBLAME_MAX_TRACES. The exporter reads the standard OTEL_EXPORTER_OTLP_* variables."""
        env = os.environ if env is None else env
        max_traces = env.get("TRACEBLAME_MAX_TRACES")
        return cls(
            tenant_id=env.get("TRACEBLAME_TENANT_ID"),
            eval_run_id=env.get("TRACEBLAME_EVAL_RUN_ID"),
            bundle_digest=env.get("TRACEBLAME_BUNDLE_DIGEST"),
            max_traces=int(max_traces) if max_traces else DEFAULT_MAX_TRACES,
        )
