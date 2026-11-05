#!/usr/bin/env bash
# Check that ClickHouse in the kind cluster keeps its data (#58): the CHI pod mounts a PVC at
# /var/lib/clickhouse, and a marker row survives a pod deletion or a node restart.
#   pod            write a marker, delete the CHI pod, wait for Ready, check the marker, clean up
#   write          write a marker and print its token (before a manual Docker Desktop restart)
#   verify TOKEN   check that marker after the restart, then clean up
# The marker lives in its own scratch database (persistence_check), never in the migrated
# `traceblame` schema (ADR-0004). Queries run through `kubectl exec` inside the pod, so no
# credentials are needed. Run `make kind-up kind-deploy` first.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="${ROOT}/.tools/bin:${PATH}"
KCTL=(kubectl --context kind-traceblame -n observability)
SELECTOR=clickhouse.altinity.com/chi=traceblame
DB=persistence_check

step() { echo "==> $*"; }
fail() {
  echo "kind-clickhouse-persistence: $*" >&2
  exit 1
}

pod_name() { "${KCTL[@]}" get pod -l "${SELECTOR}" -o jsonpath='{.items[0].metadata.name}'; }
pod_uid() { "${KCTL[@]}" get pod "$1" -o jsonpath='{.metadata.uid}' 2>/dev/null || true; }
query() { "${KCTL[@]}" exec "$(pod_name)" -c clickhouse -- clickhouse-client -q "$1"; }

wait_ready() {
  for _ in $(seq 1 150); do
    if "${KCTL[@]}" get pod -l "${SELECTOR}" -o name 2>/dev/null | grep -q pod/; then break; fi
    sleep 2
  done
  "${KCTL[@]}" wait pod -l "${SELECTOR}" --for=condition=Ready --timeout=600s >/dev/null
  for _ in $(seq 1 60); do
    if query "SELECT 1" >/dev/null 2>&1; then return 0; fi
    sleep 2
  done
  fail "ClickHouse does not answer in $(pod_name)"
}

# The data directory must be a mounted PVC that is Bound, not the container's writable layer.
check_volume() {
  local pod volume claim phase
  pod="$(pod_name)"
  volume="$("${KCTL[@]}" get pod "${pod}" -o jsonpath='{.spec.containers[?(@.name=="clickhouse")].volumeMounts[?(@.mountPath=="/var/lib/clickhouse")].name}')"
  [[ -n "${volume}" ]] || fail "${pod} mounts nothing at /var/lib/clickhouse"
  claim="$("${KCTL[@]}" get pod "${pod}" -o jsonpath="{.spec.volumes[?(@.name==\"${volume}\")].persistentVolumeClaim.claimName}")"
  [[ -n "${claim}" ]] || fail "volume ${volume} of ${pod} is not a PVC"
  phase="$("${KCTL[@]}" get pvc "${claim}" -o jsonpath='{.status.phase}')"
  [[ "${phase}" == Bound ]] || fail "PVC ${claim} is ${phase:-missing}, not Bound"
  echo "/var/lib/clickhouse in ${pod} is PVC ${claim} (Bound)"
}

write() {
  local token
  token="$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')"
  query "CREATE DATABASE IF NOT EXISTS ${DB}"
  query "CREATE TABLE IF NOT EXISTS ${DB}.marker (token String, written DateTime DEFAULT now()) ENGINE = MergeTree ORDER BY token"
  query "INSERT INTO ${DB}.marker (token) VALUES ('${token}')"
  echo "${token}"
}

verify() {
  local token="$1" found
  [[ "${token}" =~ ^[0-9a-f]{16}$ ]] || fail "token must be 16 hex characters, got '${token}'"
  found="$(query "SELECT count() FROM ${DB}.marker WHERE token = '${token}'" 2>/dev/null || echo 0)"
  [[ "${found}" == 1 ]] || fail "marker ${token} is gone (count ${found})"
  echo "marker ${token} is still there"
  query "DROP DATABASE IF EXISTS ${DB} SYNC"
}

pod_test() {
  local pod uid token
  wait_ready
  check_volume
  step "write a marker"
  token="$(write)"
  echo "marker ${token}"
  pod="$(pod_name)"
  uid="$(pod_uid "${pod}")"
  step "delete ${pod}"
  "${KCTL[@]}" delete pod "${pod}" --wait=true >/dev/null
  for _ in $(seq 1 150); do
    local now
    now="$(pod_uid "${pod}")"
    if [[ -n "${now}" && "${now}" != "${uid}" ]]; then break; fi
    sleep 2
  done
  [[ "$(pod_uid "${pod}")" != "${uid}" ]] || fail "${pod} was not recreated"
  wait_ready
  check_volume
  step "check the marker"
  verify "${token}"
}

case "${1:-pod}" in
  pod) pod_test ;;
  write)
    wait_ready
    check_volume
    token="$(write)"
    echo "marker ${token}: restart Docker Desktop, wait for the nodes, then run"
    echo "  make kind-clickhouse-persistence MODE=verify TOKEN=${token}"
    ;;
  verify)
    [[ -n "${2:-}" ]] || fail "usage: $0 verify TOKEN"
    wait_ready
    check_volume
    verify "$2"
    ;;
  *) fail "usage: $0 [pod | write | verify TOKEN]" ;;
esac
