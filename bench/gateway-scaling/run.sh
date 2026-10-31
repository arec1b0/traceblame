#!/usr/bin/env bash
# Span loss and split traces while the gateway scales 2 -> 4 -> 2 under steady load (#10).
#
# Two load generators (one per worker, tenants a and b) send RATE traces/s each for LOAD s;
# the gateway scales to 4 at UP_AT s and back to 2 at DOWN_AT s. Afterwards every span of
# the run is accounted for (`loadgen analyze`): stored in ClickHouse, dropped by a
# tail_sampling decision (replayed with the gateway's own policies), or lost. The report
# goes to evals/reports/gateway-scaling-<date>.{md,json}.
#
# Needs `make kind-up kind-deploy`. Usage: bench/gateway-scaling/run.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=../../tools/versions.env
source "${ROOT}/tools/versions.env"
export PATH="${ROOT}/.tools/bin:${PATH}"
export GOTOOLCHAIN=local
CLUSTER=traceblame
KCTL=(kubectl --context "kind-${CLUSTER}")
RATE="${RATE:-100}"
LOAD="${LOAD:-360}"
UP_AT="${UP_AT:-120}"
DOWN_AT="${DOWN_AT:-240}"
DATE="$(date -u +%F)"
OUT="${ROOT}/evals/reports"
WORK="$(mktemp -d)"
RUN_ID="$(( 0x$(openssl rand -hex 4) & 0x7fffffff ))"
PREFIX="$(printf '%08x' "${RUN_ID}")"
trap 'rm -rf "${WORK}"' EXIT

step() { echo "==> $*" >&2; }
now() { date +%s%N; }
gw() { "${KCTL[@]}" -n observability "$@"; }

step "gateway with the bench overlay, 2 replicas (run ${PREFIX})"
gw patch deployment otel-gateway --type=json -p '[
  {"op":"replace","path":"/spec/template/spec/containers/0/args",
   "value":["--config=/etc/otelcol/config/gateway.yaml","--config=/etc/otelcol/config/gateway-bench.yaml"]},
  {"op":"replace","path":"/spec/replicas","value":2}]' >/dev/null
gw rollout status deployment/otel-gateway --timeout=300s >/dev/null
for _ in $(seq 1 120); do
  [[ "$(gw get pods -l app=otel-gateway --no-headers 2>/dev/null | wc -l)" == 2 ]] && break
  sleep 1
done

step "NetworkPolicy: a pod that is not an agent tries the gateway's OTLP port"
"${KCTL[@]}" -n bench delete pod netpol-probe --ignore-not-found >/dev/null
"${KCTL[@]}" -n bench run netpol-probe --image=traceblame-loadgen:local --image-pull-policy=Never \
  --restart=Never -- probe --addr=otel-gateway.observability.svc:4317 --timeout=5s >/dev/null
"${KCTL[@]}" -n bench wait pod/netpol-probe --for=jsonpath='{.status.phase}'=Succeeded --timeout=60s >/dev/null
netpol="$("${KCTL[@]}" -n bench logs netpol-probe | tr -d '\n')"
"${KCTL[@]}" -n bench delete pod netpol-probe >/dev/null

step "load: 2 generators x ${RATE} traces/s for ${LOAD} s"
"${KCTL[@]}" -n bench delete job -l app=loadgen --ignore-not-found >/dev/null
"${KCTL[@]}" -n bench delete job loadgen-0 loadgen-1 --ignore-not-found >/dev/null
mapfile -t nodes < <("${KCTL[@]}" get nodes -l '!node-role.kubernetes.io/control-plane' -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')
tenants=(tenant-a tenant-b)
for i in 0 1; do
  POD="${i}" TENANT="${tenants[$i]}" NODE="${nodes[$i]}" RUN_ID="${RUN_ID}" RATE="${RATE}" LOAD="${LOAD}" \
    envsubst '${POD} ${TENANT} ${NODE} ${RUN_ID} ${RATE} ${LOAD}' <"${ROOT}/bench/gateway-scaling/loadgen-job.yaml" \
    | "${KCTL[@]}" apply -f - >/dev/null
done
"${KCTL[@]}" -n bench wait pod -l app=loadgen --for=condition=Ready --timeout=120s >/dev/null
t0="$(now)"

wait_until() { local at=$1; while (( $(now) < t0 + at * 1000000000 )); do sleep 1; done; }
events="${WORK}/events.jsonl"
wait_until "${UP_AT}"
step "scale 2 -> 4"
cmd="$(now)"
gw scale deployment/otel-gateway --replicas=4 >/dev/null
until [[ "$(gw get deployment otel-gateway -o jsonpath='{.status.readyReplicas}')" == 4 ]]; do sleep 1; done
printf '{"name":"scale 2->4","command_ns":%s,"settled_ns":%s}\n' "${cmd}" "$(now)" >>"${events}"
wait_until "${DOWN_AT}"
step "scale 4 -> 2"
cmd="$(now)"
gw scale deployment/otel-gateway --replicas=2 >/dev/null
until [[ "$(gw get pods -l app=otel-gateway --no-headers | wc -l)" == 2 ]]; do sleep 1; done
printf '{"name":"scale 4->2","command_ns":%s,"settled_ns":%s}\n' "${cmd}" "$(now)" >>"${events}"

step "waiting for the generators to finish"
"${KCTL[@]}" -n bench wait job -l app=loadgen --for=condition=Complete --timeout="$((LOAD + 900))s" >/dev/null

chi_pod="$(gw get pod -l clickhouse.altinity.com/chi=traceblame -o jsonpath='{.items[0].metadata.name}')"
ch_user="$(gw get secret clickhouse-credentials -o jsonpath='{.data.user}' | base64 -d)"
ch_password="$(gw get secret clickhouse-credentials -o jsonpath='{.data.password}' | base64 -d)"
ch() { gw exec "${chi_pod}" -c clickhouse -- clickhouse-client --user "${ch_user}" --password "${ch_password}" --database traceblame --query "$1"; }

step "waiting for the gateways to decide and flush (decision_wait 30 s, then stable counts)"
sleep 30
last=-1
stable=0
while (( stable < 45 )); do
  n="$(ch "SELECT count() FROM spans WHERE startsWith(trace_id, '${PREFIX}')")"
  if [[ "${n}" == "${last}" ]]; then stable=$((stable + 5)); else stable=0; last="${n}"; fi
  sleep 5
done

step "collecting"
for i in 0 1; do "${KCTL[@]}" -n bench logs "job/loadgen-${i}" | tail -n 1; done >"${WORK}/summaries.jsonl"
ch "SELECT trace_id, span_id, resource_attrs['traceblame.bench.gateway'], tenant_id, toUnixTimestamp64Nano(start_ts)
    FROM spans WHERE startsWith(trace_id, '${PREFIX}') FORMAT TSV" >"${WORK}/stored.tsv"
k8s_version="$("${KCTL[@]}" version -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)["serverVersion"]["gitVersion"])')"
docker_info="$(docker info --format '{{.NCPU}} CPUs, {{.MemTotal}} bytes')"
python3 - "${WORK}/env.json" <<PY
import json, sys
json.dump({
    "date": "${DATE}",
    "cluster": "kind ${KIND_VERSION}, node image ${KIND_NODE_IMAGE}, Kubernetes ${k8s_version}, 1 control plane + 2 workers",
    "host": "WSL2 (Windows 11), Docker Desktop: ${docker_info}",
    "collector": "traceblame-otelcol:local (otelcol-contrib 0.164 module set), image $(docker image inspect traceblame-otelcol:local --format '{{.Id}}')",
    "clickhouse": "${CLICKHOUSE_IMAGE} via Altinity operator ${CLICKHOUSE_OPERATOR_VERSION}, 1 shard x 1 replica",
    "routing": "loadbalancing exporter, routing_key traceID, k8s resolver (EndpointSlices)",
    "gateway drain": "preStop sleep 15 s, terminationGracePeriodSeconds 75 s, decision_wait 30 s, export timeout 10 s",
    "load": "2 generators x ${RATE} traces/s x ${LOAD} s, 4 spans per trace, every 2nd trace an error trace; spans exported as they end",
    "scaling": "2 -> 4 at T+${UP_AT} s, 4 -> 2 at T+${DOWN_AT} s",
    "network policy probe (non-agent pod -> gateway:4317)": "${netpol}",
    "run id": "${PREFIX}",
}, open(sys.argv[1], "w"), indent=1)
PY

step "analysis"
mkdir -p "${OUT}"
(cd "${ROOT}/collector" && go run ./test/loadgen analyze \
  --fixtures testdata/fixtures \
  --gateway-config "${ROOT}/deploy/kind/collector/gateway.yaml" \
  --thresholds "${ROOT}/deploy/kind/collector/latency-thresholds.env" \
  --summaries "${WORK}/summaries.jsonl" --stored "${WORK}/stored.tsv" \
  --events "${events}" --env "${WORK}/env.json" \
  --out "${OUT}" --name "gateway-scaling-${DATE}") >/dev/null
cp "${events}" "${OUT}/gateway-scaling-${DATE}.events.jsonl"

step "restoring the gateway (no bench overlay)"
gw patch deployment otel-gateway --type=json -p '[
  {"op":"replace","path":"/spec/template/spec/containers/0/args","value":["--config=/etc/otelcol/config/gateway.yaml"]}]' >/dev/null
echo "report: evals/reports/gateway-scaling-${DATE}.md"
