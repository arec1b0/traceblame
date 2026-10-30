from importlib.metadata import version

import traceblame_detectors


def test_version_comes_from_installed_distribution() -> None:
    assert traceblame_detectors.__version__ == version("traceblame-detectors")
