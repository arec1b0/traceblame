"""The sandbox's alert rules against their upstream text (#13).

deploy/kind/runbook/monitoring/rules.yml takes rules from samber/awesome-prometheus-alerts
(vendored in upstream/, sha256 in tools/checksums.txt). Expressions and annotations must be
verbatim; overrides() lists every other field that differs, which each fixture's provenance
records and tests/scripts/test_runbook_rules.py pins.
"""

import hashlib
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
RULES = ROOT / "deploy" / "kind" / "runbook" / "monitoring" / "rules.yml"
UPSTREAM_COMMIT = "736370e922cee74db497391c825a7a6bd3d48f17"
UPSTREAM_REPOSITORY = "https://github.com/samber/awesome-prometheus-alerts"
UPSTREAM_FILES = {
    "kubestate-exporter.yml": "dist/rules/kubernetes/kubestate-exporter.yml",
    "blackbox-exporter.yml": "dist/rules/blackbox/blackbox-exporter.yml",
}
# Local rule -> the upstream rule it is taken from.
SOURCES = {
    "OOMKilled": "KubernetesContainerOomKiller",
    "HighRequestLatency": "BlackboxProbeSlowHttp",
    "KubernetesPodCrashLooping": "KubernetesPodCrashLooping",
    "KubernetesPodNotHealthy": "KubernetesPodNotHealthy",
}
VERBATIM = ("expr", "annotations")


def _checksum(name: str) -> str:
    want = f"awesome-prometheus-alerts-{UPSTREAM_COMMIT[:7]}-{name}"
    for line in (ROOT / "tools" / "checksums.txt").read_text(encoding="utf-8").splitlines():
        digest, _, file = line.partition("  ")
        if file == want:
            return digest
    raise LookupError(f"no checksum for {want} in tools/checksums.txt")


def upstream_rules() -> dict[str, dict[str, Any]]:
    """Every upstream rule by name, from the vendored files after their sha256 check."""
    out: dict[str, dict[str, Any]] = {}
    for name in UPSTREAM_FILES:
        data = (HERE / "upstream" / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != _checksum(name):
            raise ValueError(f"upstream/{name} does not match its pinned sha256")
        for group in yaml.safe_load(data)["groups"]:
            for rule in group["rules"]:
                out[rule["alert"]] = rule
    return out


def local_rules() -> dict[str, dict[str, Any]]:
    groups = yaml.safe_load(RULES.read_text(encoding="utf-8"))["groups"]
    return {rule["alert"]: rule for group in groups for rule in group["rules"]}


def overrides() -> list[dict[str, Any]]:
    """Each field that differs from upstream: rule, upstream rule, field, original, used.
    Raises ValueError if an expression or an annotation differs, or a rule has no source."""
    upstream = upstream_rules()
    out: list[dict[str, Any]] = []
    for name, rule in local_rules().items():
        source = SOURCES.get(name)
        if source is None or source not in upstream:
            raise ValueError(f"rule {name} has no upstream source")
        original = upstream[source]
        for field in VERBATIM:
            if rule.get(field) != original.get(field):
                raise ValueError(f"rule {name}: {field} differs from upstream {source}")
        fields: list[tuple[str, Any, Any]] = []
        if rule["alert"] != original["alert"]:
            fields.append(("alert", original["alert"], rule["alert"]))
        if rule.get("for") != original.get("for"):
            fields.append(("for", original.get("for"), rule.get("for")))
        labels, original_labels = rule.get("labels", {}), original.get("labels", {})
        for key in sorted(set(labels) | set(original_labels)):
            if labels.get(key) != original_labels.get(key):
                fields.append((f"labels.{key}", original_labels.get(key), labels.get(key)))
        unknown = set(rule) - {"alert", "for", "labels", *VERBATIM}
        if unknown:
            raise ValueError(f"rule {name}: unexpected fields {sorted(unknown)}")
        out += [
            {"rule": name, "upstream": source, "field": f, "original": o, "used": u}
            for f, o, u in fields
        ]
    return out


def provenance() -> dict[str, Any]:
    return {
        "repository": UPSTREAM_REPOSITORY,
        "commit": UPSTREAM_COMMIT,
        "files": [
            {"path": path, "sha256": _checksum(name)} for name, path in UPSTREAM_FILES.items()
        ],
        "license": "CC BY 4.0",
        "verbatim": list(VERBATIM),
        "overrides": overrides(),
    }
