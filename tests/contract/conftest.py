import json
from pathlib import Path
from typing import Any

import pytest

from traceblame_contracts.cli import schema_path_for
from traceblame_contracts.profile import Profile, load_profile
from traceblame_contracts.semconv import Snapshot, load_snapshot

ROOT = Path(__file__).resolve().parents[2]
PROFILE_PATH = ROOT / "schemas" / "profile" / "aegis-telemetry.v1.yaml"


@pytest.fixture(scope="session")
def profile() -> Profile:
    return load_profile(PROFILE_PATH)


@pytest.fixture(scope="session")
def committed_schema() -> dict[str, Any]:
    return json.loads(schema_path_for(PROFILE_PATH).read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def snapshot(profile: Profile) -> Snapshot:
    return load_snapshot(ROOT / "schemas" / "semconv" / f"v{profile.semconv.version}")
