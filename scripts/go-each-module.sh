#!/usr/bin/env bash
# Run a command inside every module listed in go.work.
# Usage: scripts/go-each-module.sh <command> [args...]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="${ROOT}/.tools/bin:${PATH}"
export GOTOOLCHAIN=local

cd "${ROOT}"
mapfile -t modules < <(go list -m -f '{{.Dir}}')
for dir in "${modules[@]}"; do
  echo "==> ${dir#"${ROOT}/"}: $*" >&2
  (cd "${dir}" && "$@")
done
