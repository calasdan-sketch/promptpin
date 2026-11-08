"""Golden-file tests: every machine-readable output format, byte for byte.

The repo is built with fixed identities and dates, so commit SHAs (and therefore
every file below) are identical on any machine with a plain git. To refresh
after an intentional format change:  UPDATE_GOLDEN=1 pytest tests/test_golden.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from promptpin import attest, cli, deploys, evidence, gitutil, review

GOLDEN = Path(__file__).parent / "golden"
NOW = "2026-01-02T03:04:05Z"
DATE = "2026-01-01T00:00:00+00:00"

V1 = """\
name: greeter
description: Greets
model: gpt-4o-mini
params:
  temperature: 0.7
variables:
  - user_name
template: |
  Greet {{user_name}} warmly in one short sentence.
"""
V2 = V1.replace("warmly", "kindly").replace("0.7", "0.4")
CHAT = """\
name: support
description: Chat
model: gpt-4o-mini
format: chat
variables:
  - ticket
template: |
  system:
  You are a support agent.

  user:
  {{ticket}}
"""


def _git(repo: Path, *args: str) -> None:
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "Golden",
        "GIT_AUTHOR_EMAIL": "golden@example.invalid",
        "GIT_COMMITTER_NAME": "Golden",
        "GIT_COMMITTER_EMAIL": "golden@example.invalid",
        "GIT_AUTHOR_DATE": DATE,
        "GIT_COMMITTER_DATE": DATE,
    }
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, env=env)


@pytest.fixture()
def golden_repo(tmp_path, monkeypatch):
    for k, v in {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "Golden",
        "GIT_AUTHOR_EMAIL": "golden@example.invalid",
        "GIT_COMMITTER_NAME": "Golden",
        "GIT_COMMITTER_EMAIL": "golden@example.invalid",
        "GIT_AUTHOR_DATE": DATE,
        "GIT_COMMITTER_DATE": DATE,
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(deploys, "_utc_iso", lambda: NOW)
    repo = tmp_path / "repo"
    (repo / "prompts").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    (repo / "prompts" / "greeter.yaml").write_text(V1, encoding="utf-8")
    (repo / "prompts" / "support.yaml").write_text(CHAT, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add prompts")
    gitutil.create_tag(repo, "greeter", "v1", gitutil.head_sha(repo), "release v1")
    deploys.deploy(repo, "greeter", "v1", "production", commit=True, actor="Golden")
    deploys.deploy(
        repo, "support", gitutil.head_sha(repo), "production", commit=True, actor="Golden"
    )
    (repo / "prompts" / "greeter.yaml").write_text(V2, encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "tweak greeter")
    gitutil.create_tag(repo, "greeter", "v2", gitutil.head_sha(repo), "release v2")
    return repo


def _check(name: str, text: str) -> None:
    path = GOLDEN / name
    if os.environ.get("UPDATE_GOLDEN"):
        path.write_text(text, encoding="utf-8", newline="\n")
    assert path.read_text(encoding="utf-8") == text, f"{name} changed (UPDATE_GOLDEN=1 to refresh)"


def test_golden_lock_and_bundle(golden_repo, tmp_path):
    _check("promptpin.lock", (golden_repo / "promptpin.lock").read_text(encoding="utf-8"))
    out = tmp_path / "bundle"
    deploys.export_bundle(golden_repo, "production", out)
    for fname in ("manifest.yaml", "manifest.json", "greeter.template.md", "support.template.md"):
        _check(f"bundle_{fname}", (out / fname).read_text(encoding="utf-8"))


def test_golden_attestation_statement(golden_repo):
    st = attest.build_statement(golden_repo, "production")
    _check("attestation_statement.json", json.dumps(st, indent=2, sort_keys=True) + "\n")
    env = attest.sign(st, bytes(range(33, 65)))
    _check("attestation_envelope.json", json.dumps(env, indent=2, sort_keys=True) + "\n")


def test_golden_evidence_report_and_markdown(golden_repo, monkeypatch):
    monkeypatch.setattr(evidence, "__version__", "<version>")
    rep = evidence.build(golden_repo, "production", key=bytes(range(33, 65)))
    _check("evidence.json", json.dumps(rep, indent=2, sort_keys=True) + "\n")
    rep.pop("hmac_sha256")
    _check("evidence.md", evidence.to_markdown(rep))


def test_golden_review_markdown(golden_repo):
    deploys.deploy(golden_repo, "greeter", "v2", "production", commit=False, actor="Golden")
    text, ok = review.review(golden_repo, "HEAD")
    assert ok
    _check("review.md", text)


def test_golden_cli_json_outputs(golden_repo, capsys):
    outputs = {}
    for name, argv in {
        "cli_status.json": ["status", "--json"],
        "cli_list.json": ["list", "--json"],
        "cli_history.json": ["history", "--json"],
        "cli_verify.json": ["verify", "--json"],
    }.items():
        capsys.readouterr()
        assert cli.main(["--repo", str(golden_repo), *argv]) == 0
        outputs[name] = capsys.readouterr().out
    for name, text in outputs.items():
        _check(name, re.sub(r"\s+$", "\n", text))
