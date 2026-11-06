# ADR-0009: Image identity of a fault run: what a pod's imageID can be compared with

- **Status:** Proposed
- **Date:** 2026-11-06
- **Issue:** #65 (follow-up to #14, PR #64). The number is the next free one; it is assigned at
  merge.
- **Decider:** Daniil Krizhanovskyi

## Context

#65 asks the fault drivers to record, for every pod the run's traffic goes through, the image
kind actually ran (`status.containerStatuses[].imageID`), and to treat "an eval image whose pod
`imageID` differs from the built `eval_image_id`" as a problem. The second part assumes the two
IDs are the same kind of identifier. They are not, and a comparison of the strings can never
succeed.

Measured on 2026-11-06 on the kind cluster that still holds the 2026-11-05 validation run's eval
image (read-only, under `/tmp/traceblame-kind.lock`; the eval pod runs on `traceblame-worker`), for the runbook-agent eval image
`runbook-agent:92fb6f8-tbb1b6b60-faults`:

| Where | Identifier | Value |
|---|---|---|
| `docker image inspect .Id` (the build's `eval_image_id`; Docker 29.5.2, containerd image store) | OCI **index** digest | `sha256:a23e74792c6b...` |
| `docker save`, `manifest.json` `Config` | image **config** digest | `sha256:0a9038910b93...` |
| kind node, `crictl inspecti <tag>` `.status.id` | image **config** digest | `sha256:0a9038910b93...` |
| kind node, `crictl images -o json`, the entry whose `repoDigests` holds the pod's imageID: `.id` | image **config** digest | `sha256:0a9038910b93...` |
| same entry, `.repoDigests` | **manifest** digest of the node's own import | `docker.io/library/import-2026-11-05@sha256:00db61ab474e...` |
| pod `status.containerStatuses[].imageID` | the same node manifest digest | `docker.io/library/import-2026-11-05@sha256:00db61ab474e...` |

`kind load docker-image` exports the image and imports it again into each node's containerd
under a name `import-<date>`; containerd builds a new manifest for it, so the digest the pod
reports is not in the saved archive (its blobs hold the index `a23e...` and the config `0a90...`,
not `00db...`) and cannot be computed from Docker's side. Only the config digest is the same on
both sides, and the pod status does not expose it. The pipeline images show the same
`import-<date>@sha256:` form (otel agent, gateway, trace-assembler, loadgen); the registry
images (ClickHouse, NATS, Postgres) show their registry digest.

`crictl inspecti` resolves the image by its tag but not by the pod's imageID: given
`docker.io/library/import-2026-11-05@sha256:00db...`, `sha256:00db...` or the bare hex it answers
`no such image`, so a lookup by tag would prove the tag, not the image the pod ran. `crictl
images -o json` lists every image of the node with its `id` and `repoDigests`, and the entry
whose `repoDigests` holds the pod's imageID ties the pod to its config digest.

So the check as written (string equality of the pod's imageID and the built `eval_image_id`)
would fail on every run or, if relaxed until it passes, check nothing.

## Decision

The proof that the eval pod ran the image built for the run is **equality of config digests**,
read in two read-only hops:

1. At build, `eval_config_id`: the `Config` digest from `docker save <tag>`'s `manifest.json`,
   streamed (the archive is never written), stored next to `eval_image_id` in the state.
2. In the run phase, for every sample of the eval pod (after each deploy, and at the end): the
   pod's node name from the pod spec, then `docker exec <node> crictl images -o json`; the
   config digests (`id`) of the entries whose `repoDigests` hold the pod's imageID. Exactly one
   such digest must exist and it must equal `eval_config_id`. None, several, a different one, a
   read that fails, or a build without `eval_config_id` are problems.

Measured live on 2026-11-06 for the eval pod above: built `sha256:0a9038910b93...`, node
`sha256:0a9038910b93...`, equal; with a wrong expected digest the check fails with the node's
and the expected digest in the message.

Two signals stay as additional checks, not as the proof: the pod runs the built tag
(`spec.containers[].image` equals the tag), and it keeps one imageID across the deploys of the
phase. The report shows the proof first, then these, and names the build's `eval_image_id` for
reference only (an index digest a node never reports).

The pipeline images (otel agent and gateway, trace-assembler, ClickHouse, NATS) have no built
counterpart to compare with; for them the check is that the imageIDs at the end of each phase
equal those at its start.

## Alternatives considered

- **Compare the strings (the issue as written).** Never equal, see above.
- **Tag only.** Sound for what it says, but a stale image under the same tag would pass; the
  tag (`<app commit>-tb<traceblame commit>-faults`) makes that unlikely, not impossible. Kept
  as a signal.
- **`crictl inspecti <tag>`.** Returns the right config digest here, but it is looked up by the
  tag, so it does not tie the digest to the pod.
- **Deploy by digest from a local registry in kind.** The pod's imageID would equal the pushed
  digest. It changes `make kind-deploy` and the capture drivers and needs a registry in the
  cluster (a long-running service): a larger change than the check justifies.
- **Layer diff IDs instead of the config digest.** Equal on both sides, but they leave out the
  config (`NODE_OPTIONS`, entrypoint), which is exactly what the eval image adds.

## Consequences

- The `docker save` at build streams about 125 MB per eval image. The `docker exec` into the
  kind nodes depends on the node container names (`traceblame-worker`, `traceblame-worker2`,
  `traceblame-control-plane`, equal to the node names) and reads the node's containerd, not
  the API server. The helper validates the node name before passing it to Docker.
- A run recorded before this has no `eval_config_id`: its report says "not recorded (predates
  #65)", and a run built before it but run after it fails with "no built config digest
  recorded" and must be rebuilt.
- If `crictl` is missing from a node image or stops returning `id` and `repoDigests`, the check
  reports a problem instead of passing; the tag and imageID signals alone are never enough.
