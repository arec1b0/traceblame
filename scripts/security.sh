#!/usr/bin/env bash
# Security stage (design §1.4): gitleaks over the full git history, Syft SBOMs of the source
# tree, Grype on the SBOM. Scanners run as container images pinned by digest
# (tools/versions.env). Reports go to .security/. The gate: 0 critical findings without a
# waiver in .grype.yaml.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../tools/versions.env
source "${ROOT}/tools/versions.env"
OUT="${ROOT}/.security"
CACHE="${XDG_CACHE_HOME:-${HOME}/.cache}/traceblame-security"

if ! docker info >/dev/null 2>&1; then
  echo "security: docker is not reachable." >&2
  echo "On WSL2: Docker Desktop -> Settings -> Resources -> WSL Integration -> enable this distro." >&2
  exit 1
fi

# Every waiver needs a reason that ends in "expires YYYY-MM-DD", and must not be expired.
check_waivers() {
  local today reason expires
  today="$(date -u +%F)"
  while IFS= read -r reason; do
    expires="$(grep -oE 'expires [0-9]{4}-[0-9]{2}-[0-9]{2}' <<<"${reason}" | cut -d' ' -f2 || true)"
    if [[ -z "${expires}" ]]; then
      echo "security: waiver without 'expires YYYY-MM-DD': ${reason}" >&2
      return 1
    fi
    if [[ "${expires}" < "${today}" ]]; then
      echo "security: expired waiver: ${reason}" >&2
      return 1
    fi
  done < <(grep -E '^[[:space:]]+reason:' "${ROOT}/.grype.yaml" || true)
}

check_waivers
mkdir -p "${OUT}" "${CACHE}"
# Run as the invoking user so reports stay user-owned. The Syft and Grype images ship /tmp
# as root 0755, and a plain tmpfs inherits that mode, so set mode=1777 explicitly.
run() { docker run --rm --user "$(id -u):$(id -g)" --tmpfs /tmp:rw,mode=1777 -e HOME=/tmp "$@"; }

echo "==> gitleaks: full git history"
# safe.directory via environment only: the mounted repo has a different owner in the container.
run -v "${ROOT}:/repo:ro" \
  -e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory -e GIT_CONFIG_VALUE_0=/repo \
  "${GITLEAKS_IMAGE}" git --no-banner --no-color --redact --log-opts="--all" \
  --report-format json --report-path /dev/stdout /repo >"${OUT}/gitleaks.json" || {
  echo "security: gitleaks found secrets (see .security/gitleaks.json)" >&2
  exit 1
}

echo "==> syft: SBOM of the source tree"
run -v "${ROOT}:/repo:ro" -v "${OUT}:/out" -e XDG_CACHE_HOME=/tmp \
  "${SYFT_IMAGE}" scan dir:/repo --source-name traceblame \
  --exclude './.venv/**' --exclude './.tools/**' --exclude './.git/**' \
  -o spdx-json=/out/sbom.spdx.json -o cyclonedx-json=/out/sbom.cdx.json

echo "==> grype: vulnerabilities in the SBOM (fail on critical)"
# Mount the repo directory, not the single .grype.yaml file. On WSL2, Docker Desktop keeps a
# helper mount per bind source, and a file-level one stops git in WSL from recreating
# .grype.yaml after a branch switch deletes it.
run -v "${ROOT}:/repo:ro" -v "${OUT}:/out" -v "${CACHE}:/cache" \
  -e GRYPE_DB_CACHE_DIR=/cache/grype-db -e XDG_CACHE_HOME=/tmp \
  "${GRYPE_IMAGE}" sbom:/out/sbom.spdx.json --config /repo/.grype.yaml \
  --fail-on critical -o table=/out/grype.txt -o json=/out/grype.json || {
  status=$?
  cat "${OUT}/grype.txt" >&2 || true
  echo "security: grype gate failed (exit ${status})" >&2
  exit 1
}
cat "${OUT}/grype.txt"
echo "security: ok (reports in .security/)"
