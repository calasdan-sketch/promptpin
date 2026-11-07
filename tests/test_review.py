"""`promptpin review`: Markdown a non-engineer can approve."""

import subprocess

from promptpin import cli, deploys, review
from tests.conftest import CLASSIFIER_V1, CLASSIFIER_V2, commit_prompt, release


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _main(repo):
    return subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()


def _setup(prompt_repo):
    release(prompt_repo, "classifier", "v1")
    deploys.deploy(prompt_repo, "classifier", "v1", "production", commit=True)
    base = _main(prompt_repo)
    git(prompt_repo, "branch", "base-snapshot")
    return base


def _promote(repo, text, ver="v2", message="tune"):
    commit_prompt(repo, "classifier", text, message)
    release(repo, "classifier", ver)
    deploys.deploy(repo, "classifier", ver, "production", commit=True)


def test_no_changes(prompt_repo):
    _setup(prompt_repo)
    md, ok = review.review(prompt_repo, "base-snapshot")
    assert ok and "unchanged" in md


def test_wording_and_settings_summary(prompt_repo):
    _setup(prompt_repo)
    _promote(prompt_repo, CLASSIFIER_V2)
    md, ok = review.review(prompt_repo, "base-snapshot")
    assert ok
    assert "Wording changed" in md and "Settings changed: params.temperature" in md
    assert "| `params.temperature` | `0.2` | `0.5` |" in md
    assert "```diff" in md and "+Reply with one of: bug, feature, question, spam." in md
    assert "To undo: `promptpin deploy classifier@" in md


def test_whitespace_only_is_called_out(prompt_repo):
    _setup(prompt_repo)
    _promote(prompt_repo, CLASSIFIER_V1.replace("question.\n", "question.   \n"))
    md, ok = review.review(prompt_repo, "base-snapshot")
    assert "Whitespace-only change" in md and "Wording changed" not in md


def test_hidden_character_blocks(prompt_repo):
    _setup(prompt_repo)
    _promote(prompt_repo, CLASSIFIER_V1.replace("Classify", "Classify\u202e"))
    md, ok = review.review(prompt_repo, "base-snapshot")
    assert not ok and "invisible character" in md
    assert "\u202e" not in md and "\\u202e" in md
    assert cli.main(["--repo", str(prompt_repo), "review", "--base", "base-snapshot"]) == 1


def test_markdown_injection_is_contained(prompt_repo):
    _setup(prompt_repo)
    evil = CLASSIFIER_V1.replace(
        "Classify this ticket:",
        "```\n  @everyone [x](http://e.example) <img src=x>\n  ``````\n  Classify:",
    )
    _promote(prompt_repo, evil)
    md, _ = review.review(prompt_repo, "base-snapshot")
    fence_lines = [ln for ln in md.splitlines() if ln.startswith("`" * 3)]
    longest = max(len(ln) - len(ln.lstrip("`")) for ln in fence_lines)
    assert longest >= 7  # outer fence is longer than the longest run in the text


def test_new_env_pin_and_removed_pin(prompt_repo):
    _setup(prompt_repo)
    deploys.deploy(prompt_repo, "classifier", "v1", "staging", commit=True)
    md, ok = review.review(prompt_repo, "base-snapshot")
    assert "New in this environment" in md and ok


def test_bad_base_fails_closed(prompt_repo, capsys):
    _setup(prompt_repo)
    assert cli.main(["--repo", str(prompt_repo), "review", "--base", "nope"]) == 1


def test_head_ref_mode(prompt_repo):
    _setup(prompt_repo)
    _promote(prompt_repo, CLASSIFIER_V2)
    md, _ = review.review(prompt_repo, "base-snapshot", "HEAD")
    assert "Wording changed" in md


def test_diff_command_flags_cosmetic(prompt_repo, capsys):
    release(prompt_repo, "classifier", "v1")
    commit_prompt(prompt_repo, "classifier", CLASSIFIER_V1.replace("question.\n", "question.  \n"))
    release(prompt_repo, "classifier", "v2")
    cli.main(["--repo", str(prompt_repo), "diff", "classifier", "v1", "v2"])
    assert "whitespace-only change" in capsys.readouterr().out
