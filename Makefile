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

KIND_CLUSTER := traceblame

.PHONY: help bootstrap lint test contract security fixtures kind-up kind-down

help: ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{ printf "  %-10s %s\n", $$1, $$2 }'

bootstrap: ## Install pinned tools, sync Python and Go deps, prepare pre-commit hooks
	@bash scripts/bootstrap.sh

lint: ## Run every linter over all tracked files (pre-commit)
	$(UV_RUN) pre-commit run --all-files --show-diff-on-failure

test: ## Unit tests: pytest with coverage gates, go test -race
	$(UV_RUN) pytest -m "not contract" --cov --cov-report=term
	@for pkg in $(COV_GATED); do \
	  echo "coverage gate: $$pkg >= $(COV_MIN)%"; \
	  $(UV_RUN) coverage report --include="$$pkg/src/*" --fail-under=$(COV_MIN); \
	done
	bash scripts/go-each-module.sh go test -race ./...

contract: ## Contract tests: profile <-> JSON Schema sync, semconv pin, golden fixtures
	$(UV_RUN) traceblame-contracts gen-schema --check
	$(UV_RUN) pytest -m contract -rxX

security: ## gitleaks (full history), Syft SBOM, Grype gate; digest-pinned images (needs Docker)
	@bash scripts/security.sh

fixtures: ## Re-capture golden span fixtures from Ollama (OLLAMA_URL=...; see collector/testdata/fixtures)
	bash scripts/install-tools.sh otelcol-contrib
	uv run --locked --script tools/fixtures/capture.py $(if $(OLLAMA_URL),--ollama-url $(OLLAMA_URL))

kind-up: ## Create the local kind cluster (needs Docker in WSL)
	@bash scripts/kind-up.sh

kind-down: ## Delete the local kind cluster
	kind delete cluster --name $(KIND_CLUSTER)
