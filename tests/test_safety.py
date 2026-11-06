"""Hidden characters, terminal-safe output, fail-closed validation."""

import json

from promptpin import registry, safety
from promptpin import validate as validatemod
from promptpin.cli import main
from tests.conftest import _git, release

RLO = "\u202e"
ZWSP = "\u200b"
ZWJ = "\u200d"
ESC = "\x1b"
TAG_A = chr(0xE0041)  # invisible "A" from the Unicode tag block


def _run(args, repo, capsys):
    rc = main(["--repo", str(repo)] + args)
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def _prompt(template, **over):
    base = dict(name="p", template=template, variables=[])
    base.update(over)
    return registry.Prompt(**base)


# ------------------------------------------------------------- detection ---


def test_find_hidden_classifies_each_kind():
    text = f"a{RLO}b{ZWSP}c{ESC}d{TAG_A}e{ZWJ}f\t\n\r"
    kinds = {(h.kind, h.severity) for h in safety.find_hidden(text)}
    assert kinds == {
        ("bidi", "error"),
        ("zero-width", "error"),
        ("control", "error"),
        ("tag", "error"),
        ("joiner", "warning"),
    }


def test_plain_text_and_emoji_are_clean():
    assert safety.find_hidden("Hello, wörld! 日本語 🙂\tok\n") == []


def test_sanitize_makes_everything_visible():
    out = safety.sanitize(f"x{ESC}[2Jy{RLO}z{TAG_A}\rw\n")
    assert ESC not in out and RLO not in out and TAG_A not in out and "\r" not in out
    assert "\\u001b[2J" in out and "\\u202e" in out and "\\U000e0041" in out
    assert out.endswith("\n")  # newlines kept by default


def test_one_line_escapes_newlines():
    assert safety.one_line("a\nb") == "a\\u000ab"


# ------------------------------------------------------------- validation ---


def test_validate_bidi_override_is_error():
    findings = validatemod.validate_prompt(_prompt(f"Approve {RLO}refund{RLO}"))
    hidden = [f for f in findings if "invisible" in f.message]
    assert hidden and hidden[0].level == "error"
    assert "U+202E" in hidden[0].message and "line 1" in hidden[0].message


def test_validate_tag_smuggling_is_error():
    findings = validatemod.validate_prompt(_prompt("hello" + TAG_A * 5))
    assert any(f.level == "error" and "tag" in f.message for f in findings)


def test_validate_joiner_is_only_warning():
    findings = validatemod.validate_prompt(_prompt(f"family 👨{ZWJ}👩"))
    assert findings and all(f.level == "warning" for f in findings)


def test_validate_scans_description_and_params():
    p = _prompt("ok", description=f"d{ZWSP}", params={"stop": f"x{ESC}"})
    msgs = " ".join(f.message for f in validatemod.validate_prompt(p))
    assert "description" in msgs and "params.stop" in msgs


def test_validate_zero_prompts_fails_closed(repo, capsys):
    findings = validatemod.validate_all(repo)
    assert not validatemod.is_clean(findings)
    rc, out, _ = _run(["validate"], repo, capsys)
    assert rc == 1 and "no prompts found" in out


def test_validate_strict_fails_on_warnings(prompt_repo, capsys):
    (prompt_repo / "prompts" / "w.yaml").write_text(
        "name: w\nvariables: [unused]\ntemplate: hi\n", encoding="utf-8"
    )
    assert _run(["validate"], prompt_repo, capsys)[0] == 0
    rc, out, _ = _run(["validate", "--strict"], prompt_repo, capsys)
    assert rc == 1 and "--strict" in out


def test_validate_json_output(prompt_repo, capsys):
    (prompt_repo / "prompts" / "h.yaml").write_text(
        'name: h\ntemplate: "hi \\u202e"\n', encoding="utf-8"
    )
    rc, out, _ = _run(["validate", "--json"], prompt_repo, capsys)
    data = json.loads(out)
    assert rc == 1 and data["ok"] is False
    assert any(f["prompt"] == "h" and f["level"] == "error" for f in data["findings"])


def test_validate_non_utf8_file_is_error_not_crash(prompt_repo, capsys):
    (prompt_repo / "prompts" / "bin.yaml").write_bytes(b"name: bin\ntemplate: \xff\xfe\n")
    rc, out, _ = _run(["validate"], prompt_repo, capsys)
    assert rc == 1 and "bin" in out


# --------------------------------------------------------- output safety ---


def test_show_escapes_terminal_sequences(prompt_repo, capsys):
    (prompt_repo / "prompts" / "evil.yaml").write_text(
        'name: evil\ntemplate: "hi \\e[2J\\e]0;pwned\\a there"\n', encoding="utf-8"
    )
    _git(prompt_repo, "add", "-A")
    _git(prompt_repo, "commit", "-m", "evil")
    rc, out, _ = _run(["show", "evil"], prompt_repo, capsys)
    assert rc == 0
    assert ESC not in out and "\x07" not in out
    assert "\\u001b[2J" in out


def test_history_escapes_note(prompt_repo, capsys):
    release(prompt_repo, "classifier", "v1")
    note = f"ok{ESC}[1A{ESC}[2Kfaked line\nsecond"
    _run(["deploy", "classifier@v1", "--env", "staging", "--note", note], prompt_repo, capsys)
    rc, out, _ = _run(["history"], prompt_repo, capsys)
    assert rc == 0 and ESC not in out
    assert "\\u000asecond" in out  # a note cannot forge a new output line


def test_diff_reveals_added_hidden_characters(prompt_repo, capsys):
    from tests.conftest import CLASSIFIER_V1, commit_prompt

    release(prompt_repo, "classifier", "v1")
    # YAML block scalars don't process escapes, so write the raw character.
    commit_prompt(prompt_repo, "classifier", CLASSIFIER_V1.replace("bug,", f"bug,{RLO}"), "sneaky")
    release(prompt_repo, "classifier", "v2")
    rc, out, _ = _run(["diff", "classifier", "v1", "v2"], prompt_repo, capsys)
    assert rc == 0
    assert RLO not in out
    assert "adds 1 invisible character" in out and "\\u202e" in out


def test_error_messages_are_sanitized(prompt_repo, capsys):
    rc, _, err = _run(["deploy", f"classifier@x{ESC}[2J", "--env", "dev"], prompt_repo, capsys)
    assert rc == 1 and ESC not in err
