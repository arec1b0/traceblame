import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from traceblame_contracts.normalizers import (
    NORMALIZERS,
    GoNormalizer,
    IdentityNormalizer,
    NormalizerUnavailableError,
)


def test_identity_returns_an_equal_copy() -> None:
    doc: dict[str, Any] = {"resourceSpans": [{"scopeSpans": []}]}
    out = IdentityNormalizer().normalize(doc)
    assert out == doc
    out["resourceSpans"].clear()
    assert doc == {"resourceSpans": [{"scopeSpans": []}]}


def test_registry_names_are_unique() -> None:
    names = [n.name for n in NORMALIZERS]
    assert len(names) == len(set(names))
    assert {"identity", "go"} <= set(names)


def _fake_binary(tmp_path: Path, body: str) -> Path:
    """A stand-in for traceblame-normalize: a Python script with the same CLI."""
    path = tmp_path / "traceblame-normalize"
    path.write_text(f"#!{sys.executable}\nimport json, sys\n{body}\n", "utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_go_normalizer_pipes_the_export_and_passes_the_config(tmp_path: Path) -> None:
    binary = _fake_binary(
        tmp_path,
        "doc = json.load(sys.stdin)\ndoc['argv'] = sys.argv[1:]\njson.dump(doc, sys.stdout)",
    )
    config = tmp_path / "normalizer.yaml"
    out = GoNormalizer(binary, config).normalize({"resourceSpans": []})
    assert out == {"resourceSpans": [], "argv": ["--config", str(config)]}


def test_go_normalizer_reads_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = _fake_binary(tmp_path, "json.dump({'ok': True}, sys.stdout)")
    monkeypatch.setenv("TRACEBLAME_NORMALIZE_BIN", str(binary))
    monkeypatch.setenv("TRACEBLAME_NORMALIZE_CONFIG", str(tmp_path / "c.yaml"))
    normalizer = GoNormalizer()
    assert normalizer.binary == binary
    assert normalizer.config == tmp_path / "c.yaml"
    assert normalizer.normalize({}) == {"ok": True}


def test_go_normalizer_without_a_binary(tmp_path: Path) -> None:
    with pytest.raises(NormalizerUnavailableError, match="make contract"):
        GoNormalizer(tmp_path / "missing").normalize({})


def test_go_normalizer_reports_failures(tmp_path: Path) -> None:
    binary = _fake_binary(tmp_path, "sys.stderr.write('bad export'); sys.exit(1)")
    with pytest.raises(RuntimeError, match="bad export"):
        GoNormalizer(binary).normalize({})
