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
uv run --frozen pre-commit install-hooks
uv run --frozen pre-commit install --hook-type pre-push
