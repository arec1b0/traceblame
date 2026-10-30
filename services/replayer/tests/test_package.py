from importlib.metadata import version

import traceblame_replayer


def test_version_comes_from_installed_distribution() -> None:
    assert traceblame_replayer.__version__ == version("traceblame-replayer")
