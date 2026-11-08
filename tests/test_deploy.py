"""Deploy / rollback / status / history: pinning versions to envs."""

import pytest
import yaml

from promptpin import deploys, statedb
from promptpin.config import LOCK_FILENAME
from promptpin.errors import DeployError
from tests.conftest import CLASSIFIER_V2, commit_prompt, release


def _deploy(prompt_repo, spec_name="classifier", ref="v1", env="production", **kw):
    return deploys.deploy(prompt_repo, spec_name, ref, env, **kw)


def test_deploy_writes_lock_with_sha(prompt_repo):
    release(prompt_repo, "classifier", "v1")
    result = _deploy(prompt_repo)
    assert result["sha"]
    assert result["previous_sha"] is None
    assert result["commit_sha"]  # auto-committed by default
    pins = deploys.read_lock(prompt_repo)
    pin = pins["production"]["classifier"]
    assert pin.sha == result["sha"]
    assert pin.ref == "v1"
    assert pin.tag == "promptpin/classifier@v1"
    assert pin.model == "gpt-4o-mini"
    assert not pin.mutable_ref


def test_deploy_records_ledger(prompt_repo):
    release(prompt_repo, "classifier", "v1")
    _deploy(prompt_repo, note="first")
    conn = statedb.connect(prompt_repo)
    try:
        rows = statedb.deploy_history(conn, env="production", prompt="classifier")
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["note"] == "first"
    assert rows[0]["actor"]


def test_deploy_unknown_env_rejected(prompt_repo):
    release(prompt_repo, "classifier", "v1")
    with pytest.raises(DeployError, match="unknown env"):
        _deploy(prompt_repo, env="moon")


def test_deploy_unknown_version_rejected(prompt_repo):
    with pytest.raises(Exception, match="v99"):
        _deploy(prompt_repo, ref="v99")


def test_deploy_mutable_ref_warns_but_pins_sha(prompt_repo):
    import subprocess

    subprocess.run(
        ["git", "checkout", "-b", "exp", "HEAD"], cwd=prompt_repo, check=True, capture_output=True
    )
    result = _deploy(prompt_repo, ref="exp")
    assert result["mutable_ref"]
    pins = deploys.read_lock(prompt_repo)
    assert pins["production"]["classifier"].mutable_ref


def test_second_deploy_records_previous_sha(prompt_repo):
    release(prompt_repo, "classifier", "v1")
    r1 = _deploy(prompt_repo)
    commit_prompt(prompt_repo, "classifier", CLASSIFIER_V2, "v2 text")
    release(prompt_repo, "classifier", "v2")
    r2 = _deploy(prompt_repo, ref="v2")
    assert r2["previous_sha"] == r1["sha"]
    pins = deploys.read_lock(prompt_repo)
    assert pins["production"]["classifier"].sha == r2["sha"]


def test_rollback_restores_previous_sha(prompt_repo):
    release(prompt_repo, "classifier", "v1")
    r1 = _deploy(prompt_repo)
    commit_prompt(prompt_repo, "classifier", CLASSIFIER_V2, "v2 text")
    release(prompt_repo, "classifier", "v2")
    _deploy(prompt_repo, ref="v2")
    result = deploys.rollback(prompt_repo, "classifier", "production")
    assert result["sha"] == r1["sha"]
    pins = deploys.read_lock(prompt_repo)
    assert pins["production"]["classifier"].sha == r1["sha"]


def test_rollback_without_history_raises(prompt_repo):
    with pytest.raises(DeployError, match="nothing to roll back"):
        deploys.rollback(prompt_repo, "classifier", "production")


def test_deploy_no_commit_flag(prompt_repo):
    release(prompt_repo, "classifier", "v1")
    result = _deploy(prompt_repo, commit=False)
    assert result["commit_sha"] is None
    import subprocess

    out = subprocess.run(
        ["git", "status", "--porcelain", LOCK_FILENAME],
        cwd=prompt_repo,
        capture_output=True,
        text=True,
    )
    assert out.stdout.strip()  # lock file modified but uncommitted


def test_status_detects_moved_tag(prompt_repo):
    import subprocess

    release(prompt_repo, "classifier", "v1")
    _deploy(prompt_repo)
    # Move the tag to a new commit (simulates tag rewrite).
    commit_prompt(prompt_repo, "classifier", CLASSIFIER_V2, "sneaky change")
    subprocess.run(
        ["git", "tag", "-f", "promptpin/classifier@v1", "HEAD"],
        cwd=prompt_repo,
        check=True,
        capture_output=True,
    )
    pins = deploys.read_lock(prompt_repo)
    issues = deploys.status_issues(prompt_repo, pins)
    assert len(issues) == 1
    assert "moved" in issues[0]


def test_status_detects_deleted_tag(prompt_repo):
    import subprocess

    release(prompt_repo, "classifier", "v1")
    _deploy(prompt_repo)
    subprocess.run(
        ["git", "tag", "-d", "promptpin/classifier@v1"],
        cwd=prompt_repo,
        check=True,
        capture_output=True,
    )
    pins = deploys.read_lock(prompt_repo)
    issues = deploys.status_issues(prompt_repo, pins)
    assert len(issues) == 1
    assert "no longer exists" in issues[0]
    # The pin itself is untouched — frozen at the deployed SHA.
    assert pins["production"]["classifier"].sha


def test_lock_file_is_plain_yaml(prompt_repo):
    release(prompt_repo, "classifier", "v1")
    _deploy(prompt_repo, commit=False)
    data = yaml.safe_load((prompt_repo / LOCK_FILENAME).read_text())
    assert data["envs"]["production"]["classifier"]["sha"]
    assert data["envs"]["production"]["classifier"]["template_sha256"]


def test_deploy_requires_git_repo(tmp_path):
    from promptpin.errors import NotAGitRepoError

    with pytest.raises(NotAGitRepoError):
        deploys.deploy(tmp_path / "nope", "x", "v1", "production")


def test_deploy_full_tag_ref_records_tag_for_drift_detection(prompt_repo):
    """Deploying `name@promptpin/<name>@vN` must record the tag (like `vN`
    does) so `status` can detect tag moves/deletes afterwards."""
    release(prompt_repo, "classifier", "v1")
    result = _deploy(prompt_repo, ref="promptpin/classifier@v1")
    assert result["sha"]
    pins = deploys.read_lock(prompt_repo)
    pin = pins["production"]["classifier"]
    assert pin.tag == "promptpin/classifier@v1"
    assert not pin.mutable_ref
    # No drift yet: the tag still points at the pinned SHA.
    assert deploys.status_issues(prompt_repo, pins) == []
