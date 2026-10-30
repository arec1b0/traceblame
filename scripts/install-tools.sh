#!/usr/bin/env bash
# Install the pinned developer tools into .tools/ (Linux x86_64 only; on Windows use WSL2).
# Versions come from tools/versions.env, sha256 checksums from tools/checksums.txt.
# Re-running is a no-op for tools whose pinned version is already installed.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOOLS="${ROOT}/.tools"
BIN="${TOOLS}/bin"
STAMPS="${TOOLS}/stamps"
# shellcheck source=../tools/versions.env
source "${ROOT}/tools/versions.env"

if [[ "$(uname -s)" != "Linux" || "$(uname -m)" != "x86_64" ]]; then
  echo "install-tools: only Linux x86_64 is supported (on Windows, run from WSL2)" >&2
  exit 1
fi

mkdir -p "${BIN}" "${STAMPS}"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

# fetch NAME URL: download URL to $WORK/NAME and verify it against tools/checksums.txt.
fetch() {
  local name="$1" url="$2" expected
  expected="$(awk -v f="${name}" '$2 == f { print $1 }' "${ROOT}/tools/checksums.txt")"
  if [[ -z "${expected}" ]]; then
    echo "install-tools: no checksum for ${name} in tools/checksums.txt" >&2
    exit 1
  fi
  curl -fsSL --retry 3 -o "${WORK}/${name}" "${url}"
  echo "${expected}  ${WORK}/${name}" | sha256sum --check --quiet --strict -
}

# ensure TOOL VERSION FUNC: run FUNC unless TOOL is already installed at VERSION.
ensure() {
  local tool="$1" version="$2" func="$3"
  if [[ -f "${STAMPS}/${tool}-${version}" ]]; then
    echo "ok        ${tool} ${version}"
    return
  fi
  "${func}" "${version}"
  rm -f "${STAMPS}/${tool}-"*
  touch "${STAMPS}/${tool}-${version}"
  echo "installed ${tool} ${version}"
}

install_uv() {
  local name="uv-$1-x86_64-unknown-linux-gnu.tar.gz"
  fetch "${name}" "https://github.com/astral-sh/uv/releases/download/$1/uv-x86_64-unknown-linux-gnu.tar.gz"
  tar -xzf "${WORK}/${name}" -C "${WORK}"
  install -m 0755 "${WORK}/uv-x86_64-unknown-linux-gnu/uv" "${WORK}/uv-x86_64-unknown-linux-gnu/uvx" "${BIN}/"
}

install_go() {
  local name="go$1.linux-amd64.tar.gz"
  fetch "${name}" "https://go.dev/dl/${name}"
  rm -rf "${TOOLS}/go"
  tar -xzf "${WORK}/${name}" -C "${TOOLS}"
  ln -sf ../go/bin/go "${BIN}/go"
  ln -sf ../go/bin/gofmt "${BIN}/gofmt"
}

install_golangci_lint() {
  local name="golangci-lint-$1-linux-amd64.tar.gz"
  fetch "${name}" "https://github.com/golangci/golangci-lint/releases/download/v$1/${name}"
  tar -xzf "${WORK}/${name}" -C "${WORK}"
  install -m 0755 "${WORK}/golangci-lint-$1-linux-amd64/golangci-lint" "${BIN}/"
}

install_hadolint() {
  local name="hadolint-$1-linux-x86_64"
  fetch "${name}" "https://github.com/hadolint/hadolint/releases/download/v$1/hadolint-linux-x86_64"
  install -m 0755 "${WORK}/${name}" "${BIN}/hadolint"
}

install_gitleaks() {
  local name="gitleaks_$1_linux_x64.tar.gz"
  fetch "${name}" "https://github.com/gitleaks/gitleaks/releases/download/v$1/${name}"
  tar -xzf "${WORK}/${name}" -C "${WORK}" gitleaks
  install -m 0755 "${WORK}/gitleaks" "${BIN}/"
}

install_actionlint() {
  local name="actionlint_$1_linux_amd64.tar.gz"
  fetch "${name}" "https://github.com/rhysd/actionlint/releases/download/v$1/${name}"
  tar -xzf "${WORK}/${name}" -C "${WORK}" actionlint
  install -m 0755 "${WORK}/actionlint" "${BIN}/"
}

install_kind() {
  local name="kind-$1-linux-amd64"
  fetch "${name}" "https://github.com/kubernetes-sigs/kind/releases/download/v$1/kind-linux-amd64"
  install -m 0755 "${WORK}/${name}" "${BIN}/kind"
}

install_kubectl() {
  local name="kubectl-$1-linux-amd64"
  fetch "${name}" "https://dl.k8s.io/release/v$1/bin/linux/amd64/kubectl"
  install -m 0755 "${WORK}/${name}" "${BIN}/kubectl"
}

ensure uv "${UV_VERSION}" install_uv
ensure go "${GO_VERSION}" install_go
ensure golangci-lint "${GOLANGCI_LINT_VERSION}" install_golangci_lint
ensure hadolint "${HADOLINT_VERSION}" install_hadolint
ensure gitleaks "${GITLEAKS_VERSION}" install_gitleaks
ensure actionlint "${ACTIONLINT_VERSION}" install_actionlint
ensure kind "${KIND_VERSION}" install_kind
ensure kubectl "${KUBECTL_VERSION}" install_kubectl
