# TraceBlame — project rules for Claude Code

## What this is
TraceBlame turns OTel GenAI traces into a verdict "who is to blame"
(retrieval | prompt | tool | model | orchestration | unknown) with confidence and evidence.
Ground truth comes from our own fault injector. Part of Aegis Platform
(traceblame, agentgate, promptops + umbrella aegis-platform).
Source of truth for design: "Aegis Platform — Top-3 Design Docs", section 1 (copy in docs/design.md).

MVP target: top-1 accuracy >= 0.80 on single-fault traces (D1-test), macro-F1 >= 0.75,
app overhead p95 <= 3%, pipeline lag p95 <= 60 s.

## Stack (fixed, change only via ADR)
- Data plane: Go (OTel Collector custom distro via ocb, normalizer processor, trace-assembler)
- Analysis/control: Python 3.12, uv workspace, FastAPI (api, detectors, attributor, replayer, faults)
- SDK shims: Python + TypeScript over OpenInference / OpenLLMetry
- Storage: ClickHouse (spans, findings, blame, injections), Postgres (replay_jobs state machine)
- Messaging: NATS JetStream (spans.sampled, trace.completed, traceblame.finding.v1, traceblame.blame.v1)
- Deploy: kind locally, Helm + Argo CD; Grafana/Alertmanager dashboards-as-code, Sloth SLOs
- Supply chain: cosign keyless, Syft SBOM, scanners pinned by digest, release-please

## Hard rules
1. Telemetry: OTel GenAI semconv PINNED to an explicit version (verify the current one from
   official sources, never from memory). Own attributes only under `aegis.*`.
   Correlation IDs: trace_id, aegis.session.id, aegis.tenant.id, aegis.bundle.digest, aegis.decision.id.
2. Content capture OFF by default. Prompt/completion content is hashed/redacted in the normalizer.
   Never log content. Logs = JSON with trace_id, detector, version.
3. Every detector/attributor output carries its version. Findings idempotent by
   (trace_id, detector, detector_version).
4. ClickHouse migrations forward-only, expand -> migrate -> contract. Code N must work with schema N-1.
   Normalizer supports profile v(N) and v(N-1).
5. No synthetic toy data. Eval traces = real queries (CUAD, runbook agent tasks, Agentic RAG)
   + labelled fault injection. Split calibration/test BY SOURCE QUERY, not by trace.
6. Every alert links to docs/runbooks/<alert>.md. Every release has a documented rollback.
7. Self-telemetry pipeline is separate from the analysed pipeline (no recursion).
8. Thresholds in the design are starting hypotheses: keep them in config, not hardcoded.

## Workflow
- One GitHub issue = one branch = one PR. Conventional Commits.
- Before code for an issue: short plan (files, interfaces, tests), wait for my OK on anything
  that changes a contract (profile, JSON Schema, OpenAPI, ClickHouse schema).
- Tests first for normalizer mappings, detectors, attributor. Coverage >= 80% there.
- Do not create git commits with Co-Authored-By or any AI attribution trailers.
  Commits are GPG-signed with my key: never pass --no-gpg-sign, never change git config.
- Do not push, tag or release without explicit request.
- Dev env: repo lives in WSL ext4 (~/src/traceblame), never /mnt/c; Windows 11 host; Go/ocb/kind/make run in WSL2 (Ubuntu) or Docker. Scripts must work there.
  Python: `uv` only, dev deps in `[dependency-groups] dev`, never `tool.uv.dev-dependencies`.
- Never start long-running host services (Ollama, Docker, DBs) yourself; use the running instance (e.g. `OLLAMA_URL`) or ask me.
- If a design decision is missing or contradicts reality (library API, semconv change),
  stop and write it as an ADR draft in docs/adr/ instead of improvising.

## Commands (fill in as they appear)
Run from WSL2 (Ubuntu) in the repo root; never run `make` or `uv` from Windows in this checkout.
- `make bootstrap`: install pinned tools into `.tools/` (tools/versions.env + tools/checksums.txt),
  `uv sync --all-packages --locked`, Go module download, pre-commit hook environments
- `make lint`: pre-commit over all tracked files (stage new files first)
- `make test`: pytest with coverage gates (`COV_MIN`), `go test -race` per go.work module
- `make contract`: builds `collector/cmd/normalize`, `traceblame-contracts gen-schema --check`
  (JSON Schema + the normalizer's profile copy), gap report in `.contract/`, `pytest -m contract`
  (profile <-> JSON Schema, semconv snapshot, golden fixtures through the Go normalizer)
- `make security`: gitleaks (full history), Syft SBOM, Grype gate (0 critical without a
  waiver in .grype.yaml); scanner images pinned by digest, needs Docker
- `make fixtures`: re-capture golden fixtures from Ollama (`OLLAMA_URL=...`)
- `uv run traceblame-contracts gen-schema`: regenerate the JSON Schema and the normalizer's
  embedded profile copy after editing the profile
- `make collector`: ocb build of the collector distribution into `collector/_build/otelcol`
  (`GOWORK=off`; versions from tools/versions.env)
- `make collector-smoke`: end-to-end tests of the built collector (`OTELCOL_BIN=...` to override)
- `make collector-image`: collector image build + image smoke + end-to-end tests on the image's
  binary + Syft SBOM + Grype gate (needs Docker)
- `make kind-up` / `make kind-down`: local kind cluster `traceblame`
- `make demo`: M3
