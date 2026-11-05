#!/usr/bin/env bash
# Span loss and split traces while the gateway scales 2 -> 4 -> 2 under steady load (#10), and
# where the pipeline lag is spent, stage by stage (#57).
#
# Two load generators (one per worker, tenants a and b) send RATE traces/s each for LOAD s.
# PROFILE=stress (default): the gateway scales to 4 at UP_AT s and back to 2 at DOWN_AT s.
# PROFILE=low: RATE defaults to 10 (20 traces/s in total) and the gateway stays at 2.
# PROFILE=steady: like low, at the RATE given.
# Afterwards every span of the run is accounted for (`loadgen analyze`): stored in ClickHouse,
# dropped by a tail_sampling decision (replayed with the gateway's own policies), or lost.
#
# The trace-assembler (#35) runs during the load; the report gets its section: the lag from a
# trace's root span end to its trace.completed message (p50/p95) and how many traces split
# across gateways (#38) and kept on both were re-assembled into one.
#
# STAMPS=1 adds the stamps overlays (deploy/kind/collector/*-bench-stamps.yaml) to the agents
# and the gateways: per-stage lag on each trace's root span, against
# bench/gateway-scaling/stage-budget.env (stage_report.py). Either way the run scrapes the
# collectors' queue metrics every 5 s and reads ClickHouse's metric_log for the run window.
#
# Before the load: no other bench or capture may be running (pods, jobs, processes) and the
# agents must accept no spans for 15 s; the report records both. The agents and the gateways
# are rolled with the run's overlays, and restored to their plain configs on exit; a run that
# fails its checks before that exits without changing the cluster.
# The report records both databases' applied migrations (schema_migrations) at the start and
# whether they changed during the load; a run without the ClickHouse schema fails (#58). The
# load waits for a quiet host (MAX_HOST_LOAD) and the report records the load average.
#
# Run it under the shared kind lock: flock /tmp/traceblame-kind.lock -c 'make bench-scaling'.
# Needs `make kind-up kind-deploy`. The report goes to
# evals/reports/gateway-scaling-<date>[-RUN_NAME].{md,json}.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=../../tools/versions.env
source "${ROOT}/tools/versions.env"
export PATH="${ROOT}/.tools/bin:${PATH}"
export GOTOOLCHAIN=local
CLUSTER=traceblame
# KUBECTL overrides the client (tests/scripts/test_bench_run_restore.py passes a fake).
KCTL=("${KUBECTL:-kubectl}" --context "kind-${CLUSTER}")
PROFILE="${PROFILE:-stress}"
STAMPS="${STAMPS:-0}"
case "${PROFILE}" in
  stress) RATE="${RATE:-100}"; SCALE=1 ;;
  low) RATE="${RATE:-10}"; SCALE=0 ;;
  steady) RATE="${RATE:?PROFILE=steady needs RATE}"; SCALE=0 ;;
  *) echo "run.sh: PROFILE must be stress, low or steady" >&2; exit 2 ;;
esac
[[ "${STAMPS}" == 0 || "${STAMPS}" == 1 ]] || { echo "run.sh: STAMPS must be 0 or 1" >&2; exit 2; }
LOAD="${LOAD:-360}"
UP_AT="${UP_AT:-120}"
DOWN_AT="${DOWN_AT:-240}"
SCRAPE_EVERY=5
# The host is shared (kind, other sessions' builds and tests): the load starts only when the
# 1-minute load average is at most this, waited for up to HOST_LOAD_WAIT s.
MAX_HOST_LOAD="${MAX_HOST_LOAD:-4}"
HOST_LOAD_WAIT="${HOST_LOAD_WAIT:-600}"
DATE="$(date -u +%F)"
NAME="gateway-scaling-${DATE}${RUN_NAME:+-${RUN_NAME}}"
OUT="${ROOT}/evals/reports"
WORK="$(mktemp -d)"
RUN_ID="$(( 0x$(openssl rand -hex 4) & 0x7fffffff ))"
PREFIX="$(printf '%08x' "${RUN_ID}")"
SCRAPER_PID=""
NATS_PF=""
# Set right before this invocation first changes the collectors (patch, scale, rollout). Until
# then a failure, such as a preflight that finds another bench running, leaves the cluster as it
# is: restoring would strip that bench's overlays and roll its collectors (#61).
CHANGED_COLLECTORS=0

step() { echo "==> $*" >&2; }
now() { date +%s%N; }
gw() { "${KCTL[@]}" -n observability "$@"; }

AGENT_ARGS='["--config=/etc/otelcol/config/agent.yaml"]'
GATEWAY_ARGS='["--config=/etc/otelcol/config/gateway.yaml","--config=/etc/otelcol/config/gateway-bench.yaml"]'
if [[ "${STAMPS}" == 1 ]]; then
  AGENT_ARGS='["--config=/etc/otelcol/config/agent.yaml","--config=/etc/otelcol/config/agent-bench-stamps.yaml"]'
  GATEWAY_ARGS='["--config=/etc/otelcol/config/gateway.yaml","--config=/etc/otelcol/config/gateway-bench.yaml","--config=/etc/otelcol/config/gateway-bench-stamps.yaml"]'
fi
set_args() { # kind name args-json
  gw patch "$1" "$2" --type=json \
    -p "[{\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/args\",\"value\":$3}]" >/dev/null
}

restore() {
  local rc=$?
  [[ -n "${SCRAPER_PID}" ]] && kill "${SCRAPER_PID}" 2>/dev/null || true
  [[ -n "${NATS_PF}" ]] && kill "${NATS_PF}" 2>/dev/null || true
  if [[ "${CHANGED_COLLECTORS}" == 1 ]]; then
    step "restoring the agents and the gateway (no bench overlay)"
    set_args daemonset otel-agent '["--config=/etc/otelcol/config/agent.yaml"]' || true
    set_args deployment otel-gateway '["--config=/etc/otelcol/config/gateway.yaml"]' || true
  fi
  rm -rf "${WORK}"
  exit "${rc}"
}
trap restore EXIT

agent_pods() { gw get pods -l app=otel-agent --field-selector=status.phase=Running -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}'; }
gateway_pods() { gw get pods -l app=otel-gateway --field-selector=status.phase=Running -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}'; }
pod_metrics() { "${KCTL[@]}" get --raw "/api/v1/namespaces/observability/pods/$1:8888/proxy/metrics"; }
accepted() { # sum of otelcol_receiver_accepted_spans over the agents
  local total=0 v
  for p in $(agent_pods); do
    v="$(pod_metrics "${p}" | awk '/^otelcol_receiver_accepted_spans(_total)?[{ ]/ {s += $NF} END {printf "%d", s}')"
    total=$((total + v))
  done
  echo "${total}"
}

chi_pod="$(gw get pod -l clickhouse.altinity.com/chi=traceblame -o jsonpath='{.items[0].metadata.name}')"
ch_user="$(gw get secret clickhouse-credentials -o jsonpath='{.data.user}' | base64 -d)"
ch_password="$(gw get secret clickhouse-credentials -o jsonpath='{.data.password}' | base64 -d)"
ch() { gw exec "${chi_pod}" -c clickhouse -- clickhouse-client --user "${ch_user}" --password "${ch_password}" --database traceblame --query "$1"; }
pg_user="$("${KCTL[@]}" -n traceblame get secret postgres-credentials -o jsonpath='{.data.user}' | base64 -d)"
pg_password="$("${KCTL[@]}" -n traceblame get secret postgres-credentials -o jsonpath='{.data.password}' | base64 -d)"
pg() { "${KCTL[@]}" -n traceblame exec postgres-0 -- env PGPASSWORD="${pg_password}" psql -h 127.0.0.1 -U "${pg_user}" -d traceblame -Atqc "$1"; }

# The applied migrations of both databases (ADR-0004, ADR-0007): another session may apply an
# expand migration this checkout does not have (code N runs on schema N+1 expand), so the
# report names the schema each run saw, at its start and at the end of its load.
ch_schema() {
  ch "SELECT arrayStringConcat(arraySort(groupArray(concat(leftPad(toString(version), 4, '0'), ' ', name,
        ' (', phase, ', ', substring(sha256, 1, 12), ', ', formatDateTime(applied_at, '%FT%TZ', 'UTC'), ')'))), '; ')
      FROM schema_migrations"
}
pg_schema() {
  pg "SELECT string_agg(format('%s %s (%s, %s, %s)', lpad(version::text, 4, '0'), name, phase, left(sha256, 12),
        to_char(applied_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')), '; ' ORDER BY version)
      FROM schema_migrations"
}
beyond() { # applied versions (from a schema list) without a migration file in this checkout
  local dir="${ROOT}/packages/storage/src/traceblame_storage/migrations/$1" v out=""
  for v in $(tr ';' '\n' <<<"$2" | awk 'NF {print $1}'); do
    compgen -G "${dir}/${v}_*.sql" >/dev/null || out+="${v} "
  done
  echo "${out:-none}"
}

# Provenance of the whole tree at the start (loadgen analyze would check collector/ only): the
# reports this run writes and a worktree's local .tools link do not count.
commit="$(git -C "${ROOT}" rev-parse HEAD)"
dirty_files="$(git -C "${ROOT}" status --porcelain -- . ':(top,exclude)evals/reports' ':(top,exclude).tools' | tr '\n' ' ')"
dirty="false"
[[ -n "${dirty_files// /}" ]] && dirty="true: ${dirty_files}"

step "schema: the applied migrations of ClickHouse and Postgres"
ch_schema_start="$(ch_schema)" || { echo "run.sh: no schema_migrations in ClickHouse (data lost? #58): run make kind-deploy" >&2; exit 1; }
pg_schema_start="$(pg_schema)" || { echo "run.sh: no schema_migrations in Postgres: run make kind-deploy" >&2; exit 1; }
[[ -n "${ch_schema_start}" ]] || { echo "run.sh: ClickHouse has no applied migration (#58): run make kind-deploy" >&2; exit 1; }

step "preflight: no other bench or capture in the cluster (run ${PREFIX})"
preflight_at="$(date -u +%FT%TZ)"
"${KCTL[@]}" get pods -A --no-headers >"${WORK}/pods.txt"
busy=""
active_jobs="$("${KCTL[@]}" get jobs -A -o jsonpath='{range .items[?(@.status.active>0)]}{.metadata.namespace}/{.metadata.name}{"\n"}{end}')"
[[ -n "${active_jobs}" ]] && busy+="active jobs: ${active_jobs//$'\n'/ }; "
bench_pods="$(awk '$1 == "bench" && $4 != "Completed" {print $2}' "${WORK}/pods.txt")"
[[ -n "${bench_pods}" ]] && busy+="bench pods: ${bench_pods//$'\n'/ }; "
# Processes outside this run's process group (its own subshells share it).
pgid="$(ps -o pgid= -p $$ | tr -d ' ')"
BUSY_PROCESSES="${BUSY_PROCESSES:-tools/fixtures/|fixtures-agentic-rag|fixtures-runbook-agent|bench/gateway-scaling/run.sh}"
others="$(ps -eo pid=,pgid=,args= | awk -v g="${pgid}" '$2 != g' | grep -E "${BUSY_PROCESSES}" || true)"
[[ -n "${others}" ]] && busy+="processes: ${others//$'\n'/; }; "
if [[ -n "${busy}" ]]; then
  echo "run.sh: another bench or capture is running, the run would be invalid: ${busy}" >&2
  exit 1
fi
load1() { cut -d' ' -f1 /proc/loadavg; }
step "host load: waiting for a 1-minute load average <= ${MAX_HOST_LOAD} (up to ${HOST_LOAD_WAIT} s)"
for ((waited = 0; ; waited += 10)); do
  awk -v l="$(load1)" -v m="${MAX_HOST_LOAD}" 'BEGIN {exit !(l <= m)}' && break
  if (( waited >= HOST_LOAD_WAIT )); then
    echo "run.sh: host load $(load1) stayed above ${MAX_HOST_LOAD} for ${HOST_LOAD_WAIT} s; top processes:" >&2
    ps -eo pcpu,args --sort=-pcpu | head -6 >&2
    exit 1
  fi
  sleep 10
done
pods_summary="$(awk '{c[$1]++} END {for (n in c) printf "%s %d, ", n, c[n]}' "${WORK}/pods.txt" | sed 's/, $//')"

step "agents and gateways with the run's overlays (stamps=${STAMPS}), 2 gateway replicas"
CHANGED_COLLECTORS=1
set_args daemonset otel-agent "${AGENT_ARGS}"
set_args deployment otel-gateway "${GATEWAY_ARGS}"
gw scale deployment/otel-gateway --replicas=2 >/dev/null
gw rollout restart daemonset/otel-agent deployment/otel-gateway >/dev/null
gw rollout status daemonset/otel-agent --timeout=300s >/dev/null
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

step "idle check: the agents accept no spans for 15 s"
a0="$(accepted)"
sleep 15
a1="$(accepted)"
if (( a1 != a0 )); then
  echo "run.sh: the agents accepted $((a1 - a0)) spans in 15 s without the bench: other traffic, the run would be invalid" >&2
  exit 1
fi
idle="0 spans accepted by the agents in 15 s before the load"
host_load_start="$(cut -d' ' -f1-3 /proc/loadavg) ($(nproc) CPUs)"

step "scraping the collectors' metrics every ${SCRAPE_EVERY} s"
metrics="${WORK}/metrics.txt"
(
  tick=0
  while true; do
    ts="$(now)"
    for p in $(agent_pods); do
      { echo "# SCRAPE ${tick} ${ts} agent ${p}"; pod_metrics "${p}" | grep '^otelcol_' || true; } >>"${metrics}"
    done
    for p in $(gateway_pods); do
      { echo "# SCRAPE ${tick} ${ts} gateway ${p}"; pod_metrics "${p}" | grep '^otelcol_' || true; } >>"${metrics}"
    done
    tick=$((tick + 1))
    sleep "${SCRAPE_EVERY}"
  done
) 2>/dev/null &
SCRAPER_PID=$!

step "load: 2 generators x ${RATE} traces/s for ${LOAD} s (profile ${PROFILE})"
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
: >"${events}"
if [[ "${SCALE}" == 1 ]]; then
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
  scaling="2 -> 4 at T+${UP_AT} s, 4 -> 2 at T+${DOWN_AT} s"
else
  scaling="none: gateway fixed at 2 replicas"
fi

step "waiting for the generators to finish"
"${KCTL[@]}" -n bench wait job -l app=loadgen --for=condition=Complete --timeout="$((LOAD + 900))s" >/dev/null
host_load_end="$(cut -d' ' -f1-3 /proc/loadavg) ($(nproc) CPUs)"
ch_schema_end="$(ch_schema)"
pg_schema_end="$(pg_schema)"
schema_changed="no"
[[ "${ch_schema_end}" == "${ch_schema_start}" && "${pg_schema_end}" == "${pg_schema_start}" ]] \
  || schema_changed="YES: ClickHouse at the end: ${ch_schema_end}; Postgres at the end: ${pg_schema_end}"

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

step "waiting for the trace-assembler (grace, poll interval, then stable summary counts)"
sleep 20
last=-1
stable=0
while (( stable < 30 )); do
  n="$(ch "SELECT uniqExact(trace_id) FROM trace_summary WHERE startsWith(trace_id, '${PREFIX}')")"
  if [[ "${n}" == "${last}" ]]; then stable=$((stable + 5)); else stable=0; last="${n}"; fi
  sleep 5
done
kill "${SCRAPER_PID}" 2>/dev/null || true
SCRAPER_PID=""
ch "SELECT trace_id, toUnixTimestamp64Milli(start_ts) + toInt64(duration_ms) FROM spans
    WHERE startsWith(trace_id, '${PREFIX}') AND parent_span_id = '' FORMAT TSV" >"${WORK}/roots.tsv"
ch "SELECT trace_id, span_count FROM trace_summary FINAL
    WHERE startsWith(trace_id, '${PREFIX}') FORMAT TSV" >"${WORK}/summaries.tsv"
ch "SELECT trace_id, toUnixTimestamp64Milli(min(ingested_at)), toUnixTimestamp64Milli(max(ingested_at))
    FROM trace_arrivals WHERE startsWith(trace_id, '${PREFIX}') GROUP BY trace_id FORMAT TSV" >"${WORK}/arrivals.tsv"
ch "SELECT trace_id, toUnixTimestamp64Milli(max(assembled_at)) FROM trace_summary
    WHERE startsWith(trace_id, '${PREFIX}') GROUP BY trace_id FORMAT TSV" >"${WORK}/assembled.tsv"
# Per root span: its end (ns), the four stamps (ns, empty without the overlay) and the
# arrival of the insert that held it (ms).
ch "SELECT s.trace_id,
           toUnixTimestamp64Nano(s.start_ts) + toInt64(round(s.duration_ms * 1000000)),
           s.attrs['traceblame.bench.t_agent_ns'], s.attrs['traceblame.bench.t_gateway_ns'],
           s.attrs['traceblame.bench.t_decided_ns'], s.attrs['traceblame.bench.t_export_ns'],
           a.root_ms
    FROM spans AS s
    LEFT JOIN (SELECT trace_id, nullIf(toUnixTimestamp64Milli(minIf(ingested_at, is_root = 1)), 0) AS root_ms
               FROM trace_arrivals WHERE startsWith(trace_id, '${PREFIX}') GROUP BY trace_id) AS a
      ON a.trace_id = s.trace_id
    WHERE startsWith(s.trace_id, '${PREFIX}') AND s.parent_span_id = ''
    SETTINGS join_use_nulls = 1 FORMAT TSV" >"${WORK}/stamps.tsv"
# ClickHouse's own view of the inserts, one row per second (metric_log flushes every 7.5 s).
sleep 10
ch "SELECT toUnixTimestamp64Milli(event_time_microseconds), CurrentMetric_PendingAsyncInsert,
           ProfileEvent_AsyncInsertQuery, ProfileEvent_FailedAsyncInsertQuery, ProfileEvent_InsertQuery
    FROM system.metric_log
    WHERE event_time >= toDateTime(intDiv(${t0}, 1000000000)) - 10
    ORDER BY event_time FORMAT TSV" >"${WORK}/clickhouse.tsv"
"${KCTL[@]}" -n observability port-forward svc/nats 14222:4222 >/dev/null 2>&1 &
NATS_PF=$!
sleep 3
(cd "${ROOT}" && uv run --frozen python bench/gateway-scaling/assembler_report.py \
  --nats nats://127.0.0.1:14222 --prefix "${PREFIX}" --roots "${WORK}/roots.tsv" \
  --summaries "${WORK}/summaries.tsv" --stored "${WORK}/stored.tsv" \
  --arrivals "${WORK}/arrivals.tsv" --assembled "${WORK}/assembled.tsv" \
  --markdown "${WORK}/assembler.md" --json "${WORK}/assembler.json")
kill "${NATS_PF}"
NATS_PF=""
(cd "${ROOT}" && uv run --frozen python bench/gateway-scaling/stage_report.py run \
  --name "${RUN_NAME:-${PROFILE}}" --run-id "${PREFIX}" --profile "${PROFILE}" --stamps "${STAMPS}" \
  --total-rate "$((2 * RATE))" --budget bench/gateway-scaling/stage-budget.env \
  --roots "${WORK}/stamps.tsv" --stored "${WORK}/stored.tsv" --summaries "${WORK}/summaries.jsonl" \
  --assembler-json "${WORK}/assembler.json" --metrics "${metrics}" --clickhouse "${WORK}/clickhouse.tsv" \
  --events "${events}" --markdown "${WORK}/stages.md" --json "${WORK}/stages.json")
k8s_version="$("${KCTL[@]}" version -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)["serverVersion"]["gitVersion"])')"
docker_info="$(docker info --format '{{.NCPU}} CPUs, {{.MemTotal}} bytes')"
image_id() { docker image inspect "$1" --format '{{.Id}}' 2>/dev/null || echo unknown; }
assembler_image="$("${KCTL[@]}" -n traceblame get deployment trace-assembler -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || echo unknown)"
python3 - "${WORK}/env.json" <<PY
import json, sys
json.dump({
    "date": "${DATE}",
    "commit": "${commit}",
    "dirty": "${dirty}",
    "cluster": "kind ${KIND_VERSION}, node image ${KIND_NODE_IMAGE}, Kubernetes ${k8s_version}, 1 control plane + 2 workers",
    "host": "WSL2 (Windows 11), Docker Desktop: ${docker_info}",
    "collector": "traceblame-otelcol:local (otelcol-contrib 0.164 module set), image $(image_id traceblame-otelcol:local)",
    "loadgen": "traceblame-loadgen:local, image $(image_id traceblame-loadgen:local)",
    "trace-assembler": "${assembler_image}, image $(image_id "${assembler_image}")",
    "image ids": "local images have no registry digest; the image ID (config digest) identifies them",
    "clickhouse": "${CLICKHOUSE_IMAGE} via Altinity operator ${CLICKHOUSE_OPERATOR_VERSION}, 1 shard x 1 replica",
    "routing": "loadbalancing exporter, routing_key traceID, k8s resolver (EndpointSlices)",
    "gateway drain": "preStop sleep 15 s, terminationGracePeriodSeconds 75 s, decision_wait 30 s, export timeout 10 s",
    "profile": "${PROFILE}",
    "load": "2 generators x ${RATE} traces/s x ${LOAD} s, 4 spans per trace, every 2nd trace an error trace; spans exported as they end",
    "scaling": "${scaling}",
    "stamps overlay": "$( [[ "${STAMPS}" == 1 ]] && echo "on (agent-bench-stamps.yaml, gateway-bench-stamps.yaml)" || echo off)",
    "metrics scrape": "every ${SCRAPE_EVERY} s through the API server's pod proxy (:8888); ClickHouse system.metric_log",
    "preflight (${preflight_at})": "no active job, no bench pod, no capture process; pods: ${pods_summary}",
    "idle check": "${idle}",
    "host load average (1/5/15 min) at the load's start": "${host_load_start}",
    "host load average (1/5/15 min) at the load's end": "${host_load_end}",
    "ClickHouse schema_migrations at the start": "${ch_schema_start}",
    "ClickHouse migrations applied beyond this checkout": "$(beyond clickhouse "${ch_schema_start}")",
    "Postgres schema_migrations at the start": "${pg_schema_start}",
    "Postgres migrations applied beyond this checkout": "$(beyond postgres "${pg_schema_start}")",
    "schema changed during the load": "${schema_changed}",
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
  --out "${OUT}" --name "${NAME}") >/dev/null
cp "${events}" "${OUT}/${NAME}.events.jsonl"
{ echo; cat "${WORK}/assembler.md"; echo; cat "${WORK}/stages.md"; } >>"${OUT}/${NAME}.md"
cp "${WORK}/assembler.json" "${OUT}/${NAME}.assembler.json"
cp "${WORK}/stages.json" "${OUT}/${NAME}.stages.json"
echo "report: evals/reports/${NAME}.md"
