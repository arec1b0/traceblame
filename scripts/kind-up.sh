#!/usr/bin/env bash
# Create the local kind cluster if it does not exist, then wait for all nodes to be Ready.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../tools/versions.env
source "${ROOT}/tools/versions.env"
export PATH="${ROOT}/.tools/bin:${PATH}"

CLUSTER=traceblame

if ! docker info >/dev/null 2>&1; then
  echo "kind-up: docker is not reachable." >&2
  echo "On WSL2: Docker Desktop -> Settings -> Resources -> WSL Integration -> enable this distro." >&2
  exit 1
fi

if kind get clusters | grep -qx "${CLUSTER}"; then
  echo "kind-up: cluster ${CLUSTER} already exists"
else
  kind create cluster --name "${CLUSTER}" --config "${ROOT}/deploy/kind/kind-config.yaml" \
    --image "${KIND_NODE_IMAGE}" --wait 180s
fi

kubectl --context "kind-${CLUSTER}" wait --for=condition=Ready nodes --all --timeout=180s
kubectl --context "kind-${CLUSTER}" get nodes
