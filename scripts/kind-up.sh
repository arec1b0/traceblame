#!/usr/bin/env bash
# Create the local kind cluster if it does not exist, wait for all nodes to be Ready, then
# create the namespaces and the generated Secrets (issue #10, ADR-0005). Secrets are made
# here, in the cluster only, and never written into the repository:
#   observability/agent-tls, gateway-tls   mTLS between agents and gateways (one local CA)
#   observability/tenants-htpasswd         one basic-auth user per tenant (SHA-512 crypt)
#   bench/tenant-credentials               the same passwords, for the load generators
#   observability/clickhouse-credentials   the ClickHouse user of the collector and migrations
# Existing Secrets are kept.
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

KCTL=(kubectl --context "kind-${CLUSTER}")
"${KCTL[@]}" apply -f "${ROOT}/deploy/kind/manifests/00-namespaces.yaml"

has_secret() { "${KCTL[@]}" -n "$1" get secret "$2" >/dev/null 2>&1; }

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

if ! has_secret observability agent-tls || ! has_secret observability gateway-tls; then
  (
    cd "${WORK}"
    ec=(-newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes)
    openssl req -x509 "${ec[@]}" -days 30 -subj "/CN=traceblame-kind-ca" -keyout ca.key -out ca.crt 2>/dev/null
    openssl req "${ec[@]}" -subj "/CN=otel-gateway" -keyout gw.key -out gw.csr 2>/dev/null
    printf 'subjectAltName=DNS:otel-gateway.observability.svc\nextendedKeyUsage=serverAuth\n' >gw.ext
    openssl x509 -req -in gw.csr -CA ca.crt -CAkey ca.key -CAcreateserial -days 30 -extfile gw.ext -out gw.crt 2>/dev/null
    openssl req "${ec[@]}" -subj "/CN=otel-agent" -keyout agent.key -out agent.csr 2>/dev/null
    printf 'extendedKeyUsage=clientAuth\n' >agent.ext
    openssl x509 -req -in agent.csr -CA ca.crt -CAkey ca.key -CAcreateserial -days 30 -extfile agent.ext -out agent.crt 2>/dev/null
  )
  for s in agent-tls gateway-tls; do "${KCTL[@]}" -n observability delete secret "${s}" --ignore-not-found >/dev/null; done
  "${KCTL[@]}" -n observability create secret generic gateway-tls \
    --from-file=ca.crt="${WORK}/ca.crt" --from-file=tls.crt="${WORK}/gw.crt" --from-file=tls.key="${WORK}/gw.key"
  "${KCTL[@]}" -n observability create secret generic agent-tls \
    --from-file=ca.crt="${WORK}/ca.crt" --from-file=tls.crt="${WORK}/agent.crt" --from-file=tls.key="${WORK}/agent.key"
fi

if ! has_secret observability tenants-htpasswd || ! has_secret bench tenant-credentials; then
  pa="$(openssl rand -hex 16)"
  pb="$(openssl rand -hex 16)"
  printf 'tenant-a:%s\ntenant-b:%s\n' \
    "$(printf %s "${pa}" | openssl passwd -6 -stdin)" "$(printf %s "${pb}" | openssl passwd -6 -stdin)" \
    >"${WORK}/htpasswd"
  "${KCTL[@]}" -n observability delete secret tenants-htpasswd --ignore-not-found >/dev/null
  "${KCTL[@]}" -n bench delete secret tenant-credentials --ignore-not-found >/dev/null
  "${KCTL[@]}" -n observability create secret generic tenants-htpasswd --from-file=htpasswd="${WORK}/htpasswd"
  "${KCTL[@]}" -n bench create secret generic tenant-credentials \
    --from-literal=tenant-a="${pa}" --from-literal=tenant-b="${pb}"
fi

if ! has_secret observability clickhouse-credentials; then
  pw="$(openssl rand -hex 24)"
  "${KCTL[@]}" -n observability create secret generic clickhouse-credentials \
    --from-literal=user=traceblame --from-literal=password="${pw}" \
    --from-literal=password_sha256_hex="$(printf %s "${pw}" | sha256sum | cut -d' ' -f1)"
fi
echo "kind-up: namespaces and Secrets ready"
