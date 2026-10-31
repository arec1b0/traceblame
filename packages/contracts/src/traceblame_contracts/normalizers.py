"""The seam between golden fixtures and the profile schema in contract tests.

- `go`: the collector's normalizer processor, run through collector/cmd/normalize
  (`traceblame-normalize`, built by `make contract`). Contract tests gate on it.
- `identity`: the raw SDK export, unchanged. The gap report uses it to show what each SDK
  lacks before normalization.
"""

import copy
import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

DEFAULT_GO_BINARY = Path("collector/_build/bin/traceblame-normalize")
DEFAULT_GO_CONFIG = Path("collector/testdata/normalizer-contract.yaml")
BINARY_ENV = "TRACEBLAME_NORMALIZE_BIN"
CONFIG_ENV = "TRACEBLAME_NORMALIZE_CONFIG"


class NormalizerUnavailableError(RuntimeError):
    pass


class Normalizer(Protocol):
    name: str

    def normalize(self, otlp: Mapping[str, Any]) -> dict[str, Any]:
        """Map one OTLP/JSON export to profile-conformant OTLP/JSON."""
        ...


class IdentityNormalizer:
    name = "identity"

    def normalize(self, otlp: Mapping[str, Any]) -> dict[str, Any]:
        return copy.deepcopy(dict(otlp))


class GoNormalizer:
    """The normalizer processor of the collector distribution, as a subprocess.

    The binary and its settings come from the arguments, else from TRACEBLAME_NORMALIZE_BIN
    and TRACEBLAME_NORMALIZE_CONFIG, else from the defaults relative to the working directory
    (the repository root under `make contract`).
    """

    name = "go"

    def __init__(self, binary: Path | None = None, config: Path | None = None) -> None:
        self._binary = binary
        self._config = config

    @property
    def binary(self) -> Path:
        return self._binary or Path(os.environ.get(BINARY_ENV, DEFAULT_GO_BINARY))

    @property
    def config(self) -> Path:
        return self._config or Path(os.environ.get(CONFIG_ENV, DEFAULT_GO_CONFIG))

    def normalize(self, otlp: Mapping[str, Any]) -> dict[str, Any]:
        binary, config = self.binary, self.config
        if not binary.is_file():
            raise NormalizerUnavailableError(f"{binary} does not exist: run `make contract`")
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [str(binary), "--config", str(config)],
            input=json.dumps(otlp),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"{binary.name} failed: {result.stderr.strip()}")
        out: dict[str, Any] = json.loads(result.stdout)
        return out


NORMALIZERS: tuple[Normalizer, ...] = (IdentityNormalizer(), GoNormalizer())
