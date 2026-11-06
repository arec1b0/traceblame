#!/usr/bin/env bash
# Set up a development checkout: pinned tools, Python and Go dependencies, pre-commit hook
# environments, and the pre-push git hook (the PR-state rule, scripts/prepush-check.sh).
# No commit hook: `make lint` runs pre-commit explicitly.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

export PATH="${ROOT}/.tools/bin:${PATH}"
export GOTOOLCHAIN=local
export UV_PYTHON_PREFERENCE="${UV_PYTHON_PREFERENCE:-only-managed}"
export UV_LINK_MODE="${UV_LINK_MODE:-copy}"

bash scripts/install-tools.sh
uv sync --all-packages --locked
bash scripts/go-each-module.sh go mod download
pnpm -C sdk/ts install --frozen-lockfile
uv run --frozen pre-commit install-hooks
# The pre-push hook: .git/hooks is shared by every worktree, so the hook only execs
# scripts/pre-push-hook.sh of the worktree that pushes, which finds pre-commit there (not the
# Python of whichever worktree ran bootstrap last, as `pre-commit install` would bake in).
HOOK="$(git rev-parse --git-path hooks)/pre-push"
mkdir -p "$(dirname "${HOOK}")"
cat > "${HOOK}" <<'HOOK'
#!/usr/bin/env bash
# Installed by scripts/bootstrap.sh: run the pushing worktree's pre-push hook.
top="$(git rev-parse --show-toplevel)"
if [ -x "${top}/scripts/pre-push-hook.sh" ]; then
    exec "${top}/scripts/pre-push-hook.sh" "$@"
fi
# A worktree from before scripts/pre-push-hook.sh: pre-commit from its own venv or pinned uv.
args=(hook-impl --config=.pre-commit-config.yaml --hook-type=pre-push
    --hook-dir "$(cd "$(dirname "$0")" && pwd)" -- "$@")
if [ -x "${top}/.venv/bin/python" ]; then
    exec "${top}/.venv/bin/python" -m pre_commit "${args[@]}"
fi
exec "${top}/.tools/bin/uv" run --frozen pre-commit "${args[@]}"
HOOK
chmod +x "${HOOK}"
