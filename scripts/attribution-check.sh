#!/usr/bin/env bash
# Attribution rule: no AI attribution in commits or pull requests (project instructions,
# "Workflow"). Run by the pre-push hook (pre-commit stage pre-push; `make bootstrap`
# installs it), by `make attribution`, and by the CI job `attribution` on every pull request.
#
# Commits in the range fail on
#   - a Co-Authored-By, Assisted-by or Generated-by trailer, as git reads trailers, or as a
#     line starting with that key (git ignores a trailer line that is not in the last
#     paragraph or is mixed with prose);
#   - "Generated with" (case-sensitive) or the robot emoji in the body (not the subject).
# Plain mentions of the instructions file or of Claude, in a subject or a body, pass.
#
# The PR title and body (CI only, from PR_TITLE and PR_BODY) fail on the same phrases and on
# a trailer-key line: a merge commit's body is the PR title.
#
# The range, in order: the first argument; PRE_COMMIT_FROM_REF..PRE_COMMIT_TO_REF, which
# pre-commit sets for the pushed ref (for a new branch, from the parent of its oldest commit
# that is on no remote ref: the hook never sees the zero remote sha); origin/main..HEAD.
# pre-commit passes only the first ref of a push, and runs no hook for a push that adds no
# commits to the remote: push one branch at a time. An unresolvable range fails closed.
#
# Usage: scripts/attribution-check.sh [RANGE]
set -euo pipefail

TRAILER_KEYS=(Co-Authored-By Assisted-by Generated-by)
PHRASES=("Generated with" "🤖")

range="${1:-}"
if [[ -z "${range}" ]]; then
  if [[ -n "${PRE_COMMIT_FROM_REF:-}" && -n "${PRE_COMMIT_TO_REF:-}" ]]; then
    range="${PRE_COMMIT_FROM_REF}..${PRE_COMMIT_TO_REF}"
  else
    range="origin/main..HEAD"
  fi
fi

if ! commits="$(git rev-list --end-of-options "${range}" 2>&1)"; then
  echo "attribution-check: cannot resolve the range ${range} (git: ${commits}); refusing" >&2
  exit 1
fi

key_pattern="^($(IFS='|'; echo "${TRAILER_KEYS[*]}")):"
findings=0

report() {
  echo "attribution-check: $1: $2" >&2
  findings=$((findings + 1))
}

# check_text LABEL TEXT: the phrases, and a trailer-key line.
check_text() {
  local label="$1" text="$2" phrase
  for phrase in "${PHRASES[@]}"; do
    if grep -qF -- "${phrase}" <<<"${text}"; then
      report "${label}" "contains \"${phrase}\""
    fi
  done
  if grep -qiE -- "${key_pattern}" <<<"${text}"; then
    report "${label}" "has an attribution trailer line"
  fi
}

count=0
while IFS= read -r sha; do
  [[ -n "${sha}" ]] || continue
  count=$((count + 1))
  label="$(git log -1 --format='%h %s' "${sha}")"
  before=${findings}
  for key in "${TRAILER_KEYS[@]}"; do
    if [[ -n "$(git log -1 --format="%(trailers:key=${key},valueonly)" "${sha}" | tr -d '[:space:]')" ]]; then
      report "${label}" "has a ${key} trailer"
    fi
  done
  body="$(git log -1 --format=%b "${sha}")"
  # A trailer-key line that git did not read as a trailer: reported only if git found none.
  if ((findings == before)) && grep -qiE -- "${key_pattern}" <<<"${body}"; then
    report "${label}" "body has a trailer line outside a trailer block"
  fi
  for phrase in "${PHRASES[@]}"; do
    if grep -qF -- "${phrase}" <<<"${body}"; then
      report "${label}" "body contains \"${phrase}\""
    fi
  done
done <<<"${commits}"

[[ -z "${PR_TITLE:-}" ]] || check_text "PR title" "${PR_TITLE}"
[[ -z "${PR_BODY:-}" ]] || check_text "PR body" "${PR_BODY}"

if ((findings > 0)); then
  echo "attribution-check: ${findings} finding(s) in ${count} commit(s) of ${range}" >&2
  exit 1
fi
plural="s"
((count == 1)) && plural=""
echo "attribution-check: ${count} commit${plural} in ${range}, no AI attribution"
