"""The detectors that ship, by name (#15: none yet; #16 adds the MVP set).

An explicit allowlist, not entry points: only code in this package can become a detector.
Each plugin lives in `plugins/<name>/detector.py` with its tests next to it, imports only from
traceblame_detectors.api and the standard modules the static guard allows
(tests/test_static_guard.py), and is listed in versions.lock.json.
"""

from traceblame_detectors.api import Detector

REGISTRY: dict[str, type[Detector]] = {}
