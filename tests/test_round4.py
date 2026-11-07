"""Round 4: first-hour behaviour of the installed package."""

from __future__ import annotations

import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

import promptpin
from promptpin import fsutil, gitutil
from promptpin.cli import _label, main
from promptpin.errors import VersionNotFoundError

ROOT = Path(__file__).resolve().parents[1]


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"promptpin {promptpin.__version__}"


def test_no_command_prints_help_and_exit_2(capsys):
    assert main([]) == 2
    assert "usage: promptpin" in capsys.readouterr().out


def test_label_strips_release_tag_prefix():
    assert _label("greeter", "promptpin/greeter@v1") == "greeter@v1"
    assert _label("greeter", "main") == "greeter@main"


def test_unencodable_console_does_not_crash(prompt_repo):
    """An ASCII or cp1252 console cannot print the arrow; promptpin must escape it, not crash."""
    env = {**os.environ, "PYTHONIOENCODING": "ascii"}
    for args in (["release", "classifier"],):
        subprocess.run(
            [sys.executable, "-m", "promptpin", "--repo", str(prompt_repo), *args],
            env=env,
            check=True,
            capture_output=True,
        )
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "promptpin",
            "--repo",
            str(prompt_repo),
            "diff",
            "classifier",
            "v1",
            "v1",
        ],
        env=env,
        capture_output=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert b"Traceback" not in proc.stderr
    assert b"\\u2192" in proc.stdout


def test_oserror_is_a_sentence_not_a_traceback(prompt_repo, capsys, monkeypatch):
    def boom(*a, **k):
        raise PermissionError(13, "Permission denied", "promptpin.yaml")

    monkeypatch.setattr(fsutil, "atomic_write_text", boom)
    assert main(["--repo", str(prompt_repo), "init"]) == 1
    err = capsys.readouterr().err
    assert "Permission denied (promptpin.yaml)" in err and "Traceback" not in err


def test_unexpected_error_is_internal_error_exit_70(prompt_repo, capsys, monkeypatch):
    from promptpin import cli

    monkeypatch.setattr(cli.registry, "list_prompts", lambda *a, **k: 1 / 0, raising=False)
    monkeypatch.setattr(cli, "cmd_list", lambda args: 1 / 0)
    parser = cli.build_parser()
    parser.set_defaults(func=cli.cmd_list)
    monkeypatch.setattr(cli, "build_parser", lambda: parser)
    assert main(["--repo", str(prompt_repo)]) == 70
    err = capsys.readouterr().err
    assert "internal error (ZeroDivisionError)" in err and "PROMPTPIN_DEBUG" in err


def test_init_writes_lf_and_keeps_existing_gitignore(repo):
    (repo / ".gitignore").write_bytes(b"*.pyc")  # no trailing newline
    assert main(["--repo", str(repo), "init"]) == 0
    assert (repo / ".gitignore").read_bytes() == b"*.pyc\n.promptpin/\n"
    assert b"\r" not in (repo / "promptpin.yaml").read_bytes()
    assert b"\r" not in (repo / "prompts" / "greeter.yaml").read_bytes()


def test_atomic_write_retries_permission_error_on_windows_only(tmp_path, monkeypatch):
    target = tmp_path / "f.txt"
    real = os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(13, "in use")
        real(src, dst)

    monkeypatch.setattr(fsutil.os, "replace", flaky)
    monkeypatch.setattr(fsutil, "_IS_WINDOWS", True)
    monkeypatch.setattr(fsutil.time, "sleep", lambda s: None)
    fsutil.atomic_write_text(target, "x")
    assert target.read_text() == "x" and calls["n"] == 3
    # On POSIX the error is not retried or swallowed.
    calls["n"] = 0
    monkeypatch.setattr(fsutil, "_IS_WINDOWS", False)
    with pytest.raises(PermissionError):
        fsutil.atomic_write_text(target, "y")
    assert calls["n"] == 1
    assert not list(tmp_path.glob(".f.txt.*.tmp"))


def test_release_without_git_identity_says_how_to_fix(prompt_repo, monkeypatch):
    for key in ("NAME", "EMAIL"):
        monkeypatch.delenv(f"GIT_COMMITTER_{key}", raising=False)
        monkeypatch.delenv(f"GIT_AUTHOR_{key}", raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    subprocess.run(["git", "config", "--unset", "user.name"], cwd=prompt_repo)
    subprocess.run(["git", "config", "--unset", "user.email"], cwd=prompt_repo)
    try:
        gitutil.create_tag(prompt_repo, "classifier", "v1", gitutil.head_sha(prompt_repo), "m")
    except VersionNotFoundError as exc:
        assert "git config user.name" in str(exc)
    else:  # a git that invents an identity: nothing to assert
        pytest.skip("git accepted an empty identity")


def test_js_verifier_command(tmp_path, capsys, monkeypatch):
    assert main(["js-verifier"]) == 0
    path = Path(capsys.readouterr().out.strip())
    assert path.name == "promptpin-verify.mjs" and path.is_file()
    out = tmp_path / "v.mjs"
    assert main(["js-verifier", "--out", str(out)]) == 0
    assert out.read_bytes() == path.read_bytes()
    capsys.readouterr()
    monkeypatch.chdir(tmp_path)
    assert main(["js-verifier", "--out", "rel.mjs"]) == 0
    assert "from './rel.mjs'" in capsys.readouterr().out


def test_metadata_is_consistent():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert f'version = "{promptpin.__version__}"' in text
    assert "Calas Automations Inc." in text
    assert "https://github.com/calasdan-sketch/promptpin" in text
    assert (ROOT / "LICENSE").read_text(encoding="utf-8").startswith("MIT License")
    assert (ROOT / "src" / "promptpin" / "py.typed").exists()
    assert f"## {promptpin.__version__}" in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


@pytest.mark.skipif(os.environ.get("PROMPTPIN_SKIP_BUILD") == "1", reason="build skipped")
def test_wheel_contents(tmp_path):
    pytest.importorskip("build")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "-o",
            str(tmp_path),
            str(ROOT),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        pytest.skip(f"cannot build offline here: {proc.stderr[-200:]}")
    wheel = next(tmp_path.glob("*.whl"))
    names = zipfile.ZipFile(wheel).namelist()
    assert "promptpin/py.typed" in names and "promptpin/promptpin-verify.mjs" in names
    assert any(n.endswith("/licenses/LICENSE") for n in names)
    assert not any(n.startswith(("tests", "benchmarks")) for n in names)
    meta = next(n for n in names if n.endswith("METADATA"))
    body = zipfile.ZipFile(wheel).read(meta).decode()
    assert "License-Expression: MIT" in body and "Project-URL: Source" in body
    assert "Author: Calas Automations Inc." in body or "Calas Automations Inc." in body


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "1e999"])
def test_eval_rejects_non_finite_scores(prompt_repo, capsys, value):
    assert main(["--repo", str(prompt_repo), "release", "classifier"]) == 0
    rc = main(
        [
            "--repo",
            str(prompt_repo),
            "eval",
            "record",
            "--prompt",
            "classifier",
            "--ref",
            "v1",
            "--suite",
            "s",
            "--dataset-sha",
            "d",
            "--metric",
            f"a={value}",
        ]
    )
    err = capsys.readouterr().err
    assert rc == 1 and "NaN and infinity" in err and "Traceback" not in err


def test_list_and_status_json(prompt_repo, capsys):
    import json

    repo = str(prompt_repo)
    assert main(["--repo", repo, "list", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["prompts"][0]["name"] == "classifier" and data["prompts"][0]["latest"] is None
    assert main(["--repo", repo, "status", "--json"]) == 0  # nothing deployed: still JSON
    assert json.loads(capsys.readouterr().out) == {"envs": {}, "issues": []}
    assert main(["--repo", repo, "release", "classifier"]) == 0
    assert main(["--repo", repo, "deploy", "classifier@v1", "--env", "staging"]) == 0
    capsys.readouterr()
    assert main(["--repo", repo, "status", "--json", "--env", "staging"]) == 0
    pin = json.loads(capsys.readouterr().out)["envs"]["staging"]["classifier"]
    assert pin["ref"].endswith("v1") and len(pin["sha"]) == 40 and pin["mutable_ref"] is False


def test_broken_pipe_exit_141():
    proc = subprocess.run(
        f'"{sys.executable}" -m promptpin --help | head -c 1', shell=True, capture_output=True
    )
    assert proc.returncode in (0, 141) and b"Traceback" not in proc.stderr


def test_all_digit_short_sha_is_a_sha_not_a_version(prompt_repo):
    """About 1 commit in 27 has a 7-digit-only short SHA; `deploy name@1234567` must still work."""
    import subprocess

    for i in range(400):
        (prompt_repo / "n.txt").write_text(str(i))
        subprocess.run(["git", "add", "n.txt"], cwd=prompt_repo, check=True)
        subprocess.run(["git", "commit", "-qm", f"c{i}"], cwd=prompt_repo, check=True)
        sha = gitutil.head_sha(prompt_repo)
        if sha[:7].isdigit():
            break
    else:
        pytest.skip("no all-digit short SHA in 400 commits")
    got, _, mutable = gitutil.resolve_prompt_ref(prompt_repo, "classifier", sha[:7])
    assert got == sha and not mutable
    # a short number that is neither a release tag nor a 7-digit SHA is still 'not found'
    with pytest.raises(VersionNotFoundError):
        gitutil.resolve_prompt_ref(prompt_repo, "classifier", "1234")
