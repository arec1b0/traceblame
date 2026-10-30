from importlib.metadata import version

import traceblame_attributor


def test_version_comes_from_installed_distribution() -> None:
    assert traceblame_attributor.__version__ == version("traceblame-attributor")
