"""What a fault run ran on (#65): the images kind actually ran and the migrations both databases
had applied, read at the start and the end of every kind phase, and the problems when they
changed in between or when a read found nothing (a check that checked nothing fails).

Read-only: one `kubectl get pods`, a `schema_migrations` query in each database through
`kubectl exec`, and for the eval pod `crictl images` on its kind node (`docker exec`). The caller
holds the kind lock (flock /tmp/traceblame-kind.lock). The two queries are the bench's
(bench/gateway-scaling/run.sh reads the same files in tools/provenance/).
Passwords go to the pod through stdin, never through an argument.
"""

import base64
import json
import re
import subprocess
import sys
import tarfile
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple, Protocol, cast

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "fixtures"))

from kindlib import ROOT  # noqa: E402

SQL = ROOT / "tools" / "provenance"
# The pods the run's traffic goes through: role -> (namespace, label key, label value). Pod
# `imageID` is what the container runtime ran, not what was asked for.
ROLES: dict[str, tuple[str, str, str]] = {
    "otel-agent": ("observability", "app", "otel-agent"),
    "otel-gateway": ("observability", "app", "otel-gateway"),
    "trace-assembler": ("traceblame", "app", "trace-assembler"),
    "clickhouse": ("observability", "clickhouse.altinity.com/chi", "traceblame"),
    "nats": ("observability", "app", "nats"),
}
EVAL_POD = ("runbook-sandbox", "app", "runbook-agent")
DATABASES = ("clickhouse", "postgres")
POSTGRES_POD = ("traceblame", "postgres-0")
# One entry of the bench's aggregated list: `0001 name (phase, sha256[:12], applied_at)`.
_MIGRATION = re.compile(r"^(\d{4}) (\S+) \((\w+), ([0-9a-f]{12}), (\S+)\)$")


class Kubectl(Protocol):
    def __call__(self, *args: str, input: str | None = None) -> str: ...


class EvalImage(NamedTuple):
    """The eval image as built: its tag, `docker image inspect .Id` (an OCI index digest) and
    its config digest, the one identifier that is the same in Docker and in the kind node's
    containerd (docs/adr/0009-fault-run-image-identity.md). config_id is None for a build that
    did not record it, which the check reports."""

    tag: str
    image_id: str
    config_id: str | None


# `Config` of manifest.json in either of Docker's archive layouts: 64 lowercase hex digits.
_CONFIG_PATH = re.compile(r"blobs/sha256/(?P<blob>[0-9a-f]{64})|(?P<classic>[0-9a-f]{64})\.json")
NodeImages = Callable[[str], str]  # kind node name -> `crictl images -o json`
_NODE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,62}$")


def _stamp(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# reading ----------------------------------------------------------------------------------


def _pods(kubectl: Kubectl) -> list[dict[str, Any]]:
    out = json.loads(kubectl("get", "pods", "-A", "-o", "json"))
    return cast("list[dict[str, Any]]", out["items"])


def _live(pod: dict[str, Any]) -> bool:
    """Running and not being deleted (a rolled-out pod is not what the traffic went through)."""
    return pod.get("status", {}).get("phase") == "Running" and not pod["metadata"].get(
        "deletionTimestamp"
    )


def _containers(pod: dict[str, Any]) -> list[dict[str, str]]:
    asked = {c["name"]: str(c["image"]) for c in pod.get("spec", {}).get("containers", [])}
    return [
        {
            "pod": str(pod["metadata"]["name"]),
            "container": str(s["name"]),
            "image": asked.get(str(s["name"]), str(s.get("image", ""))),
            "image_id": str(s.get("imageID", "")),
            "node": str(pod.get("spec", {}).get("nodeName", "")),
        }
        for s in pod.get("status", {}).get("containerStatuses", [])
    ]


def _select(pods: list[dict[str, Any]], where: tuple[str, str, str]) -> list[dict[str, str]]:
    namespace, key, value = where
    out: list[dict[str, str]] = []
    for pod in pods:
        labels = pod["metadata"].get("labels", {})
        if pod["metadata"]["namespace"] == namespace and labels.get(key) == value and _live(pod):
            out += _containers(pod)
    return sorted(out, key=lambda c: (c["pod"], c["container"]))


def read_images(pods: list[dict[str, Any]]) -> dict[str, list[dict[str, str]]]:
    return {role: _select(pods, where) for role, where in ROLES.items()}


def parse_migrations(text: str) -> list[dict[str, str]]:
    """The bench's aggregated list (`; `-separated) as rows; a part that is not one of its
    entries is an error, never skipped."""
    rows: list[dict[str, str]] = []
    for part in (p.strip() for p in text.strip().split(";")):
        if not part:
            continue
        found = _MIGRATION.match(part)
        if found is None:
            raise ValueError(f"not a schema_migrations entry: {part!r}")
        version, name, phase, sha, applied = found.groups()
        rows.append(
            {
                "version": version,
                "name": name,
                "phase": phase,
                "sha256": sha,
                "applied_at": applied,
            }
        )
    return rows


def _secret(kubectl: Kubectl, namespace: str, name: str, key: str) -> str:
    raw = kubectl("-n", namespace, "get", "secret", name, "-o", f"jsonpath={{.data.{key}}}")
    return base64.b64decode(raw).decode()


def read_migrations(
    kubectl: Kubectl, database: str, pods: list[dict[str, Any]]
) -> list[dict[str, str]]:
    """schema_migrations of one database, with the bench's query, as the traceblame user."""
    if database == "clickhouse":
        found = _select(pods, ROLES["clickhouse"])
        if not found:
            raise LookupError("no running ClickHouse pod")
        namespace, pod, secret = "observability", found[0]["pod"], "clickhouse-credentials"
        query = (SQL / "clickhouse_schema.sql").read_text(encoding="utf-8").strip()
        script = (
            'exec clickhouse-client --user "$1" --password "$(cat)" --database traceblame'
            ' --query "$2"'
        )
        container = ["-c", "clickhouse"]
    else:
        namespace, pod = POSTGRES_POD
        secret = "postgres-credentials"  # noqa: S105 - the Secret's name
        query = (SQL / "postgres_schema.sql").read_text(encoding="utf-8").strip()
        script = 'PGPASSWORD="$(cat)" exec psql -h 127.0.0.1 -U "$1" -d traceblame -Atqc "$2"'
        container = []
    user = _secret(kubectl, namespace, secret, "user")
    password = _secret(kubectl, namespace, secret, "password")
    out = kubectl(
        "-n", namespace, "exec", "-i", pod, *container, "--", "sh", "-c", script, "sh", user, query,
        input=password,
    )  # fmt: skip
    return parse_migrations(out)


def snapshot(kubectl: Kubectl, now: datetime) -> dict[str, Any]:
    """The images and migrations now. A failed read is recorded under `errors` (and is a
    problem), not raised: a two-hour phase must still finish and report it."""
    snap: dict[str, Any] = {
        "at": _stamp(now),
        "images": {},
        "migrations": {db: [] for db in DATABASES},
        "errors": {},
    }
    pods: list[dict[str, Any]] = []
    try:
        pods = _pods(kubectl)
        snap["images"] = read_images(pods)
    except Exception as exc:
        snap["errors"]["images"] = _why(exc)
    for db in DATABASES:
        try:
            snap["migrations"][db] = read_migrations(kubectl, db, pods)
        except Exception as exc:
            snap["errors"][db] = _why(exc)
    return snap


def _why(exc: Exception) -> str:
    """What failed, without the command line: a failed `kubectl exec` carries its arguments."""
    if isinstance(exc, subprocess.CalledProcessError):
        stderr = str(exc.stderr or "").strip().splitlines()
        tail = f": {stderr[-1]}" if stderr else ""
        return f"exit {exc.returncode}{tail}"[:300]
    return f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}"[:300]


def node_images(node: str) -> str:
    """`crictl images -o json` inside a kind node (a Docker container named like the node)."""
    if not _NODE.match(node):
        raise ValueError(f"not a node name: {node!r}")
    out = subprocess.run(  # noqa: S603 - fixed argv, node name checked above
        ["docker", "exec", node, "crictl", "images", "-o", "json"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout


def config_ids(images_json: str, image_id: str) -> list[str]:
    """The config digests (`id`) of the node images whose repoDigests hold the pod's imageID:
    one is the image the pod ran; none or several are for the check to report."""
    found = {
        str(i["id"])
        for i in json.loads(images_json)["images"]
        if image_id and image_id in i.get("repoDigests", [])
    }
    return sorted(found)


def built_config_digest(tag: str) -> str:
    """The config digest of a docker-built image: `Config` of `docker save`'s manifest.json,
    streamed (the archive is never written). Docker's containerd image store writes
    `blobs/sha256/<64 hex>`, its classic store `<64 hex>.json`; anything else is an error."""
    proc = subprocess.Popen(  # noqa: S603 - fixed argv
        ["docker", "save", tag],  # noqa: S607
        stdout=subprocess.PIPE,
    )
    try:
        if proc.stdout is None:
            raise RuntimeError("docker save has no stdout")
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
            for member in tar:
                if member.name == "manifest.json":
                    handle = tar.extractfile(member)
                    if handle is None:
                        raise LookupError("manifest.json of docker save is not a file")
                    config = str(json.loads(handle.read())[0]["Config"])
                    found = _CONFIG_PATH.fullmatch(config)
                    if found is None:
                        raise ValueError(f"unexpected Config in manifest.json: {config!r}")
                    return "sha256:" + (found["blob"] or found["classic"])
        raise LookupError(f"no manifest.json in docker save {tag}")
    finally:
        proc.kill()
        proc.wait()


def read_eval_pods(kubectl: Kubectl, images: NodeImages) -> list[dict[str, Any]]:
    """The eval image's pods, each with the config digests its node holds for the pod's
    imageID (read once per node)."""
    cache: dict[str, str] = {}
    out: list[dict[str, Any]] = []
    for c in _select(_pods(kubectl), EVAL_POD):
        if c["image_id"]:
            if c["node"] not in cache:
                cache[c["node"]] = images(c["node"])
            c = c | {"node_config_ids": config_ids(cache[c["node"]], c["image_id"])}
        out.append(c)
    return out


# checking ---------------------------------------------------------------------------------


def _short(image_id: str) -> str:
    """`...@sha256:<64 hex>` as the digest's first 12 characters, for a message."""
    return image_id.rsplit(":", 1)[-1][:12] or "none"


def snapshot_problems(snap: dict[str, Any], label: str) -> list[str]:
    """Nothing found and nothing read are problems, not empty fields (zero checked fails)."""
    out: list[str] = []
    errors: dict[str, str] = snap.get("errors", {})
    images: dict[str, list[dict[str, str]]] = snap.get("images", {})
    if "images" in errors:
        out.append(f"{label}: images: read failed: {errors['images']}")
    elif not any(images.values()):
        out.append(f"{label}: no images found (no running pod of {', '.join(ROLES)})")
    else:
        for role in ROLES:
            found = images.get(role, [])
            if not found:
                out.append(f"{label}: {role}: no running pod with an image ID")
            out += [
                f"{label}: {role}: pod {c['pod']} container {c['container']} has no image ID"
                for c in found
                if not c["image_id"]
            ]
    for db in DATABASES:
        if db in errors:
            out.append(f"{label}: {db}: read failed: {errors[db]}")
        elif not snap.get("migrations", {}).get(db):
            out.append(f"{label}: {db}: no migrations read")
    return out


def _migration_key(m: dict[str, str]) -> str:
    return f"{m['version']} {m['name']}"


def _changed_migrations(db: str, start: list[dict[str, str]], end: list[dict[str, str]]) -> str:
    if start == end:
        return ""
    a, b = {_migration_key(m): m for m in start}, {_migration_key(m): m for m in end}
    parts = [f"added {k}" for k in sorted(b.keys() - a.keys())]
    parts += [f"removed {k}" for k in sorted(a.keys() - b.keys())]
    parts += [f"{k} differs" for k in sorted(a.keys() & b.keys()) if a[k] != b[k]]
    return f"{db}: applied migrations changed during the phase: {', '.join(parts)}"


def _ids(containers: list[dict[str, str]]) -> list[str]:
    return sorted(f"{c['container']}={c['image_id']}" for c in containers if c["image_id"])


def changes(start: dict[str, Any], end: dict[str, Any]) -> list[str]:
    """What differs between two snapshots: applied migrations per database, image IDs per role.
    A read that failed has nothing to compare (it is its own problem)."""
    out: list[str] = []
    for db in DATABASES:
        if db in start.get("errors", {}) or db in end.get("errors", {}):
            continue
        text = _changed_migrations(db, start["migrations"][db], end["migrations"][db])
        if text:
            out.append(text)
    if "images" not in start.get("errors", {}) and "images" not in end.get("errors", {}):
        for role in ROLES:
            before, after = _ids(start["images"].get(role, [])), _ids(end["images"].get(role, []))
            if before != after:
                out.append(
                    f"{role}: image IDs changed during the phase:"
                    f" {[_short(i) for i in before]} -> {[_short(i) for i in after]}"
                )
    return out


def eval_problems(rec: dict[str, Any]) -> list[str]:
    """The eval image's pod, checked on two levels. The proof: the config digest its node holds
    for the pod's imageID equals the config digest of the image built for this run (the one
    identifier that is the same in Docker and in the node's containerd; the pod's imageID and
    docker's `.Id` never are, docs/adr/0009-fault-run-image-identity.md). Additional signals,
    not the proof: the pod runs the built tag, and keeps one imageID across the phase's deploys."""
    built = rec["eval"].get("built_config_id")
    expected = str(rec["eval"]["image"])
    samples: list[dict[str, Any]] = rec.get("eval_pods", [])
    if not built:
        return ["no built config digest recorded for the eval image (build with the #65 driver)"]
    if not samples:
        return ["no eval pod was sampled"]
    out: list[str] = []
    seen: set[str] = set()
    for sample in samples:
        label = sample["label"]
        if sample.get("error"):
            out.append(f"{label}: eval pod: read failed: {sample['error']}")
            continue
        if not sample["pods"]:
            out.append(f"{label}: no eval pod found")
        for c in sample["pods"]:
            who = f"{label}: eval pod {c['pod']}"
            if c["image"] != expected:
                out.append(f"{who} runs {c['image']}, the image built for this run is {expected}")
            if not c["image_id"]:
                out.append(f"{who} has no image ID")
                continue
            seen.add(c["image_id"])
            held = c.get("node_config_ids", [])
            if not held:
                out.append(f"{who}: node {c['node']} has no image with the pod's image ID")
            elif len(held) > 1:
                out.append(
                    f"{who}: node {c['node']} has several images"
                    f" ({', '.join(_short(i) for i in held)}) with the pod's image ID"
                )
            elif held[0] != built:
                out.append(
                    f"{who}: node {c['node']} holds config digest {_short(held[0])} for its image"
                    f" ID, the image built for this run has {_short(built)}"
                )
    if len(seen) > 1:
        out.append(f"eval pod image IDs differ between deploys: {sorted(_short(i) for i in seen)}")
    return out


def phase_problems(rec: dict[str, Any]) -> list[str]:
    """Every problem of one phase's record, derived from its snapshots (not from what a driver
    stored): each snapshot's, the changes between them, the eval pod's."""
    out = snapshot_problems(rec["start"], "start") + snapshot_problems(rec["end"], "end")
    out += changes(rec["start"], rec["end"])
    if "eval" in rec:
        out += eval_problems(rec)
    return out


PHASES = ("run", "record", "check")


def run_problems(provenance: dict[str, Any] | None, app: str) -> list[str]:
    """The report's problems for one app. A state without `provenance` predates #65: nothing
    was recorded and nothing is claimed. Once recorded, the run, record and check phases each
    need their record, and runbook-agent's run phase its eval pod."""
    if provenance is None:
        return []
    out: list[str] = []
    for name in PHASES:
        if name not in provenance:
            out.append(f"provenance: no record for the {name} phase")
    for name, rec in provenance.items():
        out += [f"{name}: {p}" for p in phase_problems(rec)]
        if app == "runbook-agent" and name == "run" and "eval" not in rec:
            out.append("run: no eval pod was sampled (no eval image recorded)")
    return out


# recording --------------------------------------------------------------------------------


class Recorder:
    """An open phase: the start snapshot is taken; the driver adds the eval pod's samples and
    calls end() once the phase's work is done."""

    def __init__(
        self,
        state: dict[str, Any],
        name: str,
        rec: dict[str, Any],
        kubectl: Kubectl,
        log: Callable[[str], None],
        now: Callable[[], datetime],
        images: NodeImages,
    ) -> None:
        self._images = images
        self._state = state
        self._name = name
        self._rec = rec
        self._kubectl = kubectl
        self._log = log
        self._now = now

    def eval_pod(self, label: str) -> None:
        """The pods running the eval image right now (runbook-agent), after a deploy."""
        sample: dict[str, Any] = {"label": label, "at": _stamp(self._now())}
        try:
            sample["pods"] = read_eval_pods(self._kubectl, self._images)
        except Exception as exc:
            sample["pods"] = []
            sample["error"] = _why(exc)
        self._rec["eval_pods"].append(sample)

    def end(self) -> dict[str, Any]:
        """The end snapshot, the problems, and the record under state["provenance"][name]; the
        caller saves the state."""
        rec = self._rec
        rec["end"] = snapshot(self._kubectl, self._now())
        rec["problems"] = phase_problems(rec)
        self._state.setdefault("provenance", {})[self._name] = rec
        images = sum(len(v) for v in rec["end"]["images"].values())
        migrations = sum(len(v) for v in rec["end"]["migrations"].values())
        self._log(
            f"provenance {self._name}: {images} container images, {migrations} applied"
            f" migrations, {len(rec['problems'])} problems"
        )
        for problem in rec["problems"]:
            self._log(f"provenance {self._name}: {problem}")
        return rec


def begin(
    state: dict[str, Any],
    name: str,
    *,
    kubectl: Kubectl,
    log: Callable[[str], None],
    eval_image: EvalImage | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    images: NodeImages = node_images,
) -> Recorder:
    """Take the start snapshot of a phase. eval_image is the image a phase that deploys the
    eval image built; `images` reads a node's containerd (crictl)."""
    rec: dict[str, Any] = {"start": snapshot(kubectl, now())}
    if eval_image is not None:
        rec["eval"] = {
            "image": eval_image.tag,
            "built_image_id": eval_image.image_id,
            "built_config_id": eval_image.config_id,
        }
        rec["eval_pods"] = []
    return Recorder(state, name, rec, kubectl, log, now, images)


@contextmanager
def phase(
    state: dict[str, Any],
    name: str,
    *,
    kubectl: Kubectl,
    log: Callable[[str], None],
    eval_image: EvalImage | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    images: NodeImages = node_images,
) -> Generator[Recorder]:
    """begin(), the phase's work, end(); nothing is recorded if the work raises."""
    recorder = begin(
        state, name, kubectl=kubectl, log=log, eval_image=eval_image, now=now, images=images
    )
    yield recorder
    recorder.end()
