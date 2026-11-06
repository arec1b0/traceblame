"""tools/faults/provenance.py (#65): the images kind ran and the migrations both databases had
applied, at the start and the end of a phase, and what fails: nothing found, nothing read, a
change in between, an eval pod that is not the image built for the run. The cluster is a fake
kubectl answering with the shapes `kubectl get pods -o json` and the two `schema_migrations`
queries return; the digests and names are test doubles, never a run's data."""

import base64
import io
import json
import subprocess
import tarfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import provenance
import pytest

NOW = datetime(2026, 11, 6, 9, 0, 0, tzinfo=UTC)
NODE = "traceblame-worker"
CONFIG = "sha256:" + "c" * 64  # the eval image's config digest, built and on the node
PASSWORD = "s3cret-password"  # noqa: S105 - a test double
ROOT = Path(__file__).resolve().parents[2]


def _id(n: int) -> str:
    return f"docker.io/library/import-2026-11-06@sha256:{n:064x}"


def pod(
    namespace: str,
    name: str,
    labels: dict[str, str],
    image_id: str,
    *,
    container: str = "main",
    image: str = "app:tag",
    phase: str = "Running",
    deleting: bool = False,
) -> dict[str, Any]:
    meta: dict[str, Any] = {"namespace": namespace, "name": name, "labels": labels}
    if deleting:
        meta["deletionTimestamp"] = "2026-11-06T09:00:00Z"
    return {
        "metadata": meta,
        "spec": {"nodeName": NODE, "containers": [{"name": container, "image": image}]},
        "status": {
            "phase": phase,
            "containerStatuses": [
                {"name": container, "image": f"docker.io/library/{image}", "imageID": image_id}
            ],
        },
    }


GATEWAY = {"app": "otel-gateway"}


def good_pods() -> list[dict[str, Any]]:
    return [
        pod("observability", "otel-agent-a", {"app": "otel-agent"}, _id(1)),
        pod("observability", "otel-agent-b", {"app": "otel-agent"}, _id(1)),
        pod("observability", "otel-gateway-a", {"app": "otel-gateway"}, _id(2)),
        pod("traceblame", "trace-assembler-a", {"app": "trace-assembler"}, _id(3)),
        pod(
            "observability", "chi-traceblame-0",
            {"clickhouse.altinity.com/chi": "traceblame"}, _id(4),
        ),
        pod("observability", "nats-0", {"app": "nats"}, _id(5)),
        pod("traceblame", "postgres-0", {"app": "postgres"}, _id(6)),  # not a traffic role
    ]  # fmt: skip


def eval_pod(image: str = "runbook-agent:abc-faults", image_id: int = 7) -> dict[str, Any]:
    return pod(
        "runbook-sandbox", "runbook-agent-x", {"app": "runbook-agent"}, _id(image_id),
        container="runbook-agent", image=image,
    )  # fmt: skip


CH = (
    "0001 spans (expand, ec39fa3ba092, 2026-11-05T13:23:49Z); "
    "0002 trace_arrivals (expand, 06f38018e34f, 2026-11-05T13:23:51Z)"
)
PG = "0001 assembler_partitions (expand, 8ae1a3710e21, 2026-11-04T15:31:58Z)"


class Cluster:
    """A fake kubectl: every call is kept; .pods, .ch and .pg are what it answers with."""

    def __init__(self, pods: list[dict[str, Any]] | None = None) -> None:
        self.pods = good_pods() if pods is None else pods
        self.ch = CH
        self.pg = PG
        self.fail_exec: set[str] = set()
        self.calls: list[tuple[str, ...]] = []
        self.inputs: list[str | None] = []

    def __call__(self, *args: str, input: str | None = None) -> str:
        self.calls.append(args)
        self.inputs.append(input)
        if args[:2] == ("get", "pods"):
            return json.dumps({"items": self.pods})
        if "secret" in args:
            return base64.b64encode(
                (PASSWORD if "password" in args[-1] else "tb-user").encode()
            ).decode()
        assert "exec" in args, args
        database = "postgres" if "postgres-0" in args else "clickhouse"
        if database in self.fail_exec:
            raise subprocess.CalledProcessError(
                1, ["kubectl", *args], stderr="error: pod not found\n"
            )
        return self.ch if database == "clickhouse" else self.pg


class Node:
    """A fake `crictl images -o json` per kind node: .images[node] is [(id, repoDigests)]."""

    def __init__(self) -> None:
        self.images: dict[str, list[tuple[str, list[str]]]] = {NODE: [(CONFIG, [_id(7)])]}
        self.fail = False
        self.asked: list[str] = []

    def __call__(self, node: str) -> str:
        self.asked.append(node)
        if self.fail:
            raise subprocess.CalledProcessError(
                1, ["docker", "exec", node, "crictl"], stderr="Error: no such container\n"
            )
        return json.dumps(
            {"images": [{"id": i, "repoDigests": d} for i, d in self.images.get(node, [])]}
        )


def run_phase(
    cluster: Cluster,
    body: Callable[[provenance.Recorder], None] | None = None,
    *,
    eval_image: provenance.EvalImage | None = None,
    images: Node | None = None,
) -> tuple[dict[str, Any], list[str]]:
    state: dict[str, Any] = {}
    logs: list[str] = []
    with provenance.phase(
        state,
        "run",
        kubectl=cluster,
        log=logs.append,
        eval_image=eval_image,
        now=lambda: NOW,
        images=images or Node(),
    ) as rec:
        if body is not None:
            body(rec)
    return state["provenance"]["run"], logs


def problems_of(
    cluster: Cluster,
    body: Callable[[provenance.Recorder], None] | None = None,
    eval_image: provenance.EvalImage | None = None,
    images: Node | None = None,
) -> list[str]:
    return run_phase(cluster, body, eval_image=eval_image, images=images)[0]["problems"]


# what is recorded -------------------------------------------------------------------------


def test_an_unchanged_phase_records_every_role_and_both_schemas_without_problems() -> None:
    rec, logs = run_phase(Cluster())
    assert rec["problems"] == []
    assert rec["start"] == rec["end"]
    images = rec["end"]["images"]
    assert sorted(images) == ["clickhouse", "nats", "otel-agent", "otel-gateway", "trace-assembler"]
    assert [c["pod"] for c in images["otel-agent"]] == ["otel-agent-a", "otel-agent-b"]
    assert images["nats"][0]["image_id"] == _id(5)
    assert images["nats"][0]["image"] == "app:tag"
    assert [m["version"] for m in rec["end"]["migrations"]["clickhouse"]] == ["0001", "0002"]
    assert rec["end"]["migrations"]["postgres"][0]["name"] == "assembler_partitions"
    assert rec["end"]["at"] == "2026-11-06T09:00:00Z"
    assert logs[0].startswith(
        "provenance run: 6 container images, 3 applied migrations, 0 problems"
    )


def test_pods_that_are_not_running_or_are_being_deleted_are_not_what_ran() -> None:
    cluster = Cluster(
        [
            *good_pods(),
            pod("observability", "otel-gateway-old", GATEWAY, _id(99), deleting=True),
            pod("observability", "otel-gateway-pending", GATEWAY, "", phase="Pending"),
        ]
    )  # fmt: skip
    rec, _ = run_phase(cluster)
    assert rec["problems"] == []
    assert [c["pod"] for c in rec["end"]["images"]["otel-gateway"]] == ["otel-gateway-a"]


def test_the_migrations_query_is_the_benchs_file_and_the_password_never_reaches_argv() -> None:
    cluster = Cluster()
    run_phase(cluster)
    execs = [(a, i) for a, i in zip(cluster.calls, cluster.inputs, strict=True) if "exec" in a]
    assert len(execs) == 4  # two databases, start and end
    ch_sql = (ROOT / "tools/provenance/clickhouse_schema.sql").read_text(encoding="utf-8").strip()
    pg_sql = (ROOT / "tools/provenance/postgres_schema.sql").read_text(encoding="utf-8").strip()
    assert {a[-1] for a, _ in execs} == {ch_sql, pg_sql}
    assert all(i == PASSWORD for _, i in execs)
    assert all(PASSWORD not in arg for call in cluster.calls for arg in call)


def test_the_bench_and_the_helper_read_the_same_query_files() -> None:
    bench = (ROOT / "bench/gateway-scaling/run.sh").read_text(encoding="utf-8")
    assert "FROM schema_migrations" not in bench  # no copy of the query in the script
    assert "tools/provenance/clickhouse_schema.sql" in bench
    assert "tools/provenance/postgres_schema.sql" in bench


def test_migrations_parse_the_benchs_list_and_a_malformed_entry_is_an_error() -> None:
    rows = provenance.parse_migrations(CH + "\n")
    assert rows[1] == {
        "version": "0002",
        "name": "trace_arrivals",
        "phase": "expand",
        "sha256": "06f38018e34f",
        "applied_at": "2026-11-05T13:23:51Z",
    }
    assert provenance.parse_migrations("") == []
    with pytest.raises(ValueError, match="not a schema_migrations entry"):
        provenance.parse_migrations(CH + "; 0003 broken")


# the failure cases (each fails once without its check: see the PR) ------------------------


def test_no_images_found_is_a_problem() -> None:
    problems = problems_of(Cluster([]))
    assert any("no images found" in p for p in problems), problems


def test_a_role_without_a_running_pod_is_a_problem() -> None:
    pods = [p for p in good_pods() if p["metadata"]["name"] != "nats-0"]
    problems = problems_of(Cluster(pods))
    assert any(p.startswith("start: nats: no running pod with an image ID") for p in problems)
    assert not any("no images found" in p for p in problems)


def test_a_container_without_an_image_id_is_a_problem() -> None:
    pods = good_pods()
    pods[2]["status"]["containerStatuses"][0]["imageID"] = ""
    problems = problems_of(Cluster(pods))
    assert any(
        "otel-gateway: pod otel-gateway-a container main has no image ID" in p for p in problems
    )


@pytest.mark.parametrize("database", ["clickhouse", "postgres"])
def test_no_migrations_read_is_a_problem(database: str) -> None:
    cluster = Cluster()
    setattr(cluster, "ch" if database == "clickhouse" else "pg", "")
    problems = problems_of(cluster)
    assert any(f"{database}: no migrations read" in p for p in problems), problems
    assert not any("changed" in p for p in problems)


def test_migrations_that_changed_between_start_and_end_are_a_problem() -> None:
    cluster = Cluster()

    def another_session_migrates(_: Any) -> None:
        cluster.ch = CH + "; 0003 findings_v2 (expand, 6dcc9ca329c1, 2026-11-06T09:30:00Z)"

    problems = problems_of(cluster, body=another_session_migrates)
    assert any(
        "clickhouse: applied migrations changed during the phase" in p
        and "added 0003 findings_v2" in p
        for p in problems
    ), problems
    assert not any(p.startswith("postgres") for p in problems)


def test_image_ids_that_changed_between_start_and_end_are_a_problem() -> None:
    cluster = Cluster()

    def gateway_rolled(_: Any) -> None:
        cluster.pods[2]["status"]["containerStatuses"][0]["imageID"] = _id(42)

    problems = problems_of(cluster, body=gateway_rolled)
    assert any("otel-gateway: image IDs changed during the phase" in p for p in problems), problems
    assert not any(p.startswith("otel-agent") for p in problems)


EVAL = provenance.EvalImage("runbook-agent:abc-faults", "sha256:" + "e" * 64, CONFIG)


def test_an_eval_pod_running_the_image_built_for_the_run_is_fine_and_recorded() -> None:
    cluster = Cluster([*good_pods(), eval_pod()])
    rec, _ = run_phase(cluster, lambda r: r.eval_pod("baseline"), eval_image=EVAL)
    assert rec["problems"] == []
    assert rec["eval"] == {
        "image": EVAL.tag,
        "built_image_id": EVAL.image_id,
        "built_config_id": CONFIG,
    }
    sample = rec["eval_pods"][0]
    assert sample["label"] == "baseline"
    assert sample["pods"][0]["image_id"] == _id(7)
    assert sample["pods"][0]["node"] == NODE
    assert sample["pods"][0]["node_config_ids"] == [CONFIG]


def test_an_eval_pod_that_is_not_the_image_built_for_the_run_is_a_problem() -> None:
    cluster = Cluster([*good_pods(), eval_pod(image="runbook-agent:other-faults")])
    problems = problems_of(cluster, body=lambda r: r.eval_pod("baseline"), eval_image=EVAL)
    assert any(
        "eval pod runbook-agent-x runs runbook-agent:other-faults, the image built for this run"
        " is runbook-agent:abc-faults" in p
        for p in problems
    ), problems


def test_no_eval_pod_found_is_a_problem() -> None:
    cluster = Cluster()
    problems = problems_of(cluster, body=lambda r: r.eval_pod("baseline"), eval_image=EVAL)
    assert any("baseline: no eval pod found" in p for p in problems), problems
    assert any("no eval pod" in p for p in problems_of(Cluster(), eval_image=EVAL))  # never sampled


def test_an_eval_pod_whose_image_id_changed_between_deploys_is_a_problem() -> None:
    cluster = Cluster([*good_pods(), eval_pod()])

    def second_deploy(r: provenance.Recorder) -> None:
        r.eval_pod("baseline")
        cluster.pods[-1]["status"]["containerStatuses"][0]["imageID"] = _id(8)
        r.eval_pod("variant")

    problems = problems_of(cluster, body=second_deploy, eval_image=EVAL)
    assert any("eval pod image IDs differ between deploys" in p for p in problems), problems


def _eval_problems(
    images: Node | None = None, eval_image: provenance.EvalImage = EVAL
) -> list[str]:
    return problems_of(
        Cluster([*good_pods(), eval_pod()]),
        body=lambda r: r.eval_pod("baseline"),
        eval_image=eval_image,
        images=images,
    )


def test_a_node_config_digest_that_is_not_the_built_one_is_a_problem() -> None:
    node = Node()
    node.images[NODE] = [("sha256:" + "d" * 64, [_id(7)])]  # same imageID, another image
    problems = _eval_problems(node)
    assert any(
        "baseline: eval pod runbook-agent-x: node traceblame-worker holds config digest"
        " dddddddddddd for its image ID, the image built for this run has cccccccccccc" in p
        for p in problems
    ), problems


def test_a_node_without_an_image_for_the_pods_image_id_is_a_problem() -> None:
    node = Node()
    node.images[NODE] = [(CONFIG, [_id(99)])]  # an image, but not the pod's
    problems = _eval_problems(node)
    assert any("node traceblame-worker has no image with the pod's image ID" in p for p in problems)


def test_several_node_images_with_the_pods_image_id_are_a_problem() -> None:
    node = Node()
    node.images[NODE] = [(CONFIG, [_id(7)]), ("sha256:" + "d" * 64, [_id(7)])]
    problems = _eval_problems(node)
    assert any("node traceblame-worker has several images" in p for p in problems), problems


def test_a_build_without_a_config_digest_is_a_problem() -> None:
    problems = _eval_problems(eval_image=provenance.EvalImage(EVAL.tag, EVAL.image_id, None))
    assert any("no built config digest recorded for the eval image" in p for p in problems)


def test_a_failed_node_read_is_a_problem_without_its_command_line() -> None:
    node = Node()
    node.fail = True
    rec, _ = run_phase(
        Cluster([*good_pods(), eval_pod()]),
        lambda r: r.eval_pod("baseline"),
        eval_image=EVAL,
        images=node,
    )
    assert "baseline: eval pod: read failed: exit 1: Error: no such container" in rec["problems"]
    assert not any("docker" in p or "crictl" in p for p in rec["problems"])


def test_the_config_digest_check_does_not_replace_the_tag_signal() -> None:
    # right config digest, wrong tag: both signals are reported, the tag one as its own problem
    cluster = Cluster([*good_pods(), eval_pod(image="runbook-agent:other-faults")])
    problems = problems_of(cluster, body=lambda r: r.eval_pod("b"), eval_image=EVAL)
    assert len(problems) == 1
    assert "runs runbook-agent:other-faults" in problems[0]


def test_config_ids_read_the_nodes_images_by_the_pods_repo_digest() -> None:
    text = json.dumps(
        {
            "images": [
                {"id": "sha256:aa", "repoDigests": ["x@sha256:1", "y@sha256:2"]},
                {"id": "sha256:bb", "repoDigests": []},
                {"id": "sha256:aa", "repoDigests": ["z@sha256:2"]},
            ]
        }
    )
    assert provenance.config_ids(text, "y@sha256:2") == ["sha256:aa"]
    assert provenance.config_ids(text, "sha256:2") == []  # the whole repoDigest, not a suffix
    assert provenance.config_ids(text, "x@sha256:1") == ["sha256:aa"]
    assert provenance.config_ids(text, "") == []
    assert provenance.config_ids(text, "z@sha256:2") == ["sha256:aa"]


def _archive(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class _Save:
    """A fake `docker save` process streaming an archive."""

    def __init__(self, data: bytes) -> None:
        self.stdout: Any = io.BytesIO(data)
        self.killed = False

    def kill(self) -> None:
        self.killed = True

    def wait(self) -> int:
        return 0


def _save(monkeypatch: pytest.MonkeyPatch, members: dict[str, bytes]) -> _Save:
    proc = _Save(_archive(members))

    def popen(*_args: Any, **_kwargs: Any) -> _Save:
        return proc

    monkeypatch.setattr(provenance.subprocess, "Popen", popen)
    return proc


def test_the_built_config_digest_is_read_from_the_streamed_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = json.dumps([{"Config": f"blobs/sha256/{'c' * 64}", "Layers": []}]).encode()
    proc = _save(monkeypatch, {"blobs/sha256/" + "a" * 64: b"x" * 5000, "manifest.json": manifest})
    assert provenance.built_config_digest("t:1") == CONFIG
    assert proc.killed  # the rest of the stream is never read


HEX = "c" * 64


@pytest.mark.parametrize(
    "config",
    [f"blobs/sha256/{HEX}", f"{HEX}.json"],
    ids=["containerd image store", "classic image store"],
)
def test_both_docker_archive_layouts_give_the_config_digest(
    monkeypatch: pytest.MonkeyPatch, config: str
) -> None:
    manifest = json.dumps([{"Config": config, "Layers": []}]).encode()
    _save(monkeypatch, {"manifest.json": manifest})
    assert provenance.built_config_digest("t:1") == f"sha256:{HEX}"


@pytest.mark.parametrize(
    "config",
    [
        f"blobs/sha256/{HEX[:-1]}",  # 63 characters
        f"blobs/sha256/{HEX}0",  # 65
        f"blobs/sha256/{'C' * 64}",  # uppercase
        f"blobs/sha256/{'g' * 64}",  # not hex
        f"{HEX[:-1]}.json",
        f"{'g' * 64}.json",
        f"{HEX}.txt",
        f"blobs/sha512/{HEX}",
        f"other/{HEX}",
        f"blobs/sha256/{HEX}.json",  # both layouts at once
        f"../{HEX}.json",
        "",
    ],
)
def test_a_config_that_is_not_64_lowercase_hex_in_a_known_layout_is_an_error(
    monkeypatch: pytest.MonkeyPatch, config: str
) -> None:
    _save(monkeypatch, {"manifest.json": json.dumps([{"Config": config}]).encode()})
    with pytest.raises(ValueError, match="unexpected Config"):
        provenance.built_config_digest("t:1")


@pytest.mark.parametrize(
    ("members", "match"),
    [
        ({"index.json": b"{}"}, "no manifest.json"),
        ({"manifest.json": json.dumps([{"Config": "config.json"}]).encode()}, "unexpected Config"),
    ],
)
def test_a_save_without_a_usable_manifest_is_an_error(
    monkeypatch: pytest.MonkeyPatch, members: dict[str, bytes], match: str
) -> None:
    _save(monkeypatch, members)
    with pytest.raises((LookupError, ValueError), match=match):
        provenance.built_config_digest("t:1")


def test_a_change_in_the_optional_content_phase_is_a_problem_too() -> None:
    cluster = Cluster()
    ok, _ = run_phase(cluster)
    changed, _ = run_phase(cluster, lambda _: setattr(cluster, "pg", ""))
    problems = provenance.run_problems(
        {"run": ok, "record": ok, "check": ok, "content": changed}, "agentic-rag"
    )
    assert any(p.startswith("content: ") and "postgres: no migrations read" in p for p in problems)


@pytest.mark.parametrize("database", ["clickhouse", "postgres"])
def test_a_failed_read_is_recorded_as_a_problem_without_its_command_line(database: str) -> None:
    cluster = Cluster()
    cluster.fail_exec.add(database)
    rec, _ = run_phase(cluster)
    assert any(p.startswith(f"start: {database}: read failed") for p in rec["problems"])
    assert not any("no migrations read" in p for p in rec["problems"])  # one problem, not two
    assert rec["start"]["errors"][database] == "exit 1: error: pod not found"
    assert all("exec" not in e and "kubectl" not in e for e in rec["start"]["errors"].values())


# the problems the report re-derives -------------------------------------------------------


def test_a_run_recorded_before_the_provenance_has_no_problems_and_a_new_run_needs_every_phase() -> (
    None
):
    assert provenance.run_problems(None, "agentic-rag") == []
    rec, _ = run_phase(Cluster())
    problems = provenance.run_problems({"run": rec}, "agentic-rag")
    assert problems == [
        "provenance: no record for the record phase",
        "provenance: no record for the check phase",
    ]


def test_the_report_rederives_problems_from_the_snapshots_not_from_the_stored_list() -> None:
    cluster = Cluster()
    rec, _ = run_phase(cluster)
    rec["end"]["migrations"]["postgres"] = []  # an edited record
    assert rec["problems"] == []
    problems = provenance.run_problems({"run": rec, "record": rec, "check": rec}, "agentic-rag")
    assert "run: end: postgres: no migrations read" in problems, problems


def test_a_runbook_agent_run_needs_its_eval_pod_sampled_in_the_run_phase() -> None:
    rec, _ = run_phase(Cluster())
    rec2, _ = run_phase(
        Cluster([*good_pods(), eval_pod()]), lambda r: r.eval_pod("b"), eval_image=EVAL
    )
    full = {"record": rec, "check": rec}
    assert any(
        "run: no eval pod" in p
        for p in provenance.run_problems({"run": rec, **full}, "runbook-agent")
    )
    assert provenance.run_problems({"run": rec2, **full}, "runbook-agent") == []
