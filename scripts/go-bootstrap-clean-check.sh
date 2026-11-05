#!/usr/bin/env bash
# The Go step of `make bootstrap` (go mod download in every go.work module) must leave the tree
# clean: a missing checksum in go.work.sum or go.sum is added by `go mod download`, so a fresh
# worktree would come out of bootstrap dirty. Run the same step and fail if a tracked Go
# dependency file differs from the index.
# Usage: scripts/go-bootstrap-clean-check.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="${ROOT}/.tools/bin:${PATH}"
export GOTOOLCHAIN=local

cd "${ROOT}"
paths=(go.work go.work.sum ':(glob)**/go.mod' ':(glob)**/go.sum')

modules="$(go list -m -f '{{.Dir}}' | wc -l)"
if [ "${modules}" -eq 0 ]; then
  echo "go-bootstrap-clean: 0 modules in go.work, nothing was checked" >&2
  exit 1
fi

bash scripts/go-each-module.sh go mod download >&2

if ! git diff --quiet -- "${paths[@]}"; then
  echo "go-bootstrap-clean: go mod download changed tracked files (${modules} modules checked):" >&2
  git diff --stat -- "${paths[@]}" >&2
  echo "Commit the updated checksums (stage them first, then rerun)." >&2
  exit 1
fi
echo "go-bootstrap-clean: ${modules} modules checked, tree unchanged" >&2
