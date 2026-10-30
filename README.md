# TraceBlame

TraceBlame turns OpenTelemetry GenAI traces into a verdict on who is to blame:
`retrieval | prompt | tool | model | orchestration | unknown`, with confidence and
evidence. Ground truth comes from its own fault injector. It is part of Aegis Platform.

- Design: [docs/design.md](docs/design.md)
- Decisions: [docs/adr/](docs/adr/README.md)
- Project rules: [CLAUDE.md](CLAUDE.md)

## Development environment

All `make` targets run on Linux x86_64. On Windows, run them from WSL2 (Ubuntu) in the
checkout under `/mnt/c/...`.

Prerequisites in WSL: `bash`, `curl`, `git`, `make`, `gcc` (for `go test -race`).
`make kind-up` also needs Docker: in Docker Desktop, go to Settings → Resources →
WSL Integration and enable your distro.

Everything else is pinned and installed into `.tools/` by `make bootstrap`. Tool versions
live in [tools/versions.env](tools/versions.env), and their sha256 checksums in
[tools/checksums.txt](tools/checksums.txt). Python tools are pinned in `pyproject.toml`
and `uv.lock`, and the Python version in `.python-version`.

```bash
make bootstrap   # pinned tools, uv workspace, Go modules, pre-commit hook environments
make lint        # pre-commit over all tracked files: ruff, pyright, golangci-lint, hadolint, gitleaks, actionlint
make test        # pytest with coverage gates, go test -race
make contract    # contract tests (from #3)
make kind-up     # local kind cluster "traceblame"; make kind-down deletes it
```

Notes:
- Do not run `uv` from Windows in this checkout. `.venv` is a Linux environment, and the
  pinned `required-version` rejects other uv versions.
- `pre-commit` checks tracked files only, so stage new files before `make lint`.
- No git hook is installed. Run `make lint` before committing.

## Layout

```
services/{api,detectors,attributor,replayer}   Python services (uv workspace members)
services/assembler                             Go: trace-assembler
packages/faults                                Python: fault injection library
sdk/python                                     Python shim (traceblame-py)
collector/                                     Go: OTel Collector distribution and normalizer
deploy/kind/                                   local kind cluster config
docs/adr/                                      architecture decision records
tools/, scripts/                               pinned tool versions and setup scripts
```
