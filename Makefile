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
COV_GATED := services/detectors services/attributor
# Go packages under the same gate, as MODULE_DIR:PACKAGES.
GO_COV_GATED := collector:./processor/normalizer/...

# Base revision of the "no breaking change" contract test. CI sets it to origin/<base branch>;
# locally the default is origin/main, fetched first (a stale local main gives false results).
CONTRACT_BASE_REF ?= origin/main
export CONTRACT_BASE_REF

KIND_CLUSTER := traceblame

# Collector distribution (collector/builder-config.yaml, ocb from tools/versions.env).
COLLECTOR_BUILD := collector/_build
OTELCOL_BIN ?= $(CURDIR)/$(COLLECTOR_BUILD)/otelcol/otelcol-traceblame
# The normalizer as a CLI (collector/cmd/normalize): the contract stage runs fixtures through it.
export TRACEBLAME_NORMALIZE_BIN := $(CURDIR)/$(COLLECTOR_BUILD)/bin/traceblame-normalize

.PHONY: help bootstrap lint test contract integration security fixtures kind-up kind-down \
	kind-deploy bench-scaling collector collector-smoke collector-image

help: ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{ printf "  %-16s %s\n", $$1, $$2 }'

bootstrap: ## Install pinned tools, sync Python and Go deps, prepare pre-commit hooks
	@bash scripts/bootstrap.sh

lint: ## Run every linter over all tracked files (pre-commit)
	$(UV_RUN) pre-commit run --all-files --show-diff-on-failure

test: ## Unit tests: pytest with coverage gates, go test -race
	$(UV_RUN) pytest -m "not contract and not integration" --cov --cov-report=term
	@for pkg in $(COV_GATED); do \
	  echo "coverage gate: $$pkg >= $(COV_MIN)%"; \
	  $(UV_RUN) coverage report --include="$$pkg/src/*" --fail-under=$(COV_MIN); \
	done
	bash scripts/go-each-module.sh go test -race -count=1 ./...
	@for gate in $(GO_COV_GATED); do \
	  bash scripts/go-coverage-gate.sh "$${gate%%:*}" "$${gate#*:}" $(COV_MIN); \
	done

contract: ## Contract tests: profile/schema sync, semconv pin, fixtures through the normalizer; gap report in .contract/
	@if [ "$(origin CONTRACT_BASE_REF)" = file ]; then \
	  git fetch origin main --quiet || { echo "cannot fetch origin main; set CONTRACT_BASE_REF to override" >&2; exit 1; }; \
	fi
	cd collector && go build -o "$(TRACEBLAME_NORMALIZE_BIN)" ./cmd/normalize
	$(UV_RUN) traceblame-contracts gen-schema --check
	$(UV_RUN) traceblame-contracts gap-report
	$(UV_RUN) pytest -m contract -rxX

integration: collector ## Integration: ClickHouse in a test container, migrations, the built collector (needs Docker)
	OTELCOL_BIN="$(OTELCOL_BIN)" $(UV_RUN) pytest -m integration -rs

security: ## gitleaks (full history), Syft SBOM, Grype gate; digest-pinned images (needs Docker)
	@bash scripts/security.sh

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

kind-up: ## Create the local kind cluster, namespaces and generated Secrets (needs Docker in WSL)
	@bash scripts/kind-up.sh

kind-deploy: ## Deploy ClickHouse, the migrations and the agent/gateway tiers into kind (after kind-up)
	@bash scripts/kind-deploy.sh

bench-scaling: ## Measure span loss and split traces while scaling the gateway 2->4->2 (evals/reports/)
	@bash bench/gateway-scaling/run.sh

kind-down: ## Delete the local kind cluster
	kind delete cluster --name $(KIND_CLUSTER)
