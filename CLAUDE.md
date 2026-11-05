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
- Messaging: NATS JetStream (trace.completed, traceblame.finding.v1, traceblame.blame.v1)
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
- pre-push hook enforces the PR-state rule and the attribution rule; never bypass with
  --no-verify. CI repeats the attribution check (job "attribution (no AI trailers or
  phrases)") on the PR's commits, title and body.
- One ref per git push (the pre-push hook checks only the first ref).
- Before code for an issue: short plan (files, interfaces, tests), wait for my OK on anything
  that changes a contract (profile, JSON Schema, OpenAPI, ClickHouse schema).
- Tests first for normalizer mappings, detectors, attributor. Coverage >= 80% there.
- Do not create git commits with Co-Authored-By or any AI attribution trailers.
  scripts/attribution-check.sh enforces it (`make attribution`; the hook and CI run it), so
  there is no manual grep. It also rejects, in a commit body, PR title or PR body, a line that
  starts with the tool's footer phrase (after optional indentation and the robot emoji) and
  the robot emoji anywhere. The phrase in the middle of a sentence is fine.
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
  `uv sync --all-packages --locked`, Go module download, pre-commit hook environments, and the
  pre-push hook (`pre-commit install --hook-type pre-push`: scripts/prepush-check.sh), and
  `pnpm -C sdk/ts install --frozen-lockfile` (Node and pnpm pinned in tools/versions.env)
- `make lint`: pre-commit over all tracked files (stage new files first); includes eslint and
  `tsc --noEmit` for sdk/ts, and pyright strict over services, packages, sdk, tests and tools
  (tools/typings: stubs for what exists only in the capture scripts' own environments)
- `make test`: pytest with coverage gates (`COV_MIN`), `go test -race` per go.work module,
  `vitest run --coverage` in sdk/ts (same `COV_MIN`)
- `.tools/bin/pnpm -C sdk/ts build|test|lint|typecheck`: the TypeScript shim alone
- `make contract`: builds `collector/cmd/normalize`, `traceblame-contracts gen-schema --check`
  (JSON Schema + the normalizer's profile copy), gap report in `.contract/`, `pytest -m contract`
  (profile <-> JSON Schema, semconv snapshot, golden fixtures through the Go normalizer)
- `make integration`: ClickHouse (pinned 26.8 LTS), Postgres and NATS JetStream in test
  containers: migrations of both databases, N-1 test, table-level no-content proof through the
  built collector, the trace-assembler end to end (needs Docker; builds the collector and the
  assembler)
- `uv run traceblame-storage check|migrate|status`: forward-only ClickHouse and Postgres
  migrations (ADR-0004, ADR-0007); the `--url` scheme (`http(s)://` or `postgres://`) picks the
  database
- `make security`: gitleaks (full history), Syft SBOM, Grype gate (0 critical without a
  waiver in .grype.yaml); scanner images pinned by digest, needs Docker
- `make fixtures`: re-capture golden fixtures from Ollama (`OLLAMA_URL=...`)
- `make fixtures-agentic-rag`: real agentic-rag runs on CUAD through the kind agent and gateway
  (`APP_DIR=../agentic-rag`, `OLLAMA_URL=...`; needs `make kind-up kind-deploy`): canary
  preflight, tenant/marker checks against ClickHouse, report in `evals/reports/sdk-py-kind-*`,
  fixtures `agentic_rag_cuad_*` with provenance
- `make fixtures-runbook-agent`: real runbook-agent runs in the kind sandbox
  (`RUNBOOK_APP_DIR=../runbook-agent`, `OLLAMA_URL=...`; needs `make kind-up kind-deploy
  kind-runbook-up`; about 2 h): image with the packed shim, safety/network probes, canary,
  24 seeded tasks + one HTTP 404 task, report `evals/reports/sdk-ts-kind-*`, fixtures `runbook_*`
- `uv run traceblame-contracts gen-schema`: regenerate the JSON Schema and the normalizer's
  embedded profile copy after editing the profile
- `make assembler`: build the trace-assembler (`services/assembler/_build/assembler`;
  `scripts/sync-assembler-sql.sh` keeps its embedded SQL equal to the storage contract)
- `make collector`: ocb build of the collector distribution into `collector/_build/otelcol`
  (`GOWORK=off`; versions from tools/versions.env)
- `make collector-smoke`: end-to-end tests of the built collector (`OTELCOL_BIN=...` to override)
- `make collector-image`: collector image build + image smoke + end-to-end tests on the image's
  binary + Syft SBOM + Grype gate (needs Docker)
- `make kind-up` / `make kind-down`: local kind cluster `traceblame` (kind-up also generates
  the TLS, htpasswd and ClickHouse Secrets; never committed)
- `make kind-deploy`: images, ClickHouse via the Altinity operator, NATS JetStream, Postgres,
  migrations of both databases, agent DaemonSet, gateway and trace-assembler Deployments into
  kind (deploy/kind/README.md)
- `make kind-clickhouse-persistence`: the CHI pod mounts a Bound PVC at /var/lib/clickhouse and a
  marker survives a pod deletion (`MODE=write`, then `MODE=verify TOKEN=...` around a manual
  Docker Desktop restart; deploy/kind/README.md)
- `make kind-runbook-up` / `make kind-runbook-down`: the runbook-agent sandbox and its
  monitoring (namespace-only RBAC, derived NetworkPolicy addresses, pinned upstream rules)
- `make attribution`: no AI attribution trailers or footer phrases in origin/main..HEAD
  (scripts/attribution-check.sh [RANGE]; PR_TITLE and PR_BODY in the environment add the PR text)
- `make bench-scaling`: gateway 2 -> 4 -> 2 under steady load; span loss / split-trace report
  and the trace-assembler's lag (root end to `trace.completed`, p50/p95) and rejoined split
  traces into evals/reports/ (RATE, LOAD, UP_AT, DOWN_AT override the defaults)
- `make demo`: M3
