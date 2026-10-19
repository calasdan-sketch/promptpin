"""Serving log: record, stats per version, prune, sampling, client library."""

import time

import pytest

from promptpin import statedb
from promptpin.client import log_serve, render_template, resolve


def _serve(conn, sha, **kw):
    kw.setdefault("env", "production")
    kw.setdefault("prompt", "classifier")
    kw.setdefault("model", "gpt-4o-mini")
    return statedb.record_serve(conn, sha=sha, **kw)


def test_record_serve_and_stats(prompt_repo):
    conn = statedb.connect(prompt_repo)
    try:
        _serve(conn, "aaa", latency_ms=100.0)
        _serve(conn, "aaa", latency_ms=200.0)
        _serve(conn, "bbb", latency_ms=50.0, error="timeout")
        stats = statedb.serving_stats(conn, prompt="classifier")
    finally:
        conn.close()
    assert stats["total"] == 3
    by_sha = {v["sha"]: v for v in stats["versions"]}
    assert by_sha["aaa"]["count"] == 2
    assert by_sha["aaa"]["avg_latency_ms"] == pytest.approx(150.0)
    assert by_sha["aaa"]["share"] == pytest.approx(2 / 3)
    assert by_sha["bbb"]["error_rate"] == pytest.approx(1.0)


def test_stats_filtered_by_env_and_since(prompt_repo):
    conn = statedb.connect(prompt_repo)
    try:
        _serve(conn, "aaa", env="staging")
        _serve(conn, "aaa", env="production")
        prod = statedb.serving_stats(conn, env="production")
        assert prod["total"] == 1
        future = statedb.serving_stats(conn, since_ts=time.time() + 3600)
        assert future["total"] == 0
    finally:
        conn.close()


def test_prune_removes_old_events(prompt_repo):
    conn = statedb.connect(prompt_repo)
    try:
        _serve(conn, "aaa")
        old_ts = time.time() - 40 * 86400
        conn.execute("UPDATE serving SET ts = ?", (old_ts,))
        conn.commit()
        n = statedb.prune_serving(conn, time.time() - 30 * 86400)
        assert n == 1
        assert statedb.serving_stats(conn)["total"] == 0
    finally:
        conn.close()


def test_client_log_serve_writes_db(tmp_path):
    db = tmp_path / "sub" / "state.db"
    assert (
        log_serve(
            db, prompt="c", sha="abc123", env="staging", model="m", request_id="r1", latency_ms=12.5
        )
        is True
    )
    import sqlite3

    conn = sqlite3.connect(db)
    row = conn.execute("SELECT * FROM serving").fetchone()
    conn.close()
    assert row is not None


def test_client_log_serve_sampling_drops():
    assert log_serve(":memory:", prompt="c", sha="s", sample_rate=0.0) is False


def test_client_log_serve_bad_sample_rate(tmp_path):
    with pytest.raises(ValueError):
        log_serve(tmp_path / "x.db", prompt="c", sha="s", sample_rate=1.5)


def test_render_template_substitutes():
    out = render_template("Hello {{name}}, you are {{age}}.", name="Dan", age=40)
    assert out == "Hello Dan, you are 40."


def test_render_template_missing_var_raises():
    with pytest.raises(KeyError, match="name"):
        render_template("Hello {{name}}.")


def test_render_template_whitespace_tolerant():
    assert render_template("{{  name  }}!", name="x") == "x!"


def test_resolve_reads_lock_file(prompt_repo, tmp_path):
    import yaml

    lock = tmp_path / "test.lock"
    lock.write_text(
        yaml.safe_dump(
            {
                "envs": {
                    "production": {
                        "classifier": {
                            "ref": "v1",
                            "sha": "abc",
                            "model": "m",
                            "params": {"t": 0.2},
                        }
                    }
                }
            }
        )
    )
    pin = resolve(lock, env="production", prompt="classifier")
    assert pin["sha"] == "abc"
    assert pin["params"] == {"t": 0.2}


def test_resolve_missing_pin_raises(tmp_path):
    import yaml

    lock = tmp_path / "test.lock"
    lock.write_text(yaml.safe_dump({"envs": {}}))
    with pytest.raises(KeyError):
        resolve(lock, env="production", prompt="nope")


def test_resolve_accepts_exported_manifest(prompt_repo, tmp_path):
    """`promptpin lock --env E --out` writes a minimal manifest — resolve()
    must read it, not just the committed promptpin.lock."""
    from promptpin import deploys
    from tests.conftest import release

    release(prompt_repo, "classifier", "v1")
    deploys.deploy(prompt_repo, "classifier", "v1", "production", commit=False)
    out = tmp_path / "manifest.yaml"
    from promptpin.cli import main as cli_main

    assert (
        cli_main(["--repo", str(prompt_repo), "lock", "--env", "production", "--out", str(out)])
        == 0
    )
    pin = resolve(out, "production", "classifier")
    assert pin["sha"]
    assert pin["model"] == "gpt-4o-mini"
    assert "template_sha256" in pin
    with pytest.raises(KeyError):
        resolve(out, "staging", "classifier")
    with pytest.raises(KeyError):
        resolve(out, "production", "nope")
