# check=skip=InvalidDefaultArgInFrom
# TraceBlame collector distribution image (issue #7).
#
# Build with `make collector-image`: scripts/collector-image.sh passes the pinned images,
# the ocb version and its sha256 from tools/versions.env and tools/checksums.txt. The build
# context is collector/. linux/amd64 only, like the pinned ocb binary. The base image ARGs
# have no defaults on purpose (tools/versions.env is their only source), hence the check
# skip on the first line.
ARG GO_BUILD_IMAGE
ARG DISTROLESS_STATIC_IMAGE

FROM ${GO_BUILD_IMAGE} AS build
ARG OCB_VERSION
ARG OCB_SHA256
SHELL ["/bin/bash", "-euo", "pipefail", "-c"]
RUN curl -fsSL --retry 3 -o /usr/local/bin/ocb \
      "https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/cmd%2Fbuilder%2Fv${OCB_VERSION}/ocb_${OCB_VERSION}_linux_amd64" \
    && echo "${OCB_SHA256}  /usr/local/bin/ocb" | sha256sum --check --strict - \
    && chmod 0755 /usr/local/bin/ocb
WORKDIR /src/collector
COPY . .
ENV CGO_ENABLED=0 GOTOOLCHAIN=local GOFLAGS=-trimpath
RUN mkdir -p _build && ocb --config builder-config.yaml

FROM ${DISTROLESS_STATIC_IMAGE}
COPY --from=build /src/collector/_build/otelcol/otelcol-traceblame /otelcol-traceblame
COPY config/otelcol.yaml /etc/otelcol-traceblame/config.yaml
USER 65532:65532
# OTLP gRPC, OTLP HTTP, Prometheus self-telemetry, health_check
EXPOSE 4317 4318 8888 13133
ENTRYPOINT ["/otelcol-traceblame"]
CMD ["--config=/etc/otelcol-traceblame/config.yaml"]
