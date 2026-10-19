"""End-to-end CLI: init → release → deploy → status → rollback."""

from promptpin import deploys
from promptpin.cli import main
from tests.conftest import release


def _run(args, repo, capsys=None):
    rc = main(["--repo", str(repo)] + args)
    out = capsys.readouterr().out if capsys else ""
    return rc, out


def test_init_scaffolds(repo, capsys):
    rc, out = _run(["init"], repo, capsys)
    assert rc == 0
    assert (repo / "promptpin.yaml").exists()
    assert (repo / "prompts" / "greeter.yaml").exists()
    assert "release greeter" in out


def test_full_workflow(prompt_repo, capsys):
    assert _run(["release", "classifier"], prompt_repo, capsys)[0] == 0
    rc, out = _run(
        ["deploy", "classifier@v1", "--env", "staging", "--note", "first"], prompt_repo, capsys
    )
    assert rc == 0
    assert "pinned classifier@promptpin/classifier@v1" in out
    assert "first deploy" in out or "previous: (none" in out

    rc, out = _run(["status", "--env", "staging"], prompt_repo, capsys)
    assert rc == 0
    assert "classifier" in out and "staging" in out

    rc, out = _run(["history", "--env", "staging"], prompt_repo, capsys)
    assert rc == 0
    assert "classifier" in out


def test_cli_list_and_versions(prompt_repo, capsys):
    release(prompt_repo, "classifier", "v1")
    rc, out = _run(["list"], prompt_repo, capsys)
    assert rc == 0 and "classifier" in out
    rc, out = _run(["versions", "classifier"], prompt_repo, capsys)
    assert rc == 0 and "v1" in out


def test_cli_show_renders_vars(prompt_repo, capsys):
    rc, out = _run(["show", "classifier", "--var", "ticket_text=broken login"], prompt_repo, capsys)
    assert rc == 0
    assert "broken login" in out


def test_cli_show_missing_var_errors(prompt_repo, capsys):
    rc, _ = _run(["show", "classifier"], prompt_repo, capsys)
    assert rc == 0  # show without --var just prints the raw template
    # Strict substitution: passing vars but missing a required one is an error,
    # so a typo'd variable name can't silently render a broken prompt.
    rc, _ = _run(["show", "classifier", "--var", "other=x"], prompt_repo, capsys)
    assert rc == 1


def test_cli_deploy_bad_spec_errors(prompt_repo, capsys):
    rc, _ = _run(["deploy", "classifier", "--env", "staging"], prompt_repo, capsys)
    assert rc == 1


def test_cli_rollback_flow(prompt_repo, capsys):
    from tests.conftest import CLASSIFIER_V2, commit_prompt

    release(prompt_repo, "classifier", "v1")
    from promptpin import gitutil as _gitutil

    v1_sha = _gitutil.head_sha(prompt_repo)  # before deploy auto-commits the lock
    _run(["deploy", "classifier@v1", "--env", "production"], prompt_repo, capsys)
    commit_prompt(prompt_repo, "classifier", CLASSIFIER_V2, "v2")
    release(prompt_repo, "classifier", "v2")
    _run(["deploy", "classifier@v2", "--env", "production"], prompt_repo, capsys)
    rc, out = _run(["rollback", "classifier", "--env", "production"], prompt_repo, capsys)
    assert rc == 0
    assert "rolled back" in out
    pins = deploys.read_lock(prompt_repo)
    assert pins["production"]["classifier"].sha == v1_sha  # back to v1
    # history should now show 3 entries
    rc, out = _run(["history", "--prompt", "classifier"], prompt_repo, capsys)
    assert out.count("production") >= 3


def test_cli_log_serve_and_stats(prompt_repo, capsys):
    rc, _ = _run(
        [
            "log",
            "serve",
            "--prompt",
            "classifier",
            "--sha",
            "abc123",
            "--env",
            "production",
            "--latency-ms",
            "42",
        ],
        prompt_repo,
        capsys,
    )
    assert rc == 0
    rc, out = _run(["log", "stats", "--prompt", "classifier"], prompt_repo, capsys)
    assert rc == 0
    assert "abc123" in out and "42.0ms" in out


def test_cli_log_prune(prompt_repo, capsys):
    _run(["log", "serve", "--prompt", "c", "--sha", "s"], prompt_repo, capsys)
    rc, out = _run(["log", "prune", "--older-than", "30d"], prompt_repo, capsys)
    assert rc == 0 and "pruned 0" in out


def test_cli_lock_export(prompt_repo, tmp_path, capsys):
    release(prompt_repo, "classifier", "v1")
    _run(["deploy", "classifier@v1", "--env", "production"], prompt_repo, capsys)
    out_file = tmp_path / "manifest.yaml"
    rc, out = _run(["lock", "--env", "production", "--out", str(out_file)], prompt_repo, capsys)
    assert rc == 0 and out_file.exists()
    assert "classifier" in out_file.read_text()


def test_cli_audit(prompt_repo, capsys):
    release(prompt_repo, "classifier", "v1")
    _run(["deploy", "classifier@v1", "--env", "production"], prompt_repo, capsys)
    rc, out = _run(["audit", "--prompt", "classifier"], prompt_repo, capsys)
    assert rc == 0
    assert "recent deploys" in out


def test_cli_validate(prompt_repo, capsys):
    rc, out = _run(["validate"], prompt_repo, capsys)
    assert rc == 0 and "all prompts valid" in out


def test_cli_diff_end_to_end(prompt_repo, capsys):
    from tests.conftest import CLASSIFIER_V2, commit_prompt

    release(prompt_repo, "classifier", "v1")
    commit_prompt(prompt_repo, "classifier", CLASSIFIER_V2, "v2")
    release(prompt_repo, "classifier", "v2")
    rc, out = _run(["diff", "classifier", "v1", "v2"], prompt_repo, capsys)
    assert rc == 0
    assert "=== template ===" in out
    assert "params.temperature" in out
