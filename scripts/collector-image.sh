#!/usr/bin/env bash
# Build the collector image, smoke-test it, then SBOM it and run the Grype gate (issue #7).
#
# - Base images, the ocb version and its sha256 come from tools/versions.env and
#   tools/checksums.txt.
# - The smoke tests run twice: the image itself (component list, health_check), and the
#   binary copied out of the image through the end-to-end Go smoke tests (collector/test/smoke).
# - The image is saved as a docker archive and scanned by `scripts/security.sh image`.
#
# Usage: scripts/collector-image.sh   (COLLECTOR_IMAGE=name:tag to override the local tag)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../tools/versions.env
source "${ROOT}/tools/versions.env"
export PATH="${ROOT}/.tools/bin:${PATH}"
export GOTOOLCHAIN=local

IMAGE="${COLLECTOR_IMAGE:-traceblame-otelcol:local}"
BUILD="${ROOT}/collector/_build"
ARCHIVE="${BUILD}/otelcol-traceblame.image.tar"
BIN="${BUILD}/image/otelcol-traceblame"

if ! docker info >/dev/null 2>&1; then
  echo "collector-image: docker is not reachable." >&2
  echo "On WSL2: Docker Desktop -> Settings -> Resources -> WSL Integration -> enable this distro." >&2
  exit 1
fi

ocb_sha256="$(awk -v f="ocb_${OCB_VERSION}_linux_amd64" '$2 == f { print $1 }' "${ROOT}/tools/checksums.txt")"
if [[ -z "${ocb_sha256}" ]]; then
  echo "collector-image: no checksum for ocb ${OCB_VERSION} in tools/checksums.txt" >&2
  exit 1
fi

echo "==> build ${IMAGE}"
docker build \
  --platform linux/amd64 \
  --build-arg "GO_BUILD_IMAGE=${GO_BUILD_IMAGE}" \
  --build-arg "DISTROLESS_STATIC_IMAGE=${DISTROLESS_STATIC_IMAGE}" \
  --build-arg "OCB_VERSION=${OCB_VERSION}" \
  --build-arg "OCB_SHA256=${ocb_sha256}" \
  --file "${ROOT}/collector/Dockerfile" \
  --tag "${IMAGE}" \
  "${ROOT}/collector"

echo "==> smoke: the image lists the normalizer and becomes healthy"
docker run --rm "${IMAGE}" components | grep -qE '^ +- name: normalizer$' || {
  echo "collector-image: the normalizer processor is not in the image" >&2
  exit 1
}
cid="$(docker run --detach --publish 127.0.0.1::13133 "${IMAGE}")"
trap 'docker rm --force "${cid}" >/dev/null 2>&1 || true' EXIT
port="$(docker port "${cid}" 13133/tcp | head -n1 | sed 's/.*://')"
healthy=""
for _ in $(seq 1 100); do
  if curl -fsS "http://127.0.0.1:${port}/" >/dev/null 2>&1; then
    healthy=1
    break
  fi
  sleep 0.2
done
if [[ -z "${healthy}" ]]; then
  docker logs "${cid}" >&2 || true
  echo "collector-image: the container did not become healthy" >&2
  exit 1
fi
docker rm --force "${cid}" >/dev/null
trap - EXIT

echo "==> smoke: end-to-end tests against the binary from the image"
mkdir -p "$(dirname "${BIN}")"
copy="$(docker create "${IMAGE}")"
docker cp "${copy}:/otelcol-traceblame" "${BIN}" >/dev/null
docker rm "${copy}" >/dev/null
(cd "${ROOT}/collector" && OTELCOL_BIN="${BIN}" go test -race -count=1 ./test/smoke/)

echo "==> save ${ARCHIVE}"
docker save --output "${ARCHIVE}" "${IMAGE}"
bash "${ROOT}/scripts/security.sh" image "${ARCHIVE}" otelcol-traceblame
