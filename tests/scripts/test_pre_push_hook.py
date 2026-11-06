"""scripts/pre-push-hook.sh: the pre-push hook finds pre-commit in the worktree that pushes.

.git/hooks is shared by every worktree, and `pre-commit install` bakes in the Python of the
worktree that last ran it; once that worktree is gone, every push fails. The hook installed by
bootstrap execs this script from the pushing worktree, and the script resolves pre-commit the
way make does: the worktree's .venv, then its pinned .tools/bin/uv, then PATH.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "pre-push-hook.sh"
BOOTSTRAP = ROOT / "scripts" / "bootstrap.sh"

# A stand-in that records how it was called and what it read on stdin.
RECORDER = """#!/usr/bin/env bash
{ printf '%s|' "$(basename "$0")" "$@"; printf '\\nstdin=%s\\n' "$(cat)"; } > "$HOOK_CALLS"
exit "${HOOK_EXIT:-0}"
"""


def _tree(tmp_path: Path, *, venv: bool = False, uv: bool = False) -> Path:
    top = tmp_path / "worktree"
    (top / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, top / "scripts" / "pre-push-hook.sh")
    for wanted, rel in ((venv, ".venv/bin/python"), (uv, ".tools/bin/uv")):
        if wanted:
            path = top / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(RECORDER, encoding="utf-8")
            path.chmod(0o755)
    return top


def _run(
    top: Path, tmp_path: Path, path_dirs: list[Path] | None = None, exit_code: int = 0
) -> tuple[subprocess.CompletedProcess[str], str]:
    calls = tmp_path / "calls.txt"
    base = ["/usr/bin", "/bin"]
    env = {
        "PATH": os.pathsep.join([*(str(d) for d in path_dirs or []), *base]),
        "HOOK_CALLS": str(calls),
        "HOOK_EXIT": str(exit_code),
        "HOME": str(tmp_path),
    }
    result = subprocess.run(  # noqa: S603 - the script under test
        ["bash", str(top / "scripts" / "pre-push-hook.sh"), "origin", "https://example.invalid/r"],  # noqa: S607
        input="refs/heads/b 1111 refs/heads/b 0000\n",
        capture_output=True,
        text=True,
        env=env,
        cwd=top,
        check=False,
    )
    return result, calls.read_text(encoding="utf-8") if calls.exists() else ""


def test_the_worktrees_venv_comes_first(tmp_path: Path) -> None:
    top = _tree(tmp_path, venv=True, uv=True)
    result, calls = _run(top, tmp_path)
    assert result.returncode == 0, result.stderr
    assert calls.startswith("python|-m|pre_commit|hook-impl|--config=.pre-commit-config.yaml|")
    assert "--hook-type=pre-push|" in calls
    assert "|--|origin|https://example.invalid/r|" in calls
    assert "stdin=refs/heads/b 1111 refs/heads/b 0000" in calls  # git's ref lines reach it


def test_without_a_venv_the_pinned_uv_runs_it_like_make(tmp_path: Path) -> None:
    top = _tree(tmp_path, uv=True)
    result, calls = _run(top, tmp_path)
    assert result.returncode == 0, result.stderr
    assert calls.startswith("uv|run|--frozen|pre-commit|hook-impl|")


def test_then_pre_commit_on_path(tmp_path: Path) -> None:
    top = _tree(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "pre-commit").write_text(RECORDER, encoding="utf-8")
    (bin_dir / "pre-commit").chmod(0o755)
    result, calls = _run(top, tmp_path, [bin_dir])
    assert result.returncode == 0, result.stderr
    assert calls.startswith("pre-commit|hook-impl|")


def test_nothing_found_fails_closed_with_the_fix(tmp_path: Path) -> None:
    result, calls = _run(_tree(tmp_path), tmp_path)
    assert result.returncode == 1
    assert calls == ""
    assert "make bootstrap" in result.stderr


def test_a_failing_hook_fails_the_push(tmp_path: Path) -> None:
    result, _ = _run(_tree(tmp_path, venv=True), tmp_path, exit_code=3)
    assert result.returncode == 3


def test_bootstrap_installs_the_dispatching_hook_not_pre_commits() -> None:
    text = BOOTSTRAP.read_text(encoding="utf-8")
    assert "pre-commit install --hook-type pre-push" not in text
    assert "scripts/pre-push-hook.sh" in text
    assert "--show-toplevel" in text  # the stub resolves the pushing worktree at push time


@pytest.mark.skipif(shutil.which("git") is None, reason="no git")
def test_the_installed_stub_runs_the_pushing_worktrees_script(tmp_path: Path) -> None:
    """The stub bootstrap writes, run from a worktree, execs that worktree's script."""
    top = _tree(tmp_path, venv=True)
    subprocess.run(["git", "init", "-q", str(top)], check=True)  # noqa: S603, S607
    stub = _stub_from_bootstrap()
    hook = top / ".git" / "hooks" / "pre-push"
    hook.write_text(stub, encoding="utf-8")
    hook.chmod(0o755)
    calls = tmp_path / "calls.txt"
    env = {"PATH": "/usr/bin:/bin", "HOOK_CALLS": str(calls), "HOME": str(tmp_path)}
    result = subprocess.run(  # noqa: S603
        [str(hook), "origin", "u"], input="", capture_output=True, text=True, env=env, cwd=top,
        check=False,
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    assert calls.read_text(encoding="utf-8").startswith("python|-m|pre_commit|hook-impl|")


def _stub_from_bootstrap() -> str:
    text = BOOTSTRAP.read_text(encoding="utf-8")
    start = text.index("<<'HOOK'\n") + len("<<'HOOK'\n")
    return text[start : text.index("\nHOOK\n", start) + 1]


@pytest.mark.skipif(shutil.which("git") is None, reason="no git")
def test_the_stub_still_works_in_a_worktree_from_before_the_script(tmp_path: Path) -> None:
    """Other sessions' worktrees share .git/hooks and may predate scripts/pre-push-hook.sh."""
    top = _tree(tmp_path, venv=True)
    (top / "scripts" / "pre-push-hook.sh").unlink()
    subprocess.run(["git", "init", "-q", str(top)], check=True)  # noqa: S603, S607
    hook = top / ".git" / "hooks" / "pre-push"
    hook.write_text(_stub_from_bootstrap(), encoding="utf-8")
    hook.chmod(0o755)
    calls = tmp_path / "calls.txt"
    env = {"PATH": "/usr/bin:/bin", "HOOK_CALLS": str(calls), "HOME": str(tmp_path)}
    result = subprocess.run(  # noqa: S603
        [str(hook), "origin", "u"], input="", capture_output=True, text=True, env=env, cwd=top,
        check=False,
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    assert calls.read_text(encoding="utf-8").startswith("python|-m|pre_commit|hook-impl|")
