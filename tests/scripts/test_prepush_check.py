"""scripts/prepush-check.sh: the pre-push PR-state rule, with gh stubbed.

Never push to a branch whose PR is merged or closed: the hook passes only when the branch
has no PR or its only PR is OPEN, and fails closed when gh cannot tell.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "prepush-check.sh"

GH_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$GH_STUB_CALLS"
printf '%s' "$GH_STUB_OUTPUT"
exit "${GH_STUB_EXIT:-0}"
"""


def _run(
    tmp_path: Path,
    prs: object,
    *args: str,
    gh_exit: int = 0,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(GH_STUB, encoding="utf-8")
    gh.chmod(0o755)
    environ = {k: v for k, v in os.environ.items() if not k.startswith("PRE_COMMIT_")}
    environ.update(
        PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        GH_STUB_OUTPUT=prs if isinstance(prs, str) else json.dumps(prs),
        GH_STUB_EXIT=str(gh_exit),
        GH_STUB_CALLS=str(tmp_path / "calls"),
    )
    environ.update(env or {})
    return subprocess.run(  # noqa: S603 - the script under test
        ["bash", str(SCRIPT), *args],  # noqa: S607
        env=environ,
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
    )


def _calls(tmp_path: Path) -> list[str]:
    path = tmp_path / "calls"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


@pytest.mark.parametrize("state", ["MERGED", "CLOSED"])
def test_a_merged_or_closed_pr_blocks_the_push(tmp_path: Path, state: str) -> None:
    result = _run(tmp_path, [{"number": 33, "state": state}], "feat/11-clickhouse-migrations")
    assert result.returncode == 1
    assert f"#33 {state}" in result.stderr
    assert "branch from origin/main" in result.stderr
    assert _calls(tmp_path) == [
        "pr list --head feat/11-clickhouse-migrations --state all --json state,number"
    ]


def test_an_open_pr_next_to_a_closed_one_blocks_the_push(tmp_path: Path) -> None:
    prs = [{"number": 40, "state": "OPEN"}, {"number": 12, "state": "CLOSED"}]
    result = _run(tmp_path, prs, "feat/12-x")
    assert result.returncode == 1
    assert "#40 OPEN, #12 CLOSED" in result.stderr


@pytest.mark.parametrize(
    "prs", [[], [{"number": 39, "state": "OPEN"}]], ids=["no PR", "one open PR"]
)
def test_no_pr_or_one_open_pr_lets_the_push_through(tmp_path: Path, prs: object) -> None:
    result = _run(tmp_path, prs, "feat/39-trace-arrivals")
    assert result.returncode == 0, result.stderr
    assert "feat/39-trace-arrivals" in result.stdout


def test_it_fails_closed_when_gh_cannot_tell(tmp_path: Path) -> None:
    result = _run(tmp_path, "HTTP 401: Bad credentials", "feat/39-x", gh_exit=1)
    assert result.returncode == 1
    assert "refusing to push" in result.stderr
    result = _run(tmp_path, "not json", "feat/39-x")
    assert result.returncode == 1
    assert "refusing to push" in result.stderr


def test_the_pushed_remote_branch_comes_from_pre_commit(tmp_path: Path) -> None:
    """In the hook, pre-commit names the remote ref; it wins over the argument."""
    env = {"PRE_COMMIT_REMOTE_BRANCH": "refs/heads/feat/10-two-tier-topology"}
    result = _run(tmp_path, [{"number": 34, "state": "MERGED"}], "ignored", env=env)
    assert result.returncode == 1
    assert _calls(tmp_path) == [
        "pr list --head feat/10-two-tier-topology --state all --json state,number"
    ]


def test_a_tag_push_is_not_a_branch(tmp_path: Path) -> None:
    result = _run(tmp_path, [], env={"PRE_COMMIT_REMOTE_BRANCH": "refs/tags/v0.1.0"})
    assert result.returncode == 0
    assert _calls(tmp_path) == []
