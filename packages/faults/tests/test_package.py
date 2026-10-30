from importlib.metadata import version

import traceblame_faults


def test_version_comes_from_installed_distribution() -> None:
    assert traceblame_faults.__version__ == version("traceblame-faults")
