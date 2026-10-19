"""Validation: schema errors, variable consistency, secret heuristics."""

from promptpin import registry
from promptpin import validate as validatemod


def _prompt(**over):
    base = dict(name="p", template="Hello {{who}}.", variables=["who"])
    base.update(over)
    return registry.Prompt(**base)


def test_undeclared_variable_warns():
    findings = validatemod.validate_prompt(_prompt(variables=[]))
    assert any("not declared" in f.message for f in findings)
    assert all(f.level == "warning" for f in findings)


def test_unused_declared_variable_warns():
    findings = validatemod.validate_prompt(_prompt(template="Hi.", variables=["who"]))
    assert any("never uses" in f.message for f in findings)


def test_clean_prompt_no_findings():
    assert validatemod.validate_prompt(_prompt()) == []


def test_secret_patterns_warn_best_effort():
    p = _prompt(template="key = sk-abcdefghijklmnopqrstuvwx")
    findings = validatemod.validate_prompt(p)
    secret = [f for f in findings if "secret" in f.message]
    assert secret
    assert "best-effort" in secret[0].message


def test_private_key_block_warns():
    p = _prompt(template="-----BEGIN RSA PRIVATE KEY-----\nabc")
    findings = validatemod.validate_prompt(p)
    assert any("secret" in f.message for f in findings)


def test_validate_all_catches_schema_error(prompt_repo):
    (prompt_repo / "prompts" / "broken.yaml").write_text("name: broken\n", encoding="utf-8")
    findings = validatemod.validate_all(prompt_repo)
    errors = [f for f in findings if f.level == "error"]
    assert any("template" in e.message for e in errors)
    assert not validatemod.is_clean(findings)


def test_validate_all_clean_repo(prompt_repo):
    findings = validatemod.validate_all(prompt_repo)
    assert validatemod.is_clean(findings)


def test_validate_outside_repo_fails(tmp_path):
    import pytest

    from promptpin.errors import NotAGitRepoError

    with pytest.raises(NotAGitRepoError):
        validatemod.validate_all(tmp_path / "nope")
