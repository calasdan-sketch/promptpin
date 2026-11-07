"""HMAC-signed in-toto attestations."""

import base64
import json

import pytest

from promptpin import PinnedPrompts, PromptIntegrityError, attest, cli, deploys
from tests.conftest import CLASSIFIER_V2, commit_prompt, release

KEY = b"k" * 32


@pytest.fixture()
def pinned(prompt_repo, tmp_path):
    release(prompt_repo, "classifier", "v1")
    deploys.deploy(prompt_repo, "classifier", "v1", "production", commit=True)
    bundle = tmp_path / "b"
    deploys.export_bundle(prompt_repo, "production", bundle)
    return prompt_repo, bundle


def test_statement_shape(pinned):
    repo, bundle = pinned
    st = attest.build_statement(repo, "production")
    assert st["_type"] == "https://in-toto.io/Statement/v1"
    assert st["subject"][0]["name"] == "prompt/classifier"
    assert len(st["subject"][0]["digest"]["sha256"]) == 64
    manifest_digest = json.loads((bundle / "manifest.json").read_text())["bundle_digest"]
    assert st["predicate"]["bundle_digest"] == manifest_digest


def test_sign_open_roundtrip_and_wrong_key(pinned):
    repo, _ = pinned
    env = attest.sign(attest.build_statement(repo, "production"), KEY)
    assert attest.open_envelope(env, KEY)["predicate"]["env"] == "production"
    with pytest.raises(attest.AttestationError, match="does not match"):
        attest.open_envelope(env, b"z" * 32)


def test_payload_tamper_and_malformed(pinned):
    repo, _ = pinned
    env = attest.sign(attest.build_statement(repo, "production"), KEY)
    payload = json.loads(base64.b64decode(env["payload"]))
    payload["predicate"]["env"] = "staging"
    env["payload"] = base64.b64encode(json.dumps(payload).encode()).decode()
    with pytest.raises(attest.AttestationError):
        attest.open_envelope(env, KEY)
    for bad in (None, {}, {"payloadType": attest.PAYLOAD_TYPE}, {"payloadType": "x"}):
        with pytest.raises(attest.AttestationError):
            attest.open_envelope(bad, KEY)


def test_short_key_rejected(pinned):
    repo, _ = pinned
    with pytest.raises(attest.AttestationError, match="at least"):
        attest.sign(attest.build_statement(repo, "production"), b"short")


def test_refuses_to_attest_a_broken_lock(pinned):
    repo, _ = pinned
    lock = repo / "promptpin.lock"
    lock.write_text(lock.read_text().replace("gpt-4o-mini", "other-model"))
    with pytest.raises(attest.AttestationError, match="verify failed"):
        attest.build_statement(repo, "production")


def test_runtime_uses_attested_digest(pinned, tmp_path):
    repo, bundle = pinned
    path = tmp_path / "att.json"
    path.write_text(json.dumps(attest.sign(attest.build_statement(repo, "production"), KEY)))
    pp = PinnedPrompts(bundle, attestation=path, attestation_key=KEY)
    assert pp.env == "production" and pp.get("classifier").receipt.verified
    (bundle / "classifier.template.md").write_text("EVIL")
    with pytest.raises(PromptIntegrityError):
        PinnedPrompts(bundle, attestation=path, attestation_key=KEY)
    with pytest.raises(PromptIntegrityError):
        PinnedPrompts(bundle, attestation=path, attestation_key=b"y" * 32)
    with pytest.raises(ValueError):
        PinnedPrompts(bundle, attestation=path)


def test_attestation_goes_stale_when_pins_move(pinned, tmp_path):
    repo, _ = pinned
    env = attest.sign(attest.build_statement(repo, "production"), KEY)
    commit_prompt(repo, "classifier", CLASSIFIER_V2)
    release(repo, "classifier", "v2")
    deploys.deploy(repo, "classifier", "v2", "production", commit=True)
    problems = attest.check_against_repo(repo, attest.open_envelope(env, KEY), "production")
    assert problems


def test_other_env_changes_do_not_stale_attestation(pinned):
    repo, _ = pinned
    env = attest.sign(attest.build_statement(repo, "production"), KEY)
    deploys.deploy(repo, "classifier", "v1", "staging", commit=True)
    assert attest.check_against_repo(repo, attest.open_envelope(env, KEY), "production") == []


def test_cli_roundtrip(pinned, tmp_path, monkeypatch, capsys):
    repo, bundle = pinned
    monkeypatch.setenv("PROMPTPIN_ATTEST_KEY", KEY.decode())
    out = tmp_path / "a.json"
    assert cli.main(["--repo", str(repo), "attest", "--env", "production", "--out", str(out)]) == 0
    base = ["--repo", str(repo), "attest-verify", str(out), "--bundle", str(bundle)]
    assert cli.main(base) == 0
    (bundle / "classifier.template.md").write_text("EVIL")
    assert cli.main(base) == 1
    monkeypatch.delenv("PROMPTPIN_ATTEST_KEY")
    assert cli.main(base) == 1
