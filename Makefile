# TraceBlame developer entry points. Run from WSL2 (Ubuntu) or Linux, never from Windows.
SHELL := bash
.SHELLFLAGS := -euo pipefail -c
.DEFAULT_GOAL := help
MAKEFLAGS += --no-builtin-rules

export PATH := $(CURDIR)/.tools/bin:$(PATH)
export GOTOOLCHAIN := local
export UV_PYTHON_PREFERENCE := only-managed
# The checkout usually lives on /mnt/c: uv cache hardlinks do not work across filesystems.
export UV_LINK_MODE := copy

UV_RUN := uv run --frozen

# Line-coverage gate for analysis packages (design §1.4). A starting value, kept in config.
COV_MIN ?= 80
COV_GATED := services/detectors services/attributor packages/faults
# Go packages under the same gate, as MODULE_DIR:PACKAGES.
GO_COV_GATED := collector:./processor/normalizer/...

# Base revision of the "no breaking change" contract test. CI sets it to origin/<base branch>;
# locally the default is origin/main, fetched first (a stale local main gives false results).
CONTRACT_BASE_REF ?= origin/main
export CONTRACT_BASE_REF

KIND_CLUSTER := traceblame
# The agentic-rag checkout that make fixtures-agentic-rag runs (#12).
APP_DIR ?= $(CURDIR)/../agentic-rag
# The runbook-agent checkout that make fixtures-runbook-agent runs (#13).
RUNBOOK_APP_DIR ?= $(CURDIR)/../runbook-agent

# The kind cluster is shared between sessions: the fault drivers' phases that send traffic to
# kind or redeploy into it run under this lock, and only those (#14).
KIND_LOCK ?= /tmp/traceblame-kind.lock
# The fault validation run's id (tools/faults/): default faults-<date>-s<seed>-<commit7>.
RUN_ID ?=

# Collector distribution (collector/builder-config.yaml, ocb from tools/versions.env).
COLLECTOR_BUILD := collector/_build
OTELCOL_BIN ?= $(CURDIR)/$(COLLECTOR_BUILD)/otelcol/otelcol-traceblame
ASSEMBLER_BIN ?= $(CURDIR)/services/assembler/_build/assembler
# The normalizer as a CLI (collector/cmd/normalize): the contract stage runs fixtures through it.
export TRACEBLAME_NORMALIZE_BIN := $(CURDIR)/$(COLLECTOR_BUILD)/bin/traceblame-normalize

.PHONY: help bootstrap lint test contract integration security attribution fixtures fixtures-agentic-rag \
	fixtures-runbook-agent faults-agentic-rag faults-runbook-agent faults-report faults-validate \
	kind-up kind-down kind-deploy kind-runbook-up kind-runbook-down \
	kind-clickhouse-persistence bench-scaling assembler collector collector-smoke collector-image

help: ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{ printf "  %-16s %s\n", $$1, $$2 }'

bootstrap: ## Install pinned tools, sync Python and Go deps, pre-commit envs and the pre-push hook
	@bash scripts/bootstrap.sh

lint: ## Run every linter over all tracked files (pre-commit)
	$(UV_RUN) pre-commit run --all-files --show-diff-on-failure

test: ## Unit tests: pytest with coverage gates, go test -race, vitest (sdk/ts)
	$(UV_RUN) pytest -m "not contract and not integration" --cov --cov-report=term
	@for pkg in $(COV_GATED); do \
	  echo "coverage gate: $$pkg >= $(COV_MIN)%"; \
	  $(UV_RUN) coverage report --include="$$pkg/src/*" --fail-under=$(COV_MIN); \
	done
	bash scripts/go-each-module.sh go test -race -count=1 ./...
	@for gate in $(GO_COV_GATED); do \
	  bash scripts/go-coverage-gate.sh "$${gate%%:*}" "$${gate#*:}" $(COV_MIN); \
	done
	COV_MIN=$(COV_MIN) pnpm -C sdk/ts -s test
	node --test packages/faults/preload/faults-preload.test.mjs

contract: ## Contract tests: profile/schema sync, semconv pin, fixtures through the normalizer; gap report in .contract/
	@if [ "$(origin CONTRACT_BASE_REF)" = file ]; then \
	  git fetch origin main --quiet || { echo "cannot fetch origin main; set CONTRACT_BASE_REF to override" >&2; exit 1; }; \
	fi
	cd collector && go build -o "$(TRACEBLAME_NORMALIZE_BIN)" ./cmd/normalize
	$(UV_RUN) traceblame-contracts gen-schema --check
	$(UV_RUN) traceblame-contracts gap-report
	pnpm -C sdk/ts -s build
	$(UV_RUN) pytest -m contract -rxX

integration: collector assembler ## Integration: ClickHouse, Postgres and NATS in test containers, the built collector and assembler (needs Docker)
	OTELCOL_BIN="$(OTELCOL_BIN)" ASSEMBLER_BIN="$(ASSEMBLER_BIN)" $(UV_RUN) pytest -m integration -rs

security: ## gitleaks (full history), Syft SBOM, Grype gate; digest-pinned images (needs Docker)
	@bash scripts/security.sh

assembler: ## Build the trace-assembler (services/assembler/_build/assembler)
	mkdir -p $(dir $(ASSEMBLER_BIN))
	cd services/assembler && GOWORK=off CGO_ENABLED=0 go build -trimpath -o "$(ASSEMBLER_BIN)" ./cmd/assembler

collector: ## Build the collector distribution with ocb (collector/_build/otelcol)
	mkdir -p $(COLLECTOR_BUILD)
	cd collector && GOWORK=off CGO_ENABLED=0 ocb --config builder-config.yaml

collector-smoke: ## End-to-end tests of a built collector (OTELCOL_BIN=..., default: make collector)
	@test -x "$(OTELCOL_BIN)" || { echo "no collector at $(OTELCOL_BIN): run make collector" >&2; exit 1; }
	cd collector && OTELCOL_BIN="$(OTELCOL_BIN)" go test -race -count=1 ./test/smoke/

collector-image: ## Build and smoke-test the collector image, SBOM + Grype gate (needs Docker)
	@bash scripts/collector-image.sh

fixtures: ## Re-capture golden span fixtures from Ollama (OLLAMA_URL=...; see collector/testdata/fixtures)
	bash scripts/install-tools.sh otelcol-contrib
	uv run --locked --script tools/fixtures/capture.py $(if $(OLLAMA_URL),--ollama-url $(OLLAMA_URL))

fixtures-agentic-rag: ## Capture agentic-rag fixtures through kind (APP_DIR, OLLAMA_URL; needs kind-up, kind-deploy)
	bash scripts/install-tools.sh otelcol-contrib
	uv run --locked --script tools/fixtures/capture_agentic_rag.py --app-dir "$(APP_DIR)" $(if $(OLLAMA_URL),--ollama-url $(OLLAMA_URL))

fixtures-runbook-agent: ## Capture runbook-agent fixtures in the kind sandbox (RUNBOOK_APP_DIR, OLLAMA_URL; needs kind-runbook-up)
	uv run --locked --script tools/fixtures/capture_runbook_agent.py --app-dir "$(RUNBOOK_APP_DIR)" $(if $(OLLAMA_URL),--ollama-url $(OLLAMA_URL))

faults-agentic-rag: ## Faults into real agentic-rag runs through kind (#14; APP_DIR, OLLAMA_URL, RUN_ID)
	bash scripts/install-tools.sh otelcol-contrib
	@run_id="$$($(UV_RUN) python tools/faults/run_agentic_rag.py prepare --app-dir "$(APP_DIR)" \
	  $(if $(OLLAMA_URL),--ollama-url $(OLLAMA_URL)) $(if $(RUN_ID),--run-id $(RUN_ID)))"; \
	for phase in run record check; do \
	  flock "$(KIND_LOCK)" $(UV_RUN) python tools/faults/run_agentic_rag.py "$$phase" --run-id "$$run_id"; \
	done; echo "faults-agentic-rag: $$run_id"

faults-runbook-agent: ## Faults into real runbook-agent runs in the sandbox (#14; RUNBOOK_APP_DIR, RUN_ID)
	@run_id="$$($(UV_RUN) python tools/faults/run_runbook_agent.py build --app-dir "$(RUNBOOK_APP_DIR)" \
	  $(if $(RUN_ID),--run-id $(RUN_ID)))"; \
	flock "$(KIND_LOCK)" $(UV_RUN) python tools/faults/run_runbook_agent.py run --run-id "$$run_id" \
	  $(if $(OLLAMA_URL),--ollama-url $(OLLAMA_URL)); \
	for phase in record check; do \
	  flock "$(KIND_LOCK)" $(UV_RUN) python tools/faults/run_runbook_agent.py "$$phase" --run-id "$$run_id"; \
	done; echo "faults-runbook-agent: $$run_id"

faults-report: ## The fault validation report over both apps (RUN_ID=...) into evals/reports/
	@test -n "$(RUN_ID)" || { echo "faults-report: set RUN_ID" >&2; exit 1; }
	$(UV_RUN) python tools/faults/report.py --run-id "$(RUN_ID)"

faults-validate: faults-agentic-rag faults-runbook-agent ## Both fault runs, then the report (#14)
	@$(MAKE) --no-print-directory faults-report \
	  RUN_ID="$${RUN_ID:-faults-$$(date -u +%Y%m%d)-s7-$$(git rev-parse --short=7 HEAD)}"

attribution: ## No AI attribution in the commits of this branch (origin/main..HEAD); the pre-push hook and CI run the same script
	@bash scripts/attribution-check.sh origin/main..HEAD

kind-up: ## Create the local kind cluster, namespaces and generated Secrets (needs Docker in WSL)
	@bash scripts/kind-up.sh

kind-deploy: ## Deploy ClickHouse, the migrations and the agent/gateway tiers into kind (after kind-up)
	@bash scripts/kind-deploy.sh

bench-scaling: ## Gateway 2->4->2 span loss and split traces; per-stage lag with STAMPS=1, PROFILE=stress|low|steady (evals/reports/)
	@bash bench/gateway-scaling/run.sh

kind-down: ## Delete the local kind cluster
	kind delete cluster --name $(KIND_CLUSTER)

kind-clickhouse-persistence: ## ClickHouse keeps a marker across a CHI pod deletion (MODE=pod|write|verify, TOKEN for verify)
	@bash scripts/kind-clickhouse-persistence.sh $(or $(MODE),pod) $(TOKEN)

kind-runbook-up: ## Runbook-agent sandbox: namespace-only RBAC, network policies, monitoring (needs kind-deploy)
	bash scripts/kind-runbook.sh up

kind-runbook-down: ## Delete the runbook-agent sandbox and its monitoring
	bash scripts/kind-runbook.sh down
