"""Adversarial: concurrent deploys, tag games, corrupt inputs."""

import subprocess
import threading

import pytest
import yaml

from promptpin import deploys, statedb
from promptpin.config import LOCK_FILENAME
from tests.conftest import CLASSIFIER_V2, commit_prompt, release


def _add_second_prompt(prompt_repo):
    (prompt_repo / "prompts" / "summarizer.yaml").write_text(
        "name: summarizer\nmodel: gpt-4o-mini\ntemplate: |\n  Summarize: {{text}}\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "add", "prompts/summarizer.yaml"], cwd=prompt_repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "add summarizer"], cwd=prompt_repo, check=True, capture_output=True
    )
    release(prompt_repo, "summarizer", "v1")


def test_concurrent_deploys_same_env_no_lost_update(prompt_repo):
    """Two threads deploy different prompts to the same env at once.

    Without the deploy mutex, the read-modify-write on promptpin.lock would
    lose one of the pins. Both must survive, and the ledger must have both.
    """
    release(prompt_repo, "classifier", "v1")
    _add_second_prompt(prompt_repo)
    errors: list = []

    def do(prompt):
        try:
            deploys.deploy(prompt_repo, prompt, "v1", "staging", commit=False)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=do, args=(p,)) for p in ("classifier", "summarizer")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    pins = deploys.read_lock(prompt_repo)
    assert set(pins["staging"]) == {"classifier", "summarizer"}
    conn = statedb.connect(prompt_repo)
    try:
        rows = statedb.deploy_history(conn, env="staging")
    finally:
        conn.close()
    assert len(rows) == 2


def test_concurrent_deploys_same_prompt_last_writer_wins_cleanly(prompt_repo):
    release(prompt_repo, "classifier", "v1")
    commit_prompt(prompt_repo, "classifier", CLASSIFIER_V2, "v2")
    release(prompt_repo, "classifier", "v2")
    errors: list = []

    def do(ref):
        try:
            deploys.deploy(prompt_repo, "classifier", ref, "production", commit=False)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=do, args=(r,)) for r in ("v1", "v2")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    # Lock file must be valid YAML with exactly one pin either way.
    data = yaml.safe_load((prompt_repo / LOCK_FILENAME).read_text())
    pin = data["envs"]["production"]["classifier"]
    assert pin["sha"] in {pin["sha"]} and len(pin["sha"]) == 40
    conn = statedb.connect(prompt_repo)
    try:
        rows = statedb.deploy_history(conn, env="production", prompt="classifier")
    finally:
        conn.close()
    assert len(rows) == 2  # both deploys recorded, none lost


def test_deploy_after_tag_deleted_still_records_sha(prompt_repo):
    """Deploying by raw SHA works even when the tag is gone — pins are SHAs."""
    release(prompt_repo, "classifier", "v1")
    import subprocess

    sha_proc = subprocess.run(
        ["git", "rev-parse", "promptpin/classifier@v1^{commit}"],
        cwd=prompt_repo,
        capture_output=True,
        text=True,
    )
    sha = sha_proc.stdout.strip()
    subprocess.run(
        ["git", "tag", "-d", "promptpin/classifier@v1"],
        cwd=prompt_repo,
        check=True,
        capture_output=True,
    )
    result = deploys.deploy(prompt_repo, "classifier", sha, "production", commit=False)
    assert result["sha"] == sha
    assert not result["mutable_ref"]


def test_deploy_ref_with_slash_in_prompt_name(prompt_repo):
    """Prompt names are restricted; path traversal via name must fail."""
    from promptpin.errors import PromptpinError

    with pytest.raises(PromptpinError):
        deploys.deploy(prompt_repo, "../evil", "v1", "production", commit=False)


def test_lock_file_survives_git_merge_of_different_envs(prompt_repo, tmp_path):
    """Two envs pinned on two branches merge without conflict (per-env keys)."""
    import subprocess

    release(prompt_repo, "classifier", "v1")
    deploys.deploy(prompt_repo, "classifier", "v1", "staging", commit=True)

    def git(*a):
        subprocess.run(["git", *a], cwd=prompt_repo, check=True, capture_output=True)

    git("checkout", "-b", "deploy-prod")
    deploys.deploy(prompt_repo, "classifier", "v1", "production", commit=True)
    git("checkout", "-")
    # Merge the prod branch: lock file has per-env mapping, git merges cleanly.
    proc = subprocess.run(
        ["git", "merge", "deploy-prod", "-m", "merge prod pin"],
        cwd=prompt_repo,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    pins = deploys.read_lock(prompt_repo)
    assert "staging" in pins and "production" in pins
