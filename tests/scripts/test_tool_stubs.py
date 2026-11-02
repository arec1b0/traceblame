"""tools/typings: what each stub mirrors, and that its pins are the scripts' own (#49).

The stubs stand in, for pyright only, for what exists only in the capture scripts' own uv
environments (their PEP 723 dependencies) or in the agentic-rag checkout. They are as true as
the day they were written, so each says what it mirrors, and a stub of a pinned package must
carry the pin of every script that imports it: bumping a script's pin then fails here until the
stub has been checked against the new release.
"""

import re
import tomllib
from pathlib import Path
from typing import cast

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
TYPINGS = ROOT / "tools" / "typings"
SCRIPTS = sorted((ROOT / "tools" / "fixtures").rglob("*.py"))
STUBS = sorted(TYPINGS.rglob("*.pyi"))
FIXTURES = ROOT / "collector" / "testdata" / "fixtures"
APP_STUB_SOURCE = re.compile(r"^# source: arec1b0/agentic-rag at ([0-9a-f]{7,40})\b")

PEP723 = re.compile(r"(?m)^# /// script\n((?:#.*\n)+?)# ///$")


def _header(stub: Path) -> list[str]:
    lines: list[str] = []
    for line in stub.read_text(encoding="utf-8").splitlines():
        if not line.startswith("#"):
            break
        lines.append(line)
    return lines


def _module(stub: Path) -> str:
    """The dotted name of the package a stub stands for."""
    return ".".join(stub.parent.relative_to(TYPINGS).parts)


def _dependencies(script: Path) -> list[str]:
    found = PEP723.search(script.read_text(encoding="utf-8"))
    if found is None:
        return []
    body = "\n".join(
        line.removeprefix("# ").removeprefix("#") for line in found.group(1).splitlines()
    )
    return cast("list[str]", tomllib.loads(body).get("dependencies", []))


def _importers(module: str) -> list[Path]:
    pattern = re.compile(
        rf"(?m)^\s*(?:from {re.escape(module)}(?:\.|\s+import)|import {re.escape(module)}\b)"
    )
    return [s for s in SCRIPTS if pattern.search(s.read_text(encoding="utf-8"))]


def test_there_are_stubs() -> None:
    assert STUBS, "tools/typings is empty; pyright's extraPaths for tools/ would point nowhere"


@pytest.mark.parametrize("stub", STUBS, ids=lambda p: str(p.relative_to(TYPINGS)))
def test_a_stub_names_what_it_mirrors(stub: Path) -> None:
    header = _header(stub)
    assert any(line.startswith("# source:") for line in header), f"{stub.name} has no source"


@pytest.mark.parametrize("stub", STUBS, ids=lambda p: str(p.relative_to(TYPINGS)))
def test_a_stub_is_a_package_stub(stub: Path) -> None:
    # pyright reports a file-form stub without its source as reportMissingModuleSource in
    # strict mode, and a package-form one not.
    assert stub.name == "__init__.pyi"


def test_a_pinned_stub_matches_the_pins_of_the_scripts_that_import_it() -> None:
    pinned: dict[Path, list[str]] = {}
    for stub in STUBS:
        pins = [
            line.removeprefix("# pin:").strip()
            for line in _header(stub)
            if line.startswith("# pin:")
        ]
        if pins:
            pinned[stub] = pins
    assert pinned, "no stub carries a pin"
    for stub, pins in pinned.items():
        importers = _importers(_module(stub))
        assert importers, f"no script imports {_module(stub)}"
        for script in importers:
            deps = _dependencies(script)
            for pin in pins:
                assert re.fullmatch(r"[A-Za-z0-9._-]+==[A-Za-z0-9.!+_-]+", pin), pin
                assert pin in deps, (
                    f"{stub.relative_to(ROOT)} mirrors {pin}; {script.name} pins {deps}"
                )


def test_the_app_stubs_are_at_the_commit_of_the_agentic_rag_fixtures() -> None:
    """A re-capture on a newer app commit fails here until the src.* stubs are re-checked."""
    stubs = [s for s in STUBS if s.relative_to(TYPINGS).parts[0] == "src"]
    assert stubs, "no stub for the agentic-rag app modules"
    recorded: set[str] = set()
    for path in sorted(FIXTURES.rglob("agentic_rag_cuad_*.provenance.yaml")):
        doc = cast("dict[str, dict[str, str]]", yaml.safe_load(path.read_text(encoding="utf-8")))
        recorded.add(doc["app"]["commit"])
    assert recorded, "no agentic_rag_cuad_* provenance records an app commit"
    assert len(recorded) == 1, f"the fixtures were captured on several app commits: {recorded}"
    (commit,) = recorded
    for stub in stubs:
        found = next(
            (m for line in _header(stub) if (m := APP_STUB_SOURCE.match(line))),
            None,
        )
        assert found, (
            f"{stub.relative_to(TYPINGS)} header has no `# source: arec1b0/agentic-rag at <sha>`"
        )
        assert commit.startswith(found.group(1)), (
            f"{stub.relative_to(ROOT)} mirrors agentic-rag {found.group(1)}; the fixtures "
            f"record {commit}: re-check the stub against that commit, then update its header"
        )


def test_the_reader_finds_the_scripts_pins() -> None:
    deps = _dependencies(ROOT / "tools" / "fixtures" / "capture_openllmetry.py")
    assert "opentelemetry-instrumentation-openai==0.62.4" in deps
    assert _dependencies(ROOT / "tools" / "fixtures" / "kindlib.py") == []
