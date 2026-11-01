"""The aegis.* attributes the shim sets, with the scope and pattern the profile gives them.

tests/contract/test_shim_contract.py checks these against schemas/profile/aegis-telemetry.v1.yaml.
"""

import re

TENANT_ID = "aegis.tenant.id"
EVAL_RUN_ID = "aegis.eval.run.id"
SESSION_ID = "aegis.session.id"
BUNDLE_DIGEST = "aegis.bundle.digest"
DECISION_ID = "aegis.decision.id"
FAULT_INJECTION_ID = "aegis.fault.injection.id"
ARGS_HASH = "aegis.tool.args_hash"
DOC_IDS = "aegis.retrieval.doc_ids"
SCORES = "aegis.retrieval.scores"
PARSE_OK = "aegis.output.parse_ok"
CITED_DOC_IDS = "aegis.output.cited_doc_ids"

SCOPES = {
    TENANT_ID: "resource",
    EVAL_RUN_ID: "resource",
    SESSION_ID: "span",
    BUNDLE_DIGEST: "span",
    DECISION_ID: "span",
    FAULT_INJECTION_ID: "span",
    ARGS_HASH: "span",
    DOC_IDS: "span",
    SCORES: "span",
    PARSE_OK: "span",
    CITED_DOC_IDS: "span",
}

_MARKER = r"^[A-Za-z0-9._:-]{1,128}$"
PATTERNS = {
    EVAL_RUN_ID: _MARKER,
    FAULT_INJECTION_ID: _MARKER,
    BUNDLE_DIGEST: r"^sha256:[0-9a-f]{64}$",
    ARGS_HASH: r"^[0-9a-f]{16}$",
}


def checked(name: str, value: str) -> str:
    """value, if it is a non-empty string that fits the profile pattern of name."""
    if not isinstance(value, str) or not value:  # pyright: ignore[reportUnnecessaryIsInstance]
        raise ValueError(f"{name} must be a non-empty string")
    pattern = PATTERNS.get(name)
    # fullmatch: with re.match, a trailing newline would pass the pattern's `$`.
    if pattern is not None and re.fullmatch(pattern, value) is None:
        raise ValueError(f"{name} does not match the profile pattern {pattern}")
    return value
