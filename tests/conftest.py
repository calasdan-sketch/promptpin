"""Shared fixtures: throwaway git repos with prompt files."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from promptpin import gitutil


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """An empty git repo with user config and one initial commit."""
    d = tmp_path / "repo"
    d.mkdir()
    _git(d, "init")
    _git(d, "config", "user.name", "Test User")
    _git(d, "config", "user.email", "test@example.com")
    (d / "README.md").write_text("test repo\n")
    _git(d, "add", "README.md")
    _git(d, "commit", "-m", "init")
    return d


@pytest.fixture()
def prompt_repo(repo: Path) -> Path:
    """A repo with prompts/ containing a classifier prompt, committed."""
    prompts = repo / "prompts"
    prompts.mkdir()
    (prompts / "classifier.yaml").write_text(
        "name: classifier\n"
        "description: Ticket classifier\n"
        "model: gpt-4o-mini\n"
        "params:\n"
        "  temperature: 0.2\n"
        "  max_tokens: 200\n"
        "variables:\n"
        "  - ticket_text\n"
        "template: |\n"
        "  Classify this ticket: {{ticket_text}}\n"
        "  Reply with one of: bug, feature, question.\n",
        encoding="utf-8",
    )
    _git(repo, "add", "prompts")
    _git(repo, "commit", "-m", "add classifier prompt")
    return repo


def commit_prompt(repo: Path, name: str, text: str, message: str = "update prompt") -> str:
    (repo / "prompts" / f"{name}.yaml").write_text(text, encoding="utf-8")
    _git(repo, "add", f"prompts/{name}.yaml")
    _git(repo, "commit", "-m", message)
    return gitutil.head_sha(repo)


def release(repo: Path, name: str, version: str = "v1") -> str:
    return gitutil.create_tag(repo, name, version, gitutil.head_sha(repo), f"release {version}")


CLASSIFIER_V1 = """\
name: classifier
description: Ticket classifier
model: gpt-4o-mini
params:
  temperature: 0.2
  max_tokens: 200
variables:
  - ticket_text
template: |
  Classify this ticket: {{ticket_text}}
  Reply with one of: bug, feature, question.
"""

CLASSIFIER_V2 = """\
name: classifier
description: Ticket classifier
model: gpt-4o-mini
params:
  temperature: 0.5
  max_tokens: 200
variables:
  - ticket_text
template: |
  Classify this ticket: {{ticket_text}}
  Reply with one of: bug, feature, question, spam.
"""
