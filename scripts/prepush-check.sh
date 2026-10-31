#!/usr/bin/env bash
# Pre-push PR-state rule (pre-commit hook, stage pre-push; `make bootstrap` installs it):
# never push to a branch whose PR is merged or closed, branch from origin/main instead.
# Passes when the branch has no PR or its only PR is OPEN; fails closed when gh cannot tell.
#
# The branch is the pushed remote ref (pre-commit's PRE_COMMIT_REMOTE_BRANCH), else the
# argument, else the current branch. pre-commit checks the first pushed ref only, and runs
# no hook for a push that adds no commits to the remote: push one branch at a time.
#
# Usage: scripts/prepush-check.sh [branch]
set -euo pipefail

ref="${PRE_COMMIT_REMOTE_BRANCH:-${1:-$(git rev-parse --abbrev-ref HEAD)}}"
case "${ref}" in
  refs/heads/*) branch="${ref#refs/heads/}" ;;
  refs/*)
    echo "prepush-check: ${ref} is not a branch, nothing to check"
    exit 0
    ;;
  *) branch="${ref}" ;;
esac

if ! prs="$(gh pr list --head "${branch}" --state all --json state,number 2>&1)"; then
  echo "prepush-check: cannot read the PRs of ${branch} (gh: ${prs}); refusing to push" >&2
  exit 1
fi

python3 - "${branch}" "${prs}" <<'PY'
import json
import sys

branch, raw = sys.argv[1], sys.argv[2]
try:
    prs = json.loads(raw)
except ValueError:
    sys.exit(f"prepush-check: unexpected gh output for {branch}: {raw!r}; refusing to push")
if not prs:
    print(f"prepush-check: {branch} has no PR")
    sys.exit(0)
listed = ", ".join(f"#{p['number']} {p['state']}" for p in prs)
if len(prs) == 1 and prs[0]["state"] == "OPEN":
    print(f"prepush-check: {branch} has one open PR ({listed})")
    sys.exit(0)
sys.exit(
    f"prepush-check: {branch} has {listed}: never push to a branch whose PR is merged or"
    " closed; branch from origin/main instead"
)
PY
