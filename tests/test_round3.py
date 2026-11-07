"""Round 3: availability, hostile input and performance fixes."""

from __future__ import annotations

import os
import sqlite3
import threading
import time

import pytest

from promptpin import (
    PinnedPrompts,
    PromptIntegrityError,
    deploys,
    log_serve,
    registry,
    safety,
    validate,
    yamlsafe,
)
from tests.conftest import release


@pytest.fixture()
def bundle(prompt_repo, tmp_path):
    release(prompt_repo, "classifier", "v1")
    deploys.deploy(prompt_repo, "classifier", "v1", "production", commit=False)
    out = tmp_path / "bundle"
    result = deploys.export_bundle(prompt_repo, "production", out)
    return out, result["bundle_digest"]


# -- failure policy covers every failure, not only ValueError ------------------


@pytest.mark.parametrize(
    "garbage", ["prompts: [unclosed\n  - : :\n", "[" * 5000 + "]" * 5000, "\x00\x01"]
)
def test_last_good_survives_corrupt_manifest(bundle, garbage):
    out, _ = bundle
    events = []
    pp = PinnedPrompts(
        out, on_failure="last_good", reverify_interval=0, on_event=lambda k, d: events.append(k)
    )
    (out / "manifest.yaml").write_text(garbage, encoding="utf-8")
    served = pp.get("classifier")  # must not raise
    assert served.receipt.verified is False
    assert "verify_failed" in events


def test_raise_policy_reports_corrupt_manifest_as_integrity_error(bundle):
    out, _ = bundle
    pp = PinnedPrompts(out, reverify_interval=0)
    (out / "manifest.yaml").write_text("[" * 5000, encoding="utf-8")
    with pytest.raises(PromptIntegrityError):
        pp.get("classifier")


def test_startup_with_corrupt_manifest_raises(bundle):
    out, _ = bundle
    (out / "manifest.yaml").write_text("a: [", encoding="utf-8")
    with pytest.raises(PromptIntegrityError):
        PinnedPrompts(out, on_failure="last_good")


def test_transient_mismatch_during_replacement_is_retried(bundle):
    out, _ = bundle
    pp = PinnedPrompts(out, on_failure="last_good", reverify_interval=0)
    manifest = (out / "manifest.yaml").read_bytes()
    (out / "manifest.yaml").write_bytes(b"prompts: {}\n")

    def restore():
        time.sleep(0.02)
        (out / "manifest.yaml").write_bytes(manifest)

    t = threading.Thread(target=restore)
    t.start()
    served = pp.get("classifier")
    t.join()
    assert served.receipt.verified is True


def test_get_is_not_blocked_by_a_slow_reverify(bundle, monkeypatch):
    out, _ = bundle
    pp = PinnedPrompts(out, reverify_interval=0)
    gate = threading.Event()
    real = pp._read

    def slow():
        gate.wait(5)
        return real()

    monkeypatch.setattr(pp, "_read", slow)
    first = threading.Thread(target=pp.get, args=("classifier",))
    first.start()
    time.sleep(0.1)  # first holds the reload
    start = time.monotonic()
    served = pp.get("classifier")  # served from the last verified copy, no waiting
    assert time.monotonic() - start < 1.0
    assert served.receipt.verified
    gate.set()
    first.join()


def test_render_shortcut_and_value_scan(bundle):
    out, _ = bundle
    pp = PinnedPrompts(out)
    text, receipt = pp.render("classifier", ticket_text="hi")
    assert "hi" in text and receipt.rendered_sha256
    evil = "ok‮\U000e0041"
    text, _ = pp.render("classifier", ticket_text=evil)  # default: unchanged behaviour
    assert evil in text
    with pytest.raises(ValueError, match="ticket_text"):
        pp.render("classifier", ticket_text=evil, scan_values="error")
    with pytest.raises(ValueError):
        pp.render("classifier", ticket_text="x", scan_values="maybe")
    pp.render("classifier", ticket_text="plain café ❤️", scan_values="error")


# -- serving log -----------------------------------------------------------------


def test_log_serve_concurrent_writers_do_not_fail(tmp_path):
    db = tmp_path / "state" / "s.db"
    errors = []

    def work(n):
        for i in range(60):
            try:
                log_serve(db, prompt="p", sha="a" * 40, request_id=f"{n}-{i}")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM serving").fetchone()[0] == 480


def test_log_serve_is_fast_after_first_call(tmp_path):
    db = tmp_path / "s.db"
    log_serve(db, prompt="p", sha="a" * 40)
    start = time.perf_counter()
    for _ in range(200):
        log_serve(db, prompt="p", sha="a" * 40)
    per_call = (time.perf_counter() - start) / 200
    assert per_call < 0.01  # was ~25 ms when every call opened and closed a connection


@pytest.mark.skipif(os.name == "nt", reason="cannot delete an open database on Windows")
def test_log_serve_notices_a_replaced_database(tmp_path):
    db = tmp_path / "s.db"
    log_serve(db, prompt="p", sha="a" * 40)
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(str(db) + suffix)
        except FileNotFoundError:
            pass
    assert log_serve(db, prompt="p", sha="b" * 40) is True
    assert sqlite3.connect(db).execute("SELECT sha FROM serving").fetchall() == [("b" * 40,)]


def test_log_serve_on_error(tmp_path):
    bad = tmp_path / "file"
    bad.write_text("x")
    with pytest.raises((OSError, sqlite3.Error)):
        log_serve(bad / "s.db", prompt="p", sha="a" * 40)
    assert log_serve(bad / "s.db", prompt="p", sha="a" * 40, on_error="ignore") is False
    with pytest.raises(ValueError):
        log_serve(tmp_path / "s.db", prompt="p", sha="a", on_error="nope")


# -- hostile input ---------------------------------------------------------------


def test_deeply_nested_yaml_is_a_yaml_error_not_a_crash():
    import yaml

    with pytest.raises(yaml.YAMLError):
        yamlsafe.load("[" * 4000 + "]" * 4000)


def test_validate_reports_nested_and_oversized_prompts(prompt_repo):
    (prompt_repo / "prompts" / "deep.yaml").write_text(
        "name: deep\nparams: " + "[" * 4000 + "]" * 4000 + "\ntemplate: hi\n", encoding="utf-8"
    )
    (prompt_repo / "prompts" / "huge.yaml").write_text(
        "name: huge\ntemplate: |\n  " + "x" * (yamlsafe.MAX_FILE_BYTES + 10) + "\n",
        encoding="utf-8",
    )
    findings = validate.validate_all(prompt_repo)
    text = "\n".join(str(f) for f in findings)
    assert "[error] deep" in text and "nested too deeply" in text
    assert "[error] huge" in text and "limit" in text


def test_find_hidden_fast_path_matches_reference():
    def reference(text):
        out = []
        for i, ch in enumerate(text):
            hit = safety.classify(ch)
            if hit and not (ch == "\r"):
                out.append((i, ord(ch)))
        return out

    samples = [
        "plain ascii\twith\ttabs\n",
        "a‮b​c\U000e0041d\x1b[2J",
        "café ❤️ \U0001f468‍\U0001f469",
        "あいう " * 50,
    ]
    for s in samples:
        got = [(h.index, h.codepoint) for h in safety.find_hidden(s) if h.codepoint != 0xFE0F]
        want = [(i, c) for i, c in reference(s) if c != 0xFE0F]
        assert [g for g in got if g[1] != 0x200D] == [w for w in want if w[1] != 0x200D]


def test_find_hidden_scales_linearly():
    start = time.perf_counter()
    assert safety.find_hidden("x " * 1_000_000) == []
    assert time.perf_counter() - start < 2.0


def test_homoglyph_word_is_flagged():
    word = "pаssword"  # Cyrillic a
    assert safety.find_mixed_script(f"say {word} now") == [(4, word)]
    assert safety.find_mixed_script("password привет café") == []
    assert safety.find_mixed_script("plain ascii") == []


def test_validate_flags_homoglyph_bad_placeholder_and_nan():
    p = registry.parse_yaml_text(
        'name: x\nparams: {t: .nan}\ntemplate: "p\\u0430ss {{a.b}} {{ok}}"\nvariables: [ok]\n',
        "x.yaml",
    )
    msgs = [str(f) for f in validate.validate_prompt(p)]
    assert any("mixes Latin" in m and "\\u0430" in m for m in msgs)
    assert any("{{a.b}}" in m and "not a valid placeholder" in m for m in msgs)
    assert any(m.startswith("[error]") and "NaN" in m for m in msgs)
    assert not any("{{ok}}" in m for m in msgs)


# -- file writes -----------------------------------------------------------------


def test_lock_and_bundle_are_written_with_lf_and_keep_mode(prompt_repo, tmp_path):
    release(prompt_repo, "classifier", "v1")
    deploys.deploy(prompt_repo, "classifier", "v1", "production", commit=False)
    assert b"\r" not in (prompt_repo / "promptpin.lock").read_bytes()
    out = tmp_path / "b"
    deploys.export_bundle(prompt_repo, "production", out)
    deploys.export_bundle(prompt_repo, "production", out)  # overwrite is atomic and idempotent
    assert not [p for p in out.iterdir() if p.name.endswith(".tmp")]
    assert b"\r" not in (out / "manifest.yaml").read_bytes()


def test_unreadable_lock_is_a_deploy_error_not_a_traceback(prompt_repo):
    (prompt_repo / "promptpin.lock").write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(deploys.DeployError):
        deploys.read_lock(prompt_repo)


# -- CI annotations --------------------------------------------------------------


def test_github_annotations_are_encoded_and_fail_the_build(prompt_repo, capsys):
    from promptpin.cli import main

    (prompt_repo / "prompts" / "bad.yaml").write_text(
        'name: bad\nmodel: m\ntemplate: "hi\\u202e %\\x1b[2J {{a-b}}"\n', encoding="utf-8"
    )
    rc = main(["--repo", str(prompt_repo), "validate", "--github"])
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.startswith("::")]
    assert rc == 1
    assert any(ln.startswith("::error title=promptpin validate%3A bad::") for ln in lines)
    assert all("\x1b" not in ln and "‮" not in ln for ln in out.splitlines())
    assert all("\n" not in ln for ln in lines)
    # a clean repo prints no annotations and exits 0
    (prompt_repo / "prompts" / "bad.yaml").unlink()
    assert main(["--repo", str(prompt_repo), "validate", "--github"]) == 0


def test_github_annotation_strict_promotes_warnings(prompt_repo, capsys):
    from promptpin.cli import main

    (prompt_repo / "prompts" / "warn.yaml").write_text(
        "name: warn\nmodel: m\ntemplate: 'x {{undeclared}}'\n", encoding="utf-8"
    )
    assert main(["--repo", str(prompt_repo), "validate", "--github"]) == 0
    assert capsys.readouterr().out.startswith("::warning")
    assert main(["--repo", str(prompt_repo), "validate", "--github", "--strict"]) == 1
    assert capsys.readouterr().out.startswith("::error")


def test_verify_bundle_warns_about_unlisted_template_files(prompt_repo, tmp_path):
    release(prompt_repo, "classifier", "v1")
    deploys.deploy(prompt_repo, "classifier", "v1", "production", commit=False)
    out = tmp_path / "b"
    deploys.export_bundle(prompt_repo, "production", out)
    (out / "old.template.md").write_text("unverified text", encoding="utf-8")
    checks = deploys.verify_lock(prompt_repo, env="production", bundle_dir=out)
    assert [c.level for c in checks] == ["warning"]
    assert "old.template.md" in checks[0].message


def test_raise_policy_serves_nothing_to_concurrent_callers_after_a_failure(bundle):
    out, _ = bundle
    pp = PinnedPrompts(out, reverify_interval=0)
    (out / "manifest.yaml").write_text("not: [valid", encoding="utf-8")
    with pytest.raises(PromptIntegrityError):
        pp.get("classifier")
    pp._reloading = True  # pretend another thread is already re-checking
    with pytest.raises(PromptIntegrityError):
        pp.get("classifier")


def test_find_hidden_keeps_lone_carriage_return_rule():
    assert [h.codepoint for h in safety.find_hidden("a\rb")] == [13]
    assert safety.find_hidden("a\r\nb") == []
