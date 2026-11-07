"""Canonical form: cosmetic edits are recognised, real edits never are."""

import pytest

from promptpin.canon import canonical_template, classify_change
from promptpin.registry import parse_yaml_text

BASE = "name: p\nmodel: m\nparams:\n  temperature: 0.2\ntemplate: |\n  Hello {{x}}\n  Bye\n"


def _p(src):
    return parse_yaml_text(src, "p.yaml")


@pytest.mark.parametrize(
    "a,b",
    [
        ("a\nb\n", "a\r\nb\r\n"),
        ("a\nb\n", "a  \nb\t\n"),
        ("a\nb\n", "a\nb\n\n\n"),
        ("a\nb", "a\nb\n"),
    ],
)
def test_cosmetic_variants_share_canonical_form(a, b):
    assert canonical_template(a) == canonical_template(b)


@pytest.mark.parametrize(
    "a,b",
    [
        ("a\nb\n", "a\nc\n"),
        ("a\nb\n", "a\n\nb\n"),  # inner blank line is content
        ("a\n  b\n", "a\nb\n"),  # indentation is content
        ("a b\n", "a  b\n"),  # inner spacing is content
        ("a\n", "a​\n"),  # invisible chars never normalised away
        ("a\n", "a‮\n"),
        ("a\n", "a\r\n\rb\n"),
    ],
)
def test_real_edits_stay_distinct(a, b):
    assert canonical_template(a) != canonical_template(b)


def test_classify():
    base = _p(BASE)
    assert classify_change(base, _p(BASE)) == "identical"
    assert classify_change(base, _p(BASE.replace("Bye", "Bye   "))) == "cosmetic"
    assert classify_change(base, _p(BASE.replace("Bye", "Ciao"))) == "wording"
    assert classify_change(base, _p(BASE.replace("0.2", "0.9"))) == "config"
    both = BASE.replace("Bye", "Ciao").replace("0.2", "0.9")
    assert classify_change(base, _p(both)) == "wording+config"
    # whitespace edit AND a settings edit must not be reported as cosmetic
    assert classify_change(base, _p(BASE.replace("Bye", "Bye ").replace("0.2", "0.9"))) == "config"


def test_empty_template_canonical():
    assert canonical_template("\n\n") == ""
