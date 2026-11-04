# check=skip=InvalidDefaultArgInFrom
# TraceBlame trace-assembler image (issue #35).
#
# Built by `make kind-deploy` (scripts/kind-deploy.sh passes the pinned images from
# tools/versions.env). The build context is services/assembler/. linux/amd64, static binary,
# distroless non-root. The base image ARGs have no defaults on purpose (tools/versions.env is
# their only source), hence the check skip on the first line.
ARG GO_BUILD_IMAGE
ARG DISTROLESS_STATIC_IMAGE

FROM ${GO_BUILD_IMAGE} AS build
ARG VERSION=1.0.0
WORKDIR /src
COPY go.mod go.sum ./
RUN GOFLAGS=-mod=readonly go mod download
COPY . .
ENV CGO_ENABLED=0 GOTOOLCHAIN=local GOOS=linux GOARCH=amd64
RUN go build -trimpath \
      -ldflags "-s -w -X github.com/arec1b0/traceblame/services/assembler/internal/config.Version=${VERSION}" \
      -o /assembler ./cmd/assembler

FROM ${DISTROLESS_STATIC_IMAGE}
COPY --from=build /assembler /assembler
USER 65532:65532
# Prometheus self-telemetry, /healthz and /readyz
EXPOSE 9464
ENTRYPOINT ["/assembler"]
