"""Round 5: git-derived history, promote, evidence, attestation freshness, upgrade path."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

import pytest

import promptpin
from promptpin import (
    PinnedPrompts,
    PromptIntegrityError,
    attest,
    cli,
    deploys,
    evidence,
    gitutil,
    history,
    log_serve,
)
from promptpin.errors import PromptpinError
from tests.conftest import CLASSIFIER_V2, commit_prompt, release

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "v0_1_1"
KEY = bytes(range(33, 65))


@pytest.fixture()
def two_envs(prompt_repo):
    """v1 on staging, v2 on staging then production; every deploy committed."""
    release(prompt_repo, "classifier", "v1")
    deploys.deploy(prompt_repo, "classifier", "v1", "staging", commit=True)
    commit_prompt(prompt_repo, "classifier", CLASSIFIER_V2, "tweak")
    release(prompt_repo, "classifier", "v2")
    deploys.deploy(prompt_repo, "classifier", "v2", "staging", commit=True)
    return prompt_repo


# ------------------------------------------------------------------ author --


def test_author_is_pinned_everywhere():
    name = "Calas Automations Inc."
    assert promptpin.__author__ == name
    assert f'authors = [{{ name = "{name}" }}]' in (ROOT / "pyproject.toml").read_text("utf-8")
    assert f"Copyright (c) 2026 {name}" in (ROOT / "LICENSE").read_text("utf-8")
    assert name in (ROOT / "README.md").read_text("utf-8")


def test_no_personal_handle_used_as_author_label():
    for rel in ("pyproject.toml", "LICENSE", "src/promptpin/__init__.py"):
        text = (ROOT / rel).read_text("utf-8")
        assert "Dan Calas" not in text and "DC Enterprises" not in text


# ------------------------------------------------------------ git history --


def test_history_comes_from_git_not_the_ledger(two_envs):
    ledger = two_envs / ".promptpin" / "state.db"
    ledger.unlink()  # a fresh clone / CI runner has no ledger
    events, problems = history.lock_history(two_envs, env="staging")
    assert problems == []
    assert [e.action for e in events] == ["repin", "pin"]
    assert events[0].previous_sha == events[1].sha
    assert events[0].commit_author == "Test User"


def test_rollback_works_on_a_fresh_clone_without_ledger(two_envs):
    (two_envs / ".promptpin" / "state.db").unlink()
    first = history.lock_history(two_envs, env="staging")[0][-1].sha
    res = deploys.rollback(two_envs, "classifier", "staging", commit=True)
    assert res["sha"] == first
    assert res["rolled_back_from"] != first  # the pin that was replaced, not the target
    assert deploys.read_lock(two_envs)["staging"]["classifier"].sha == first


def test_rollback_with_one_pin_still_refuses(prompt_repo):
    release(prompt_repo, "classifier", "v1")
    deploys.deploy(prompt_repo, "classifier", "v1", "staging", commit=True)
    with pytest.raises(PromptpinError, match="nothing to roll back"):
        deploys.rollback(prompt_repo, "classifier", "staging")


def test_history_flags_author_that_differs_from_deployed_by(two_envs):
    deploys.deploy(two_envs, "classifier", "v1", "production", commit=False, actor="Someone Else")
    gitutil.commit_file(two_envs, "promptpin.lock", "lock")
    ev = history.lock_history(two_envs, env="production")[0][0]
    assert ev.deployed_by == "Someone Else" and ev.claim_mismatch is True


def test_history_survives_a_corrupt_historic_lock(two_envs):
    (two_envs / "promptpin.lock").write_text("envs: [unclosed\n", encoding="utf-8")
    gitutil.commit_file(two_envs, "promptpin.lock", "oops")
    events, problems = history.lock_history(two_envs)
    assert any("could not be read" in p for p in problems)
    assert len(events) >= 2  # older, readable commits are still reported


def test_history_cli_json_and_text(two_envs, capsys):
    assert cli.main(["--repo", str(two_envs), "history", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["source"] == "git" and data["events"][0]["env"] == "staging"
    assert cli.main(["--repo", str(two_envs), "history", "--source", "ledger"]) == 0
    assert "local ledger" in capsys.readouterr().out


def test_audit_json(two_envs, capsys):
    assert cli.main(["--repo", str(two_envs), "audit", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert "classifier" in data["prompts"] and data["deploys_source"] == "git"


# ---------------------------------------------------------------- promote --


def test_promote_copies_the_exact_pin(two_envs):
    res = deploys.promote(two_envs, "classifier", "staging", "production", commit=True)
    lock = deploys.read_lock(two_envs)
    a, b = lock["staging"]["classifier"], lock["production"]["classifier"]
    assert (a.sha, a.digest, a.template_sha256) == (b.sha, b.digest, b.template_sha256)
    assert b.tag == a.tag and res["promoted_from"] == "staging"
    assert deploys.verify_lock(two_envs) == []


def test_promote_refuses_missing_source_same_env_and_moved_tag(two_envs):
    with pytest.raises(PromptpinError, match="not pinned on 'production'"):
        deploys.promote(two_envs, "classifier", "production", "staging")
    with pytest.raises(PromptpinError, match="same environment"):
        deploys.promote(two_envs, "classifier", "staging", "staging")
    commit_prompt(two_envs, "classifier", CLASSIFIER_V2.replace("spam", "junk"), "other")
    gitutil._run(two_envs, "tag", "-f", "promptpin/classifier@v2")
    with pytest.raises(PromptpinError, match="refusing to promote"):
        deploys.promote(two_envs, "classifier", "staging", "production")
    assert "production" not in deploys.read_lock(two_envs)


def test_promote_refuses_when_source_pin_was_tampered(two_envs):
    lock = (two_envs / "promptpin.lock").read_text("utf-8").replace("gpt-4o-mini", "other-model")
    (two_envs / "promptpin.lock").write_text(lock, encoding="utf-8")
    with pytest.raises(PromptpinError, match="fails verification"):
        deploys.promote(two_envs, "classifier", "staging", "production")


def test_promote_cli(two_envs, capsys):
    args = ["--repo", str(two_envs), "promote", "classifier", "--from", "staging", "--to"]
    assert cli.main([*args, "production"]) == 0
    assert "promoted classifier staging" in capsys.readouterr().out


# ------------------------------------------------------------ log verify --


def test_grace_window_still_catches_a_never_pinned_commit(two_envs, tmp_path):
    from promptpin.servecheck import verify_serving

    deploys.deploy(two_envs, "classifier", "v2", "production", commit=True)
    pin = deploys.read_lock(two_envs)["production"]["classifier"]
    old = deploys.read_lock(two_envs)["staging"]["classifier"]
    db = tmp_path / "s.db"
    log_serve(db, prompt="classifier", sha=pin.sha, digest=pin.digest)  # fine
    log_serve(db, prompt="classifier", sha="c" * 40, digest="d" * 64)  # never pinned
    errs = [c.message for c in verify_serving(two_envs, "production", db) if c.level == "error"]
    assert len(errs) == 1 and "never pinned" in errs[0] and "ccccccc" in errs[0]
    assert old.sha == pin.sha


def test_grace_window_allows_an_earlier_pinned_commit(two_envs, tmp_path):
    from promptpin.servecheck import verify_serving

    deploys.deploy(two_envs, "classifier", "v1", "production", commit=True)
    deploys.deploy(two_envs, "classifier", "v2", "production", commit=True)
    first = history.lock_history(two_envs, env="production")[0][-1]
    db = tmp_path / "s.db"
    log_serve(db, prompt="classifier", sha=first.sha, digest="")
    assert [c for c in verify_serving(two_envs, "production", db) if c.level == "error"] == []


def test_log_verify_and_stats_json(two_envs, tmp_path, capsys):
    deploys.deploy(two_envs, "classifier", "v2", "production", commit=True)
    db = tmp_path / "s.db"
    pin = deploys.read_lock(two_envs)["production"]["classifier"]
    log_serve(db, prompt="classifier", sha=pin.sha, digest=pin.digest)
    base = ["--repo", str(two_envs), "log"]
    assert cli.main([*base, "verify", "--db", str(db), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert cli.main([*base, "stats", "--json"]) == 0
    assert "total" in json.loads(capsys.readouterr().out)


# ---------------------------------------------------------------- attest ---


def test_attestation_records_creation_time(two_envs):
    st = attest.build_statement(two_envs, "staging")
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", st["predicate"]["created_at"])


def test_freshness_fails_closed(two_envs):
    st = attest.build_statement(two_envs, "staging")
    now = time.time()
    assert attest.check_freshness(st, 3600, now=now) == []
    assert "older" in attest.check_freshness(st, 3600, now=now + 7200)[0]
    assert "future" in attest.check_freshness(st, 3600, now=now - 7200)[0]
    del st["predicate"]["created_at"]
    assert "no valid created_at" in attest.check_freshness(st, 3600)[0]
    st["predicate"]["created_at"] = "yesterday-ish"
    assert "no valid created_at" in attest.check_freshness(st, 3600)[0]


def test_attest_verify_max_age_cli(two_envs, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PROMPTPIN_ATTEST_KEY", KEY.decode())
    out = tmp_path / "a.json"
    base = ["--repo", str(two_envs)]
    assert cli.main([*base, "attest", "--env", "staging", "--out", str(out)]) == 0
    assert cli.main([*base, "attest-verify", str(out), "--max-age", "1h"]) == 0
    env = json.loads(out.read_text())
    st = attest.open_envelope(env, KEY)
    st["predicate"]["created_at"] = "2020-01-01T00:00:00Z"
    out.write_text(json.dumps(attest.sign(st, KEY)))
    capsys.readouterr()
    assert cli.main([*base, "attest-verify", str(out), "--max-age", "1h"]) == 1
    assert "older than the allowed maximum" in capsys.readouterr().out
    assert cli.main([*base, "attest-verify", str(out)]) == 0  # no limit asked for


@pytest.mark.parametrize("key", [b"k" * 32, b"ab" * 20, b"x" * 31])
def test_weak_keys_are_refused(two_envs, key):
    with pytest.raises(attest.AttestationError):
        attest.sign(attest.build_statement(two_envs, "staging"), key)


# -------------------------------------------------------------- evidence ---


def test_evidence_report_roundtrip_and_tamper(two_envs, tmp_path):
    deploys.promote(two_envs, "classifier", "staging", "production", commit=True)
    rep = evidence.build(two_envs, "production", key=KEY)
    assert rep["verify"]["ok"] and rep["pins"][0]["prompt"] == "classifier"
    assert rep["history"][0]["action"] == "pin"
    assert evidence.check(rep, KEY) == []
    for path, value in (("claim_mismatches", 99), ("env", "staging")):
        bad = dict(rep, **{path: value})
        assert any("edited" in p for p in evidence.check(bad))
    bad = dict(rep, hmac_sha256=rep["hmac_sha256"][:-4] + "AAAA")
    assert any("hmac" in p for p in evidence.check(bad, KEY))
    assert any(
        "no hmac" in p
        for p in evidence.check({k: v for k, v in rep.items() if k != "hmac_sha256"}, KEY)
    )
    assert evidence.check({"schema": "x"}) == ["not a promptpin evidence report"]


def test_evidence_reports_failure_and_exit_code(two_envs, tmp_path, capsys):
    deploys.promote(two_envs, "classifier", "staging", "production", commit=True)
    lock = (two_envs / "promptpin.lock").read_text("utf-8")
    (two_envs / "promptpin.lock").write_text(lock.replace("gpt-4o-mini", "x-model"), "utf-8")
    out = tmp_path / "ev.json"
    assert (
        cli.main(["--repo", str(two_envs), "evidence", "--env", "production", "--out", str(out)])
        == 1
    )
    rep = json.loads(out.read_text())
    assert rep["verify"]["ok"] is False and rep["verify"]["errors"]


def test_evidence_cli_verify_and_expected_hash(two_envs, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PROMPTPIN_ATTEST_KEY", KEY.decode())
    deploys.promote(two_envs, "classifier", "staging", "production", commit=True)
    out = tmp_path / "ev.json"
    base = ["--repo", str(two_envs)]
    assert cli.main([*base, "evidence", "--env", "production", "--sign", "--out", str(out)]) == 0
    sha = json.loads(out.read_text())["report_sha256"]
    assert cli.main([*base, "evidence-verify", str(out), "--sign", "--expect-sha256", sha]) == 0
    assert cli.main([*base, "evidence-verify", str(out), "--expect-sha256", "0" * 64]) == 1
    out.write_text(out.read_text().replace('"production"', '"staging"', 1))
    assert cli.main([*base, "evidence-verify", str(out)]) == 1


def test_evidence_markdown_cannot_be_broken_by_a_hostile_note(two_envs, capsys):
    deploys.deploy(
        two_envs,
        "classifier",
        "v2",
        "production",
        commit=True,
        actor="evil|`\x1b[2J<script>|\u202e",
        note="n",
    )
    rep = evidence.build(two_envs, "production")
    md = evidence.to_markdown(rep)
    row = next(line for line in md.splitlines() if line.startswith("| classifier"))
    assert row.count("|") - row.count("\\|") == 6  # 5 columns, no injected cell
    assert "`\\x1b" not in md and "\x1b" not in md and "\u202e" not in md


def test_evidence_includes_serving_log_findings(two_envs, tmp_path):
    deploys.promote(two_envs, "classifier", "staging", "production", commit=True)
    db = tmp_path / "s.db"
    log_serve(db, prompt="classifier", sha="e" * 40, digest="")
    rep = evidence.build(two_envs, "production", db_file=db)
    assert rep["serving"]["checked"] is True
    assert any(c["level"] == "error" for c in rep["serving"]["checks"])


def test_evidence_refuses_an_unpinned_env(two_envs):
    with pytest.raises(PromptpinError, match="nothing to report"):
        evidence.build(two_envs, "production")


# ------------------------------------------------------------ upgrade path --


def test_0_1_1_lock_file_still_parses_and_verifies_with_warnings(tmp_path, prompt_repo):
    """Real promptpin 0.1.1 output: no format/digest keys."""
    text = (FIXTURES / "promptpin.lock").read_text("utf-8")
    pin = deploys.parse_lock(text)["production"]["greeter"]
    assert pin.digest == "" and pin.format == "" and len(pin.sha) == 40
    # Same pin shape, but pointing at a commit in a repo we control.
    release(prompt_repo, "classifier", "v1")
    deploys.deploy(prompt_repo, "classifier", "v1", "production", commit=False)
    lock = deploys.read_lock(prompt_repo)
    p = lock["production"]["classifier"]
    p.digest, p.format = "", ""
    deploys.write_lock(prompt_repo, lock)
    checks = deploys.verify_lock(prompt_repo)
    assert [c.level for c in checks] == ["warning"] and "redeploy" in checks[0].message
    deploys.deploy(prompt_repo, "classifier", "v1", "production", commit=False)
    assert deploys.verify_lock(prompt_repo) == []


def test_0_1_1_bundle_is_refused_with_a_plain_instruction():
    with pytest.raises(PromptIntegrityError, match="re-export the bundle"):
        PinnedPrompts(FIXTURES / "bundle")


def test_init_template_hash_is_unchanged_since_0_1_1(tmp_path):
    """Pins from 0.1.1 hash the template bytes; those bytes must still hash the same."""
    import hashlib

    template = (FIXTURES / "bundle" / "greeter.template.md").read_bytes()
    lock = deploys.parse_lock((FIXTURES / "promptpin.lock").read_text("utf-8"))
    want = lock["production"]["greeter"].template_sha256
    assert hashlib.sha256(template).hexdigest() == want


def test_old_serving_rows_without_digest_columns_are_migrated(tmp_path):
    db = tmp_path / "old.db"
    c = sqlite3.connect(db)
    c.execute(
        "CREATE TABLE serving (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,"
        " env TEXT NOT NULL, prompt TEXT NOT NULL, sha TEXT NOT NULL,"
        " model TEXT NOT NULL DEFAULT '', request_id TEXT NOT NULL DEFAULT '',"
        " latency_ms REAL, tokens_in INTEGER, tokens_out INTEGER,"
        " error TEXT NOT NULL DEFAULT '')"
    )
    c.execute("INSERT INTO serving (ts, env, prompt, sha) VALUES (1, 'production', 'p', 'abc')")
    c.commit()
    c.close()
    log_serve(db, prompt="p", sha="def", digest="d")  # adds the new columns in place
    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM serving").fetchone()[0] == 2
    c.close()


def test_history_cannot_be_hidden_by_a_hostile_author_name(prompt_repo):
    """An author name containing field separators / line splitters must not drop the commit."""
    import os
    import subprocess

    release(prompt_repo, "classifier", "v1")
    deploys.deploy(prompt_repo, "classifier", "v1", "staging", commit=False)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "evil\x1f \x85name",
        "GIT_COMMITTER_NAME": "evil",
    }
    subprocess.run(
        ["git", "add", "promptpin.lock"], cwd=prompt_repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "lock", "--", "promptpin.lock"],
        cwd=prompt_repo,
        check=True,
        capture_output=True,
        env=env,
    )
    events, problems = history.lock_history(prompt_repo)
    assert problems == [] and len(events) == 1
    assert events[0].commit_author.startswith("evil")


def test_non_canonical_base64_envelope_is_rejected(two_envs):
    """Same signed bytes, different file text (stray bits in the last base64 character)."""
    import base64

    for note in range(40):  # find a statement whose payload needs '=' padding
        st = attest.build_statement(two_envs, "staging")
        st["predicate"]["pad"] = "x" * note
        env = attest.sign(st, KEY)
        payload = env["payload"]
        if payload.endswith("="):
            break
    else:  # pragma: no cover
        pytest.fail("no padded payload found")
    raw = base64.b64decode(payload)
    last = payload.rstrip("=")[-1]
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    variants = [
        payload.replace(last + "=", c + "=")
        for c in alphabet
        if c != last and base64.b64decode(payload.replace(last + "=", c + "=")) == raw
    ]
    assert variants, "expected a non-canonical spelling of the same bytes"
    assert attest.open_envelope(env, KEY)  # the canonical form is accepted
    with pytest.raises(attest.AttestationError, match="non-canonical"):
        attest.open_envelope(dict(env, payload=variants[0]), KEY)


def test_editing_only_the_digest_field_in_the_lock_is_an_error(two_envs):
    lock = deploys.read_lock(two_envs)
    real = lock["staging"]["classifier"].digest
    text = (two_envs / "promptpin.lock").read_text("utf-8")
    (two_envs / "promptpin.lock").write_text(text.replace(real, "f" * 64), "utf-8")
    errs = [c.message for c in deploys.verify_lock(two_envs) if c.level == "error"]
    assert any("digest mismatch" in e for e in errs)


def test_parallel_deploys_from_separate_processes_all_commit(prompt_repo):
    """Before round 5 the commit ran outside the mutex and one process lost the git index.lock."""
    import subprocess
    import sys

    cfg = prompt_repo / "promptpin.yaml"
    cfg.write_text(
        "prompts_dir: prompts\nenvs: [dev, staging, production, e1, e2, e3]\n"
        "auto_commit_deploys: true\n",
        encoding="utf-8",
    )
    gitutil.commit_file(prompt_repo, "promptpin.yaml", "config")
    release(prompt_repo, "classifier", "v1")
    procs = [
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "promptpin",
                "--repo",
                str(prompt_repo),
                "deploy",
                "classifier@v1",
                "--env",
                e,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for e in ("dev", "staging", "production", "e1", "e2", "e3")
    ]
    results = [(p.wait(), p.stderr.read().decode()) for p in procs]
    assert [r for r in results if r[0] != 0] == []
    assert len(deploys.read_lock(prompt_repo)) == 6
    assert deploys.verify_lock(prompt_repo) == []
    assert "promptpin.lock" not in gitutil.status_porcelain(prompt_repo)  # nothing left uncommitted


def test_auto_commit_deploys_false_in_config_is_honoured_by_the_cli(prompt_repo):
    (prompt_repo / "promptpin.yaml").write_text(
        "prompts_dir: prompts\nenvs: [staging, production]\nauto_commit_deploys: false\n",
        encoding="utf-8",
    )
    gitutil.commit_file(prompt_repo, "promptpin.yaml", "config")
    release(prompt_repo, "classifier", "v1")
    args = ["--repo", str(prompt_repo)]
    assert cli.main([*args, "deploy", "classifier@v1", "--env", "staging"]) == 0
    assert "promptpin.lock" in gitutil.status_porcelain(prompt_repo)  # written, not committed
    assert cli.main([*args, "deploy", "classifier@v1", "--env", "production"]) == 0
    assert (
        cli.main([*args, "promote", "classifier", "--from", "staging", "--to", "production"]) == 0
    )
    assert gitutil._run(prompt_repo, "log", "--oneline", "--", "promptpin.lock").stdout == ""


def test_limit_must_be_positive(two_envs, capsys):
    for sub in (["history", "--limit", "0"], ["evidence", "--env", "staging", "--limit", "-1"]):
        assert cli.main(["--repo", str(two_envs), *sub]) == 1
        assert "--limit must be 1 or more" in capsys.readouterr().err


def test_evidence_signing_key_follows_attestation_key_rules(two_envs):
    with pytest.raises(PromptpinError):
        evidence.build(two_envs, "staging", key=b"k" * 40)


def test_editing_only_the_template_hash_field_in_the_lock_is_an_error(two_envs):
    pin = deploys.read_lock(two_envs)["staging"]["classifier"]
    text = (two_envs / "promptpin.lock").read_text("utf-8")
    (two_envs / "promptpin.lock").write_text(
        text.replace(pin.template_sha256, "0" * 64), encoding="utf-8"
    )
    errs = [c.message for c in deploys.verify_lock(two_envs) if c.level == "error"]
    assert any("template_sha256 mismatch" in e for e in errs)
