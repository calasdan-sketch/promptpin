"""verify --reachable-from / --require-signed, and the manifest.json twin."""

import json
import shutil
import subprocess

import pytest

from promptpin import cli, deploys
from tests.conftest import CLASSIFIER_V2, commit_prompt, release


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _msgs(checks):
    return [c.message for c in checks if c.level == "error"]


def test_reachable_from_default_branch(prompt_repo):
    release(prompt_repo, "classifier", "v1")
    deploys.deploy(prompt_repo, "classifier", "v1", "production", commit=False)
    main = git(prompt_repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert _msgs(deploys.verify_lock(prompt_repo, reachable_from=main)) == []


def test_side_branch_pin_is_rejected(prompt_repo):
    main = git(prompt_repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    git(prompt_repo, "checkout", "-b", "feature")
    commit_prompt(prompt_repo, "classifier", CLASSIFIER_V2)
    release(prompt_repo, "classifier", "v1")
    deploys.deploy(prompt_repo, "classifier", "v1", "production", commit=False)
    git(prompt_repo, "checkout", main)
    errs = _msgs(deploys.verify_lock(prompt_repo, reachable_from=main))
    assert len(errs) == 1 and "never merged" in errs[0]
    git(prompt_repo, "merge", "--no-ff", "feature", "-m", "merge")
    assert _msgs(deploys.verify_lock(prompt_repo, reachable_from=main)) == []


def test_unresolvable_reachable_ref_fails_closed(prompt_repo):
    release(prompt_repo, "classifier", "v1")
    deploys.deploy(prompt_repo, "classifier", "v1", "production", commit=False)
    errs = _msgs(deploys.verify_lock(prompt_repo, reachable_from="origin/mian"))
    assert errs and "cannot check reachability" in errs[0]


def test_option_like_ref_rejected(prompt_repo):
    release(prompt_repo, "classifier", "v1")
    deploys.deploy(prompt_repo, "classifier", "v1", "production", commit=False)
    assert _msgs(deploys.verify_lock(prompt_repo, reachable_from="--all"))


def test_unsigned_commits_fail_require_signed(prompt_repo):
    release(prompt_repo, "classifier", "v1")
    deploys.deploy(prompt_repo, "classifier", "v1", "production", commit=False)
    errs = _msgs(deploys.verify_lock(prompt_repo, require_signed=True))
    assert any("not signed" in e for e in errs)


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")
def test_ssh_signed_commits_pass(prompt_repo, tmp_path):
    key = tmp_path / "k"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    pub = (tmp_path / "k.pub").read_text().split()
    (tmp_path / "allowed").write_text(f"test@example.com {pub[0]} {pub[1]}\n")
    git(prompt_repo, "config", "gpg.format", "ssh")
    git(prompt_repo, "config", "user.signingkey", str(key))
    git(prompt_repo, "config", "gpg.ssh.allowedSignersFile", str(tmp_path / "allowed"))
    git(prompt_repo, "config", "commit.gpgsign", "true")
    probe = commit_prompt(prompt_repo, "classifier", CLASSIFIER_V2, "signed edit")
    if git(prompt_repo, "log", "-1", "--format=%G?", probe).stdout.strip() != "G":
        pytest.skip("this machine's ssh-keygen cannot produce verifiable signatures")
    release(prompt_repo, "classifier", "v1")
    deploys.deploy(prompt_repo, "classifier", "v1", "production", commit=True)
    assert _msgs(deploys.verify_lock(prompt_repo, require_signed=True)) == []
    # an unsigned follow-up commit to the lock is caught
    git(prompt_repo, "config", "commit.gpgsign", "false")
    lock = prompt_repo / "promptpin.lock"
    lock.write_text(lock.read_text() + "\n")
    git(prompt_repo, "commit", "-am", "unsigned lock edit")
    assert any("lock" in e for e in _msgs(deploys.verify_lock(prompt_repo, require_signed=True)))


def test_cli_flags(prompt_repo, capsys):
    release(prompt_repo, "classifier", "v1")
    deploys.deploy(prompt_repo, "classifier", "v1", "production", commit=False)
    rc = cli.main(["--repo", str(prompt_repo), "verify", "--reachable-from", "nope"])
    assert rc == 1


def test_manifest_json_twin_tamper_detected(prompt_repo, tmp_path):
    release(prompt_repo, "classifier", "v1")
    deploys.deploy(prompt_repo, "classifier", "v1", "production", commit=False)
    out = tmp_path / "b"
    deploys.export_bundle(prompt_repo, "production", out)
    assert _msgs(deploys.verify_lock(prompt_repo, bundle_dir=out)) == []
    m = json.loads((out / "manifest.json").read_text())
    m["prompts"]["classifier"]["sha"] = "f" * 40
    (out / "manifest.json").write_text(json.dumps(m))
    assert any(
        "manifest.json differs" in e
        for e in _msgs(deploys.verify_lock(prompt_repo, bundle_dir=out))
    )


@pytest.mark.parametrize(
    "code,level",
    [
        ("G", None),
        ("U", "warning"),
        ("N", "error"),
        ("B", "error"),
        ("E", "error"),
        ("X", "error"),
        ("Y", "error"),
        ("R", "error"),
        ("?", "error"),
    ],
)
def test_signature_status_mapping(prompt_repo, monkeypatch, code, level):
    release(prompt_repo, "classifier", "v1")
    deploys.deploy(prompt_repo, "classifier", "v1", "production", commit=False)
    monkeypatch.setattr(deploys.gitutil, "signature_status", lambda *a, **k: (code, "someone"))
    checks = [
        c for c in deploys.verify_lock(prompt_repo, require_signed=True) if "sign" in c.message
    ]
    levels = {c.level for c in checks}
    assert levels == ({level} if level else set())
