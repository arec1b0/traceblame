#!/usr/bin/env bash
# Vendor the OpenTelemetry semantic-conventions registry files that the telemetry profile
# depends on, pinned by tag and resolved to a commit (ADR-0001).
# Usage: tools/semconv/fetch.sh <version>     e.g. tools/semconv/fetch.sh 1.41.1
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VERSION="${1:?usage: tools/semconv/fetch.sh <version>}"
TAG="v${VERSION}"
REPO="open-telemetry/semantic-conventions"
DEST="${ROOT}/schemas/semconv/${TAG}"
FILES=(
  LICENSE
  model/error/registry.yaml
  model/gen-ai/registry.yaml
  model/gen-ai/deprecated/registry-deprecated.yaml
  model/server/registry.yaml
  model/service/registry.yaml
)

commit="$(curl -fsSL -H 'Accept: application/vnd.github.sha' "https://api.github.com/repos/${REPO}/commits/${TAG}")"
if [[ ! "${commit}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "fetch: could not resolve ${TAG} to a commit" >&2
  exit 1
fi

rm -rf "${DEST}"
for f in "${FILES[@]}"; do
  mkdir -p "${DEST}/$(dirname "${f}")"
  curl -fsSL --retry 3 -o "${DEST}/${f}" "https://raw.githubusercontent.com/${REPO}/${commit}/${f}"
done

cat > "${DEST}/source.yaml" <<YAML
# Vendored by tools/semconv/fetch.sh. Do not edit; re-run the script instead.
repository: ${REPO}
version: ${VERSION}
tag: ${TAG}
commit: ${commit}
schema_url: https://opentelemetry.io/schemas/${VERSION}
license: Apache-2.0 (see LICENSE)
YAML

(cd "${DEST}" && find . -type f ! -name SHA256SUMS -printf '%P\n' | LC_ALL=C sort | xargs sha256sum) \
  > "${DEST}/SHA256SUMS"
echo "fetch: vendored ${REPO}@${TAG} (${commit}) into ${DEST#"${ROOT}/"}"
