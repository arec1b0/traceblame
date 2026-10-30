from pathlib import Path

import pytest

from traceblame_contracts.semconv import SnapshotError, load_snapshot

_REGISTRY = """\
groups:
  - id: registry.example
    type: attribute_group
    attributes:
      - id: example.name
        type: string
        stability: development
      - id: example.count
        type: int
        stability: stable
      - id: example.reasons
        type: string[]
        stability: development
      - id: example.operation
        stability: development
        type:
          members:
            - id: chat
              value: "chat"
            - id: embeddings
              value: "embeddings"
      - id: example.code
        stability: development
        type:
          members:
            - id: ok
              value: 0
  - id: registry.example.deprecated
    type: attribute_group
    attributes:
      - id: example.old
        type: string
        stability: development
        deprecated:
          reason: renamed
          renamed_to: example.name
  - id: span.example
    type: span
    attributes:
      - ref: example.name
"""

_SOURCE = """\
repository: open-telemetry/semantic-conventions
version: 1.41.1
tag: v1.41.1
commit: ead83b9b0fa36540c1642fce46e874f002ac23f1
schema_url: https://opentelemetry.io/schemas/1.41.1
license: Apache-2.0 (see LICENSE)
"""


@pytest.fixture
def snapshot(tmp_path: Path) -> Path:
    (tmp_path / "model" / "example").mkdir(parents=True)
    (tmp_path / "model" / "example" / "registry.yaml").write_text(_REGISTRY, "utf-8")
    (tmp_path / "source.yaml").write_text(_SOURCE, "utf-8")
    return tmp_path


def test_loads_source_and_attributes(snapshot: Path) -> None:
    snap = load_snapshot(snapshot)
    assert snap.source.version == "1.41.1"
    assert snap.source.commit == "ead83b9b0fa36540c1642fce46e874f002ac23f1"
    assert set(snap.attributes) == {
        "example.name",
        "example.count",
        "example.reasons",
        "example.operation",
        "example.code",
        "example.old",
    }


@pytest.mark.parametrize(
    ("name", "type_"),
    [
        ("example.name", "string"),
        ("example.count", "int"),
        ("example.reasons", "string[]"),
        ("example.operation", "string"),
        ("example.code", "int"),
    ],
)
def test_attribute_types(snapshot: Path, name: str, type_: str) -> None:
    assert load_snapshot(snapshot).attributes[name].type == type_


def test_enum_members_and_deprecation(snapshot: Path) -> None:
    attrs = load_snapshot(snapshot).attributes
    assert attrs["example.operation"].members == ("chat", "embeddings")
    assert attrs["example.old"].deprecated
    assert not attrs["example.name"].deprecated
    assert attrs["example.count"].stability == "stable"


def test_checksum_verification(snapshot: Path) -> None:
    snap = load_snapshot(snapshot)
    assert snap.checksum_mismatches() == ["SHA256SUMS is missing"]


def test_duplicate_definitions_are_an_error(snapshot: Path) -> None:
    (snapshot / "model" / "copy.yaml").write_text(_REGISTRY, "utf-8")
    with pytest.raises(SnapshotError, match=r"example\.name"):
        load_snapshot(snapshot)
