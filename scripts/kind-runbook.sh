#!/usr/bin/env bash
# The runbook-agent sandbox in the kind cluster (#13, deploy/kind/runbook/):
#   up    namespaces, the agent's namespace-only RBAC, network policies, and the monitoring
#         (kube-state-metrics, Prometheus, Alertmanager, blackbox_exporter) that fires alerts
#   down  delete both namespaces
# Run `make kind-up kind-deploy` first. The agent and the tee collector are applied by the
# capture (tools/fixtures/capture_runbook_agent.py), which builds the agent's image.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../tools/versions.env
source "${ROOT}/tools/versions.env"
export PATH="${ROOT}/.tools/bin:${PATH}"
KCTL=(kubectl --context kind-traceblame)
DIR="${ROOT}/deploy/kind/runbook"

step() { echo "==> $*"; }

down() {
  "${KCTL[@]}" delete namespace runbook-sandbox runbook-monitoring --ignore-not-found --wait=true
}

# The address a pod reaches the host's Ollama at (Docker Desktop's host.docker.internal).
ollama_ip() {
  local pod="ollama-ip-$$"
  "${KCTL[@]}" -n runbook-monitoring run "${pod}" --restart=Never --image="${BUSYBOX_IMAGE}" \
    --command -- sh -c 'nslookup host.docker.internal | awk "/^Address: /{print \$2; exit}"' >/dev/null
  "${KCTL[@]}" -n runbook-monitoring wait "pod/${pod}" --for=jsonpath='{.status.phase}'=Succeeded --timeout=60s >/dev/null
  "${KCTL[@]}" -n runbook-monitoring logs "${pod}"
  "${KCTL[@]}" -n runbook-monitoring delete pod "${pod}" --wait=false >/dev/null
}

up() {
  step "namespaces and RBAC"
  "${KCTL[@]}" apply -f "${DIR}/manifests/00-namespaces.yaml" -f "${DIR}/manifests/10-rbac.yaml"

  step "network policies"
  # The API server's endpoint as pods reach it (the EndpointSlice of the kubernetes Service;
  # the Endpoints API is deprecated), and host.docker.internal as resolved inside the cluster.
  local endpoints
  endpoints="$("${KCTL[@]}" get endpointslice -n default -l kubernetes.io/service-name=kubernetes \
    -o jsonpath='{range .items[*]}{range .endpoints[*]}{range .addresses[*]}{@}{" "}{end}{end}{end}')"
  APISERVER_PORT="$("${KCTL[@]}" get endpointslice -n default -l kubernetes.io/service-name=kubernetes \
    -o jsonpath='{.items[0].ports[?(@.name=="https")].port}')"
  read -r -a addresses <<<"${endpoints}"
  [[ ${#addresses[@]} -eq 1 ]] || {
    echo "kind-runbook: want one API server address, got '${endpoints}'" >&2
    exit 1
  }
  APISERVER_IP="${addresses[0]}"
  OLLAMA_IP="$(ollama_ip)"
  [[ "${APISERVER_IP}" =~ ^[0-9.]+$ && "${APISERVER_PORT}" =~ ^[0-9]+$ && "${OLLAMA_IP}" =~ ^[0-9.]+$ ]] || {
    echo "kind-runbook: cannot resolve the API server (${APISERVER_IP}:${APISERVER_PORT}) or Ollama (${OLLAMA_IP})" >&2
    exit 1
  }
  APISERVER_IP="${APISERVER_IP}" APISERVER_PORT="${APISERVER_PORT}" OLLAMA_IP="${OLLAMA_IP}" \
    envsubst '${APISERVER_IP} ${APISERVER_PORT} ${OLLAMA_IP}' \
    <"${DIR}/manifests/30-network.yaml" | "${KCTL[@]}" apply -f -

  step "monitoring"
  local mon="${DIR}/monitoring" ns=(-n runbook-monitoring)
  "${KCTL[@]}" "${ns[@]}" create configmap prometheus-config --from-file="${mon}/prometheus.yml" \
    --dry-run=client -o yaml | "${KCTL[@]}" apply -f -
  "${KCTL[@]}" "${ns[@]}" create configmap prometheus-rules --from-file="${mon}/rules.yml" \
    --dry-run=client -o yaml | "${KCTL[@]}" apply -f -
  "${KCTL[@]}" "${ns[@]}" create configmap alertmanager-config --from-file="${mon}/alertmanager.yml" \
    --dry-run=client -o yaml | "${KCTL[@]}" apply -f -
  "${KCTL[@]}" "${ns[@]}" create configmap blackbox-config --from-file="${mon}/blackbox.yml" \
    --dry-run=client -o yaml | "${KCTL[@]}" apply -f -
  KUBE_STATE_METRICS_IMAGE="${KUBE_STATE_METRICS_IMAGE}" PROMETHEUS_IMAGE="${PROMETHEUS_IMAGE}" \
    ALERTMANAGER_IMAGE="${ALERTMANAGER_IMAGE}" BLACKBOX_EXPORTER_IMAGE="${BLACKBOX_EXPORTER_IMAGE}" \
    envsubst '${KUBE_STATE_METRICS_IMAGE} ${PROMETHEUS_IMAGE} ${ALERTMANAGER_IMAGE} ${BLACKBOX_EXPORTER_IMAGE}' \
    <"${DIR}/manifests/20-monitoring.yaml" | "${KCTL[@]}" apply -f -
  # New configuration takes effect on restart.
  "${KCTL[@]}" "${ns[@]}" rollout restart deployment/prometheus deployment/alertmanager deployment/blackbox-exporter
  for d in kube-state-metrics prometheus alertmanager blackbox-exporter; do
    "${KCTL[@]}" "${ns[@]}" rollout status "deployment/${d}" --timeout=300s
  done
  echo "kind-runbook: ready (API server ${APISERVER_IP}:${APISERVER_PORT}, Ollama ${OLLAMA_IP})"
}

case "${1:-}" in
  up) up ;;
  down) down ;;
  *) echo "usage: $0 up|down" >&2; exit 2 ;;
esac
