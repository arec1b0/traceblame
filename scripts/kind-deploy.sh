#!/usr/bin/env bash
# Deploy the two-tier collector topology into the kind cluster (issue #10, design §1.1):
# ClickHouse through the Altinity operator, the migrations, the otel-agent DaemonSet and the
# otel-gateway Deployment. Run `make kind-up` first (cluster, namespaces, Secrets).
#
# Every image is pinned (tools/versions.env); the operator bundle is checked against
# tools/checksums.txt and its images are rewritten to their pinned digests.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../tools/versions.env
source "${ROOT}/tools/versions.env"
export PATH="${ROOT}/.tools/bin:${PATH}"
export GOTOOLCHAIN=local
CLUSTER=traceblame
KCTL=(kubectl --context "kind-${CLUSTER}")
BUILD="${ROOT}/collector/_build"
WORK="$(mktemp -d)"
PF_PID=""
cleanup() {
  if [[ -n "${PF_PID}" ]]; then kill "${PF_PID}" 2>/dev/null || true; fi
  rm -rf "${WORK}"
}
trap cleanup EXIT

step() { echo "==> $*"; }

step "collector image"
ocb_sha256="$(awk -v f="ocb_${OCB_VERSION}_linux_amd64" '$2 == f { print $1 }' "${ROOT}/tools/checksums.txt")"
docker build --quiet --platform linux/amd64 \
  --build-arg "GO_BUILD_IMAGE=${GO_BUILD_IMAGE}" --build-arg "DISTROLESS_STATIC_IMAGE=${DISTROLESS_STATIC_IMAGE}" \
  --build-arg "OCB_VERSION=${OCB_VERSION}" --build-arg "OCB_SHA256=${ocb_sha256}" \
  --file "${ROOT}/collector/Dockerfile" --tag traceblame-otelcol:local "${ROOT}/collector" >/dev/null
kind load docker-image traceblame-otelcol:local --name "${CLUSTER}"

step "load generator image"
rm -rf "${BUILD}/loadgen" && mkdir -p "${BUILD}/loadgen/fixtures"
(cd "${ROOT}/collector" && CGO_ENABLED=0 go build -trimpath -o "${BUILD}/loadgen/loadgen" ./test/loadgen)
(cd "${ROOT}/collector/testdata/fixtures" && find . -name '*.otlp.json' ! -name '*_content.otlp.json' \
  -exec install -D -m 0644 {} "${BUILD}/loadgen/fixtures/{}" \;)
docker build --quiet --build-arg "DISTROLESS_STATIC_IMAGE=${DISTROLESS_STATIC_IMAGE}" \
  --file "${ROOT}/bench/gateway-scaling/loadgen.Dockerfile" --tag traceblame-loadgen:local "${BUILD}/loadgen" >/dev/null
kind load docker-image traceblame-loadgen:local --name "${CLUSTER}"

step "ClickHouse operator ${CLICKHOUSE_OPERATOR_VERSION}"
bundle="clickhouse-operator-install-bundle-${CLICKHOUSE_OPERATOR_VERSION}.yaml"
curl -fsSL --retry 3 -o "${WORK}/${bundle}" \
  "https://raw.githubusercontent.com/Altinity/clickhouse-operator/release-${CLICKHOUSE_OPERATOR_VERSION}/deploy/operator/clickhouse-operator-install-bundle.yaml"
expected="$(awk -v f="${bundle}" '$2 == f { print $1 }' "${ROOT}/tools/checksums.txt")"
echo "${expected}  ${WORK}/${bundle}" | sha256sum --check --quiet --strict -
sed -e "s|image: altinity/clickhouse-operator:${CLICKHOUSE_OPERATOR_VERSION}$|image: ${CLICKHOUSE_OPERATOR_IMAGE}|" \
  -e "s|image: altinity/metrics-exporter:${CLICKHOUSE_OPERATOR_VERSION}$|image: ${CLICKHOUSE_OPERATOR_METRICS_IMAGE}|" \
  "${WORK}/${bundle}" | "${KCTL[@]}" apply -f - >/dev/null
"${KCTL[@]}" -n kube-system rollout status deployment/clickhouse-operator --timeout=300s

step "ClickHouse"
CLICKHOUSE_IMAGE="${CLICKHOUSE_IMAGE}" envsubst '${CLICKHOUSE_IMAGE}' \
  <"${ROOT}/deploy/kind/manifests/10-clickhouse.yaml" | "${KCTL[@]}" apply -f -
for _ in $(seq 1 120); do
  if "${KCTL[@]}" -n observability get pod -l clickhouse.altinity.com/chi=traceblame -o name 2>/dev/null | grep -q pod/; then break; fi
  sleep 2
done
"${KCTL[@]}" -n observability wait pod -l clickhouse.altinity.com/chi=traceblame --for=condition=Ready --timeout=600s

step "migrations"
user="$("${KCTL[@]}" -n observability get secret clickhouse-credentials -o jsonpath='{.data.user}' | base64 -d)"
password="$("${KCTL[@]}" -n observability get secret clickhouse-credentials -o jsonpath='{.data.password}' | base64 -d)"
# A short-lived port-forward for the runner; restarted if it exits before ClickHouse answers.
ready=""
for _ in $(seq 1 60); do
  if [[ -z "${PF_PID}" ]] || ! kill -0 "${PF_PID}" 2>/dev/null; then
    "${KCTL[@]}" -n observability port-forward svc/clickhouse-traceblame 18123:8123 >/dev/null 2>&1 &
    PF_PID=$!
  fi
  if curl -fsS -m 2 http://127.0.0.1:18123/ping >/dev/null 2>&1; then ready=1; break; fi
  sleep 1
done
[[ -n "${ready}" ]] || { echo "kind-deploy: ClickHouse did not answer through the port-forward" >&2; exit 1; }
(cd "${ROOT}" && uv run --frozen traceblame-storage migrate --url "http://${user}:${password}@127.0.0.1:18123/traceblame")
kill "${PF_PID}" && PF_PID=""

step "collector configuration"
collector="${ROOT}/deploy/kind/collector"
"${KCTL[@]}" -n observability create configmap agent-config --from-file="${collector}/agent.yaml" \
  --dry-run=client -o yaml | "${KCTL[@]}" apply -f -
"${KCTL[@]}" -n observability create configmap gateway-config --from-file="${collector}/gateway.yaml" \
  --from-file="${collector}/gateway-bench.yaml" --dry-run=client -o yaml | "${KCTL[@]}" apply -f -
"${KCTL[@]}" -n observability create configmap latency-thresholds --from-env-file="${collector}/latency-thresholds.env" \
  --dry-run=client -o yaml | "${KCTL[@]}" apply -f -
# One allowlist for both tiers (#36): the restart below rolls agents and gateways together.
"${KCTL[@]}" -n observability create configmap eval-tenants --from-env-file="${collector}/eval-tenants.env" \
  --dry-run=client -o yaml | "${KCTL[@]}" apply -f -

step "agents and gateways"
BUSYBOX_IMAGE="${BUSYBOX_IMAGE}" envsubst '${BUSYBOX_IMAGE}' <"${ROOT}/deploy/kind/manifests/20-agent.yaml" \
  | "${KCTL[@]}" apply -f -
"${KCTL[@]}" apply -f "${ROOT}/deploy/kind/manifests/30-gateway.yaml"
# New images and configuration take effect on restart.
"${KCTL[@]}" -n observability rollout restart daemonset/otel-agent deployment/otel-gateway
"${KCTL[@]}" -n observability rollout status deployment/otel-gateway --timeout=300s
"${KCTL[@]}" -n observability rollout status daemonset/otel-agent --timeout=300s
echo "kind-deploy: ready"
