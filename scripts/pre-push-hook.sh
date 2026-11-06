#!/usr/bin/env bash
# The pre-push hook (scripts/prepush-check.sh and the attribution check, through pre-commit),
# resolved in the worktree that pushes. .git/hooks is shared by every worktree, so the hook
# bootstrap installs only execs this script from the pushing worktree; pre-commit is found the
# way make finds it: this worktree's .venv, then its pinned uv (.tools/bin/uv run --frozen),
# then PATH. Git passes the remote as arguments and the refs on stdin; both go through.
set -euo pipefail

TOP="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${TOP}"
HOOK_DIR="$(git rev-parse --git-path hooks 2>/dev/null || echo "${TOP}/.git/hooks")"
ARGS=(hook-impl --config=.pre-commit-config.yaml --hook-type=pre-push --hook-dir "${HOOK_DIR}" -- "$@")

if [ -x "${TOP}/.venv/bin/python" ]; then
    exec "${TOP}/.venv/bin/python" -m pre_commit "${ARGS[@]}"
elif [ -x "${TOP}/.tools/bin/uv" ]; then
    export UV_PYTHON_PREFERENCE="${UV_PYTHON_PREFERENCE:-only-managed}"
    export UV_LINK_MODE="${UV_LINK_MODE:-copy}"
    exec "${TOP}/.tools/bin/uv" run --frozen pre-commit "${ARGS[@]}"
elif command -v pre-commit >/dev/null; then
    exec pre-commit "${ARGS[@]}"
fi
echo "pre-push: pre-commit not found in ${TOP} (.venv, .tools/bin/uv) or on PATH; run make bootstrap" >&2
exit 1
