from importlib.metadata import version

import traceblame_py


def test_version_comes_from_installed_distribution() -> None:
    assert traceblame_py.__version__ == version("traceblame-py")
