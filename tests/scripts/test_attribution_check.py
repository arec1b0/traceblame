"""scripts/attribution-check.sh: no AI attribution in commits (hook and CI) or in the PR text (CI).

Every test runs in a temporary git repository with its own identity and no signing. The
environment hides the user's global and system git configuration (GIT_CONFIG_GLOBAL and
GIT_CONFIG_SYSTEM), so nothing here reads or changes it.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "attribution-check.sh"

ROBOT = "\U0001f916"
PHRASE = "Generated with"

HOOK_CONFIG = f"""\
repos:
  - repo: local
    hooks:
      - id: attribution
        name: attribution
        entry: {SCRIPT}
        language: unsupported
        stages: [pre-push]
        always_run: true
        pass_filenames: false
"""


def _env(tmp_path: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PRE_COMMIT_", "PR_", "GIT_"))}
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_SYSTEM=os.devnull,
        GIT_AUTHOR_NAME="Test",
        GIT_AUTHOR_EMAIL="test@example.com",
        GIT_COMMITTER_NAME="Test",
        GIT_COMMITTER_EMAIL="test@example.com",
        PRE_COMMIT_HOME=str(tmp_path / "pre-commit-home"),
    )
    env.update(extra or {})
    return env


def _git(repo: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - git in a temporary repository
        ["git", "-C", str(repo), *args],  # noqa: S607
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _commit(repo: Path, env: dict[str, str], message: str) -> str:
    done = subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), "commit", "--allow-empty", "-q", "-F", "-"],  # noqa: S607
        env=env,
        input=message,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return _git(repo, env, "rev-parse", "HEAD").stdout.strip()


class Fixture:
    """A repository whose main is also on its remote `origin`, with a feature branch checked out."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.env = _env(tmp_path)
        self.remote = tmp_path / "remote.git"
        self.repo = tmp_path / "repo"
        for args in (
            ["init", "-q", "--bare", "-b", "main", str(self.remote)],
            ["init", "-q", "-b", "main", str(self.repo)],
        ):
            done = subprocess.run(  # noqa: S603
                ["git", *args],  # noqa: S607
                env=self.env,
                capture_output=True,
                text=True,
                check=False,
            )
            assert done.returncode == 0, done.stderr
        # The temporary repository only: commits here are never signed.
        self.git("config", "commit.gpgsign", "false")
        self.git("remote", "add", "origin", str(self.remote))
        (self.repo / ".pre-commit-config.yaml").write_text(HOOK_CONFIG, encoding="utf-8")
        self.git("add", ".pre-commit-config.yaml")
        self.commit("chore: base")
        assert self.git("push", "-q", "-u", "origin", "main").returncode == 0
        self.git("switch", "-q", "-c", "feat/x")

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return _git(self.repo, self.env, *args)

    def commit(self, message: str) -> str:
        return _commit(self.repo, self.env, message)

    def check(
        self, *args: str, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - the script under test
            ["bash", str(SCRIPT), *args],  # noqa: S607
            env={**self.env, **(env or {})},
            cwd=self.repo,
            capture_output=True,
            text=True,
            check=False,
        )

    def install_hook(self) -> None:
        done = subprocess.run(
            [sys.executable, "-m", "pre_commit", "install", "--hook-type", "pre-push"],
            env=self.env,
            cwd=self.repo,
            capture_output=True,
            text=True,
            check=False,
        )
        assert done.returncode == 0, done.stderr


@pytest.fixture
def fx(tmp_path: Path) -> Fixture:
    return Fixture(tmp_path)


# Commits -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "trailer",
    [
        "Co-Authored-By: Someone <someone@example.com>",
        "co-authored-by: Someone <someone@example.com>",
        "Assisted-by: some-tool",
        "Generated-by: some-tool",
    ],
)
def test_an_attribution_trailer_fails(fx: Fixture, trailer: str) -> None:
    sha = fx.commit(f"feat: x\n\nBody.\n\n{trailer}\n")
    result = fx.check()
    assert result.returncode == 1
    assert sha[:7] in result.stderr
    assert "trailer" in result.stderr


def test_a_trailer_line_outside_a_recognised_trailer_block_still_fails(fx: Fixture) -> None:
    # git only reads the last paragraph as trailers, and only when most of it is trailers.
    fx.commit("feat: x\n\nSome prose.\nCo-Authored-By: Someone <s@example.com>\nMore prose.\n")
    result = fx.check()
    assert result.returncode == 1
    assert "trailer" in result.stderr


def test_a_subject_naming_the_instructions_file_passes(fx: Fixture) -> None:
    fx.commit("docs: reword the attribution rule in CLAUDE.md\n")
    result = fx.check()
    assert result.returncode == 0, result.stderr
    assert "1 commit" in result.stdout


def test_prose_naming_the_tool_passes(fx: Fixture) -> None:
    fx.commit(
        "docs: note the tooling\n\n"
        "The shim was exercised with Claude Code as the tool; the old check also flagged\n"
        "that. Co-Authored-By is mentioned here in prose, not as a trailer line.\n"
    )
    result = fx.check()
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("line", [f"{ROBOT} {PHRASE} [a tool](https://example.com)", PHRASE, ROBOT])
def test_an_attribution_phrase_in_a_body_fails(fx: Fixture, line: str) -> None:
    sha = fx.commit(f"feat: x\n\nBody.\n\n{line}\n")
    result = fx.check()
    assert result.returncode == 1
    assert sha[:7] in result.stderr


def test_subjects_are_not_checked_for_phrases(fx: Fixture) -> None:
    # The footers live in bodies; a subject may say "Generated with" about a file.
    fx.commit(f"docs: files {PHRASE} the pinned tool\n")
    assert fx.check().returncode == 0


def test_the_phrase_is_case_sensitive(fx: Fixture) -> None:
    fx.commit("feat: x\n\nThe table is generated with the pinned tool.\n")
    assert fx.check().returncode == 0


def test_every_finding_is_listed(fx: Fixture) -> None:
    first = fx.commit("feat: a\n\nAssisted-by: tool\n")
    fx.commit("feat: clean\n")
    second = fx.commit(f"feat: b\n\n{ROBOT}\n")
    result = fx.check()
    assert result.returncode == 1
    assert first[:7] in result.stderr
    assert second[:7] in result.stderr


def test_an_empty_range_passes(fx: Fixture) -> None:
    fx.git("switch", "-q", "main")
    result = fx.check()
    assert result.returncode == 0
    assert "0 commits" in result.stdout


# The range ---------------------------------------------------------------------------------


def test_the_default_range_is_origin_main_to_head(fx: Fixture) -> None:
    # A bad commit that is already on main (the remote) is not this branch's.
    fx.git("switch", "-q", "main")
    fx.commit("feat: old\n\nAssisted-by: tool\n")
    assert fx.git("push", "-q", "origin", "main").returncode == 0
    fx.git("switch", "-q", "feat/x")
    fx.git("reset", "-q", "--hard", "origin/main")
    fx.commit("feat: clean\n")
    assert fx.check().returncode == 0


def test_pre_commit_refs_set_the_range(fx: Fixture) -> None:
    bad = fx.commit("feat: bad\n\nAssisted-by: tool\n")
    good = fx.commit("feat: good\n")
    env = {"PRE_COMMIT_FROM_REF": bad, "PRE_COMMIT_TO_REF": good}
    assert fx.check(env=env).returncode == 0  # the range excludes FROM_REF itself
    env = {"PRE_COMMIT_FROM_REF": "origin/main", "PRE_COMMIT_TO_REF": good}
    assert fx.check(env=env).returncode == 1


def test_an_argument_wins_over_the_environment(fx: Fixture) -> None:
    fx.commit("feat: bad\n\nAssisted-by: tool\n")
    good = fx.commit("feat: good\n")
    env = {"PRE_COMMIT_FROM_REF": "origin/main", "PRE_COMMIT_TO_REF": good}
    assert fx.check(f"{good}..{good}", env=env).returncode == 0


def test_an_unresolvable_range_fails_closed(fx: Fixture) -> None:
    result = fx.check("no-such-ref..HEAD")
    assert result.returncode == 1
    assert "cannot resolve" in result.stderr
    fx.git("remote", "remove", "origin")
    assert fx.check().returncode == 1  # the default range needs origin/main


# The PR text (CI) --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "label", "text"),
    [
        ("PR_BODY", "PR body", f"Summary.\n\n{ROBOT} {PHRASE} [a tool](https://example.com)\n"),
        ("PR_BODY", "PR body", f"Summary.\n\n{PHRASE} a tool\n"),
        ("PR_BODY", "PR body", "Summary.\n\nCo-Authored-By: Someone <s@example.com>\n"),
        ("PR_TITLE", "PR title", f"feat: x {ROBOT}"),
    ],
)
def test_attribution_in_the_pr_text_fails(fx: Fixture, name: str, label: str, text: str) -> None:
    fx.commit("feat: clean\n")
    result = fx.check("origin/main..HEAD", env={name: text})
    assert result.returncode == 1
    assert label in result.stderr


def test_a_clean_pr_text_passes(fx: Fixture) -> None:
    fx.commit("feat: clean\n")
    env = {
        "PR_TITLE": "docs: reword the rule in CLAUDE.md",
        "PR_BODY": "Closes #47.\n\nUses Claude Code in prose; Co-Authored-By mid-line is fine.\n",
    }
    result = fx.check("origin/main..HEAD", env=env)
    assert result.returncode == 0, result.stderr


def test_a_clean_pr_text_does_not_excuse_a_bad_commit(fx: Fixture) -> None:
    fx.commit("feat: bad\n\nAssisted-by: tool\n")
    result = fx.check("origin/main..HEAD", env={"PR_BODY": "Fine."})
    assert result.returncode == 1


# A real push through the installed pre-push hook -------------------------------------------


def test_a_new_branch_is_checked_from_the_commits_on_no_remote(fx: Fixture) -> None:
    """The hook never sees the zero remote sha: pre-commit turns it into FROM_REF/TO_REF."""
    fx.install_hook()
    fx.commit("feat: one\n")
    fx.commit("feat: two\n\nAssisted-by: tool\n")
    blocked = fx.git("push", "-u", "origin", "feat/x")
    assert blocked.returncode != 0
    assert "attribution" in blocked.stdout + blocked.stderr
    assert fx.git("ls-remote", "--heads", "origin", "feat/x").stdout.strip() == ""

    fx.git("reset", "-q", "--hard", "HEAD~1")
    fx.commit("feat: two, clean\n")
    done = fx.git("push", "-u", "origin", "feat/x")
    assert done.returncode == 0, done.stdout + done.stderr
    assert fx.git("ls-remote", "--heads", "origin", "feat/x").stdout.strip() != ""


def test_a_later_push_checks_only_the_new_commits(fx: Fixture) -> None:
    # Commits that are already on the remote are not rechecked: push a bad one without the
    # hook installed, then install it.
    fx.commit("feat: old\n\nAssisted-by: tool\n")
    assert fx.git("push", "-q", "-u", "origin", "feat/x").returncode == 0
    fx.install_hook()
    fx.commit("feat: new, clean\n")
    done = fx.git("push", "origin", "feat/x")
    assert done.returncode == 0, done.stdout + done.stderr

    fx.commit(f"feat: newer\n\n{ROBOT}\n")
    assert fx.git("push", "origin", "feat/x").returncode != 0
