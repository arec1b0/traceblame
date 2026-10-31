# check=skip=InvalidDefaultArgInFrom
# Load generator of bench/gateway-scaling (issue #10): the loadgen binary and the content-off
# golden fixtures it replays. Built by scripts/kind-deploy.sh from collector/_build/loadgen;
# the base image comes from tools/versions.env (no default on purpose).
ARG DISTROLESS_STATIC_IMAGE
FROM ${DISTROLESS_STATIC_IMAGE}
COPY loadgen /loadgen
COPY fixtures /fixtures
USER 65532:65532
ENTRYPOINT ["/loadgen"]
