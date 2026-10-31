#!/usr/bin/env bash
# Go line-coverage gate (design §1.4): run the tests of PACKAGES in MODULE_DIR with the race
# detector and coverage over the same packages; fail when the total is below MIN percent.
# Usage: scripts/go-coverage-gate.sh MODULE_DIR PACKAGES MIN
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="${ROOT}/.tools/bin:${PATH}"
export GOTOOLCHAIN=local

module="$1" packages="$2" min="$3"
profile="$(mktemp)"
trap 'rm -f "${profile}"' EXIT

cd "${ROOT}/${module}"
go test -race -count=1 -covermode=atomic -coverpkg="${packages}" -coverprofile="${profile}" "${packages}"
total="$(go tool cover -func="${profile}" | awk '/^total:/ { sub("%", "", $3); print $3 }')"
echo "coverage gate: ${module}/${packages} ${total}% >= ${min}%"
if ! awk -v total="${total}" -v min="${min}" 'BEGIN { exit !(total + 0 >= min + 0) }'; then
  echo "coverage gate: ${module}/${packages} is below ${min}%" >&2
  exit 1
fi
