from importlib.metadata import version

import traceblame_api


def test_version_comes_from_installed_distribution() -> None:
    assert traceblame_api.__version__ == version("traceblame-api")
