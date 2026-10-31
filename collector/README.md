# TraceBlame collector distribution

A custom OpenTelemetry Collector built with ocb (design §1.2): upstream receivers, processors
and exporters, plus the TraceBlame normalizer processor.

| Path | What it is |
|---|---|
| `builder-config.yaml` | ocb manifest; every component pinned to one upstream release line |
| `processor/normalizer/` | the normalizer processor (component type `normalizer`) |
| `config/otelcol.yaml` | default configuration baked into the image |
| `Dockerfile` | image build: ocb in a pinned Go image, binary on distroless `static:nonroot` |
| `test/smoke/` | end-to-end tests of a built binary: OTLP/HTTP in, file exporter out |
| `testdata/fixtures/` | golden span fixtures (real SDK exports, dataset D4) |

## Versions

The distribution uses the module set of upstream's `otelcol-contrib` 0.164.0: ocb and core
beta modules v0.164.0, core stable modules v1.70.0, contrib v0.164.1. The same
`otelcol-contrib` release captured the fixtures. All versions live in
[tools/versions.env](../tools/versions.env); `builder_config_test.go` fails when the
manifest or `go.mod` drifts from them, and ocb's strict versioning check fails the build when
a component pulls in a different core minor.

Component types follow v0.164.0: the OTLP exporters are `otlp_grpc` and `otlp_http` (the old
`otlp` / `otlphttp` names are deprecated aliases).

## Build and test

From WSL2 or Linux, in the repository root:

```bash
make collector          # ocb build: collector/_build/otelcol/otelcol-traceblame
make collector-smoke    # end-to-end tests against that binary
make collector-image    # image build, image smoke tests, Syft SBOM, Grype gate (needs Docker)
```

`make collector-image` runs the end-to-end tests a second time against the binary copied out
of the image, then writes the SBOM and scan reports to `.security/otelcol-traceblame/`. In CI,
`collector-image.yml` does the same on every change; on `main` it pushes
`ghcr.io/<owner>/traceblame-otelcol:<sha>` and signs it, with the SBOM attested, through
`sign-image.yml`.

ocb runs with `GOWORK=off`: the generated module in `_build/otelcol` is not part of the
repository's `go.work`, and reaches the normalizer through a `replace` that ocb writes.

## Notes

- On WSL2 the `memory_limiter` percentage limits fail: the cgroup reader cannot parse Docker
  Desktop's mountinfo entry. The smoke tests use fixed `limit_mib`; inside containers the
  percentage limits work.
- The image is linux/amd64 only, like the pinned ocb release binary.
