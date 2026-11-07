"""`log verify`: the serving log must only contain the pinned prompt."""

import sqlite3
import time

import pytest

from promptpin import PinnedPrompts, cli, deploys, log_serve
from promptpin.servecheck import verify_serving
from tests.conftest import release


@pytest.fixture()
def live(prompt_repo, tmp_path):
    release(prompt_repo, "classifier", "v1")
    deploys.deploy(prompt_repo, "classifier", "v1", "production", commit=False)
    pin = deploys.read_lock(prompt_repo)["production"]["classifier"]
    return prompt_repo, pin, tmp_path / "serve.db"


def _msgs(checks, level="error"):
    return [c.message for c in checks if c.level == level]


def _backdate(db, seconds=3600):
    c = sqlite3.connect(db)
    c.execute("UPDATE serving SET ts = ts + ?", (seconds,))
    c.commit()
    c.close()


def test_clean_log_passes(live):
    repo, pin, db = live
    for _ in range(3):
        log_serve(db, prompt="classifier", sha=pin.sha, digest=pin.digest)
    _backdate(db)
    assert verify_serving(repo, "production", db, grace_seconds=0) == []


def test_other_sha_is_error(live):
    repo, pin, db = live
    log_serve(db, prompt="classifier", sha=pin.sha, digest=pin.digest)
    log_serve(db, prompt="classifier", sha="a" * 40, digest="b" * 64)
    _backdate(db)
    errs = _msgs(verify_serving(repo, "production", db, grace_seconds=0))
    assert len(errs) == 1 and "aaaaaaa" in errs[0] and "1 of 2" in errs[0]


def test_same_sha_different_digest_is_error(live):
    repo, pin, db = live
    log_serve(db, prompt="classifier", sha=pin.sha, digest="c" * 64)
    _backdate(db)
    assert "digest" in _msgs(verify_serving(repo, "production", db, grace_seconds=0))[0]


def test_missing_digest_is_warning_only(live):
    repo, pin, db = live
    log_serve(db, prompt="classifier", sha=pin.sha)
    _backdate(db)
    checks = verify_serving(repo, "production", db, grace_seconds=0)
    assert not _msgs(checks) and "without a digest" in _msgs(checks, "warning")[0]


def test_old_pin_requests_inside_grace_ignored(live):
    repo, pin, db = live
    log_serve(db, prompt="classifier", sha="a" * 40)  # served just now: inside default grace
    checks = verify_serving(repo, "production", db)
    assert not _msgs(checks)


def test_unpinned_prompt_is_error(live):
    repo, pin, db = live
    log_serve(db, prompt="rogue", sha=pin.sha)
    log_serve(db, prompt="classifier", sha=pin.sha, digest=pin.digest)
    _backdate(db)
    assert any(
        "not pinned" in m for m in _msgs(verify_serving(repo, "production", db, grace_seconds=0))
    )


def test_fail_closed_setup_errors(live, tmp_path):
    repo, _, db = live
    assert "not found" in _msgs(verify_serving(repo, "production", db))[0]
    assert "no pins" in _msgs(verify_serving(repo, "staging", db))[0]
    bad = tmp_path / "bad.db"
    bad.write_text("not sqlite")
    assert _msgs(verify_serving(repo, "production", bad))
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    assert "no 'serving' table" in _msgs(verify_serving(repo, "production", empty))[0]


def test_old_database_without_digest_columns(live, tmp_path):
    repo, pin, _ = live
    db = tmp_path / "old.db"
    c = sqlite3.connect(db)
    c.execute(
        "CREATE TABLE serving (id INTEGER PRIMARY KEY, ts REAL, env TEXT, prompt TEXT, sha TEXT,"
        " model TEXT, request_id TEXT, latency_ms REAL, tokens_in INT, tokens_out INT, error TEXT)"
    )
    c.execute(
        "INSERT INTO serving (ts, env, prompt, sha, model, request_id, error)"
        " VALUES (?, 'production', 'classifier', ?, '', '', '')",
        (time.time() + 3600, pin.sha),
    )
    c.commit()
    c.close()
    checks = verify_serving(repo, "production", db, grace_seconds=0)
    assert not _msgs(checks)
    log_serve(db, prompt="classifier", sha=pin.sha)  # writer migrates the old schema


def test_receipt_log_end_to_end(live, tmp_path):
    repo, pin, db = live
    out = tmp_path / "b"
    deploys.export_bundle(repo, "production", out)
    _, receipt = PinnedPrompts(out).get("classifier").render(ticket_text="hi")
    receipt.log(db, request_id="r1")
    _backdate(db)
    assert verify_serving(repo, "production", db, grace_seconds=0) == []


def test_cli_exit_codes(live, capsys):
    repo, pin, db = live
    log_serve(db, prompt="classifier", sha="d" * 40)
    _backdate(db)
    argv = ["--repo", str(repo), "log", "verify", "--db", str(db), "--grace-seconds", "0"]
    assert cli.main(argv) == 1
    assert "FAILED" in capsys.readouterr().out
