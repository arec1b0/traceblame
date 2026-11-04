#!/usr/bin/env bash
# The trace-assembler embeds a copy of the ClickHouse statements it runs (go:embed cannot reach
# outside the module). The contract statements and tests/integration/*.sql are the source; this
# script copies them to services/assembler/internal/ch/sql/. With --check it only compares and
# fails on a difference (run by `pytest -m contract`).
#
# Usage: scripts/sync-assembler-sql.sh [--check]
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dest="${root}/services/assembler/internal/ch/sql"
contract="${root}/packages/storage/src/traceblame_storage/contract"
sources=(
  "${contract}/trace_arrivals_scan.sql"
  "${contract}/spans_by_trace_read.sql"
  "${root}/tests/integration/trace_summary_assembled_read.sql"
  "${root}/tests/integration/trace_summary_reconcile_read.sql"
)

mode="${1:-}"
if [[ -n "${mode}" && "${mode}" != "--check" ]]; then
  echo "usage: $0 [--check]" >&2
  exit 2
fi

status=0
for src in "${sources[@]}"; do
  name="$(basename "${src}")"
  if [[ "${mode}" == "--check" ]]; then
    if ! cmp -s "${src}" "${dest}/${name}"; then
      echo "sync-assembler-sql: ${dest}/${name} differs from ${src}; run scripts/sync-assembler-sql.sh" >&2
      status=1
    fi
  else
    mkdir -p "${dest}"
    cp "${src}" "${dest}/${name}"
  fi
done
exit "${status}"
