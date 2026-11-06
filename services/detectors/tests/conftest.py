"""Shared fixtures: the test-only reference detectors and configs over them."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from traceblame_detectors.api import Detector
from traceblame_detectors.config import DetectorConfig, load_config
from traceblame_detectors.testing import load_detector

PLUGINS = Path(__file__).resolve().parent / "plugins"
NAMES = ("reference_root", "reference_golden", "reference_baseline")


@pytest.fixture
def registry() -> dict[str, type[Detector]]:
    return {n: load_detector(PLUGINS / n / "detector.py") for n in NAMES}


@pytest.fixture
def make_config(registry: dict[str, type[Detector]]) -> Callable[..., DetectorConfig]:
    def make(mode: str = "production", **detectors: Any) -> DetectorConfig:
        return load_config({"mode": mode, "detectors": detectors}, registry)

    return make
