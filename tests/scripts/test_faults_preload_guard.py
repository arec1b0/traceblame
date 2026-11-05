"""The fault preload exists only in the eval image (#14): no non-eval manifest and no capture
path sets NODE_OPTIONS or names the preload; the eval image's Dockerfile does. The driver also
checks the built images (run_runbook_agent.image_guard)."""

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EVAL_ONLY = re.compile(r"NODE_OPTIONS|faults-preload|traceblame-faults")
# Where the preload may be named: the faults package and its eval driver, docs about them.
ALLOWED = (
    ROOT / "packages" / "faults",
    ROOT / "tools" / "faults",
    ROOT / "tests" / "scripts" / "test_faults_preload_guard.py",
)


def _scanned() -> list[Path]:
    files = [*(ROOT / "deploy").rglob("*"), *(ROOT / "tools" / "fixtures").rglob("*")]
    files += [*(ROOT / "scripts").rglob("*"), ROOT / "Makefile"]
    return [
        f
        for f in files
        if f.is_file()
        and "__pycache__" not in f.parts
        and f.suffix not in (".json", ".lock")
        and not any(f.is_relative_to(a) for a in ALLOWED)
    ]


def test_there_is_something_to_scan() -> None:
    names = {f.name for f in _scanned()}
    assert "agent.yaml" in names
    assert "capture_runbook_agent.py" in names


@pytest.mark.parametrize("path", _scanned(), ids=lambda p: str(p.relative_to(ROOT)))
def test_no_non_eval_file_sets_node_options_or_names_the_preload(path: Path) -> None:
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.name == "Makefile":
        # The Makefile's faults targets run the eval driver, and make test the preload's unit
        # tests; nothing else may name the preload.
        text = "\n".join(
            line
            for line in text.splitlines()
            if "tools/faults/" not in line and "packages/faults/" not in line
        )
    assert not EVAL_ONLY.search(text), f"{path.relative_to(ROOT)} names an eval-only setting"


def test_the_eval_dockerfile_adds_the_preload_and_node_options_only() -> None:
    sys.path.insert(0, str(ROOT / "tools" / "faults"))
    import run_runbook_agent

    text = run_runbook_agent.eval_dockerfile("runbook-agent:abc-tb123")
    lines = text.splitlines()
    assert lines[0] == "FROM runbook-agent:abc-tb123"
    assert lines[1].startswith("COPY faults-preload.mjs /opt/traceblame-faults/")
    assert lines[2] == "ENV NODE_OPTIONS=--import=/opt/traceblame-faults/faults-preload.mjs"
    assert len(lines) == 3
