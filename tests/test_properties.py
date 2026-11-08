"""Property-based and differential tests (Hypothesis; skipped when it is not installed)."""

from __future__ import annotations

import base64
import json

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from promptpin import attest, canon, client, evidence, safety  # noqa: E402

SETTINGS = settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
KEY = bytes(range(33, 65))
# Text with the awkward characters on purpose: CR, tabs, bidi, zero-width, tag chars, controls.
NASTY = st.text(
    alphabet=st.sampled_from(list("ab {}\t\r\n‮​­\U000e0041\x1b\x07 \u0085️‍")),
    max_size=60,
)
ANY_TEXT = st.one_of(NASTY, st.text(max_size=80))


def _reference_canonical(text: str) -> str:
    """Independent line-based statement of canon.py's rules (differential oracle)."""
    body = text
    while True:  # same fixed-point rule, written the other way round (split into lines)
        lines = [ln.rstrip(" \t") for ln in body.replace("\r\n", "\n").split("\n")]
        again = "\n".join(lines)
        if again == body:
            break
        body = again
    if not body.strip("\n"):
        return ""
    body = body.rstrip("\n")
    return body if body.endswith("\r") else body + "\n"


@SETTINGS
@given(ANY_TEXT)
def test_canonical_matches_reference_implementation(text):
    assert canon.canonical_template(text) == _reference_canonical(text)


@SETTINGS
@given(ANY_TEXT)
def test_canonical_is_idempotent(text):
    once = canon.canonical_template(text)
    assert canon.canonical_template(once) == once


@SETTINGS
@given(ANY_TEXT)
def test_canonical_never_hides_invisible_characters(text):
    """Cosmetic canonicalisation must not remove bidi/zero-width/tag characters."""
    before = [h.codepoint for h in safety.find_hidden(text) if h.kind in ("bidi", "tag")]
    after = [
        h.codepoint
        for h in safety.find_hidden(canon.canonical_template(text))
        if h.kind in ("bidi", "tag")
    ]
    assert before == after


@SETTINGS
@given(ANY_TEXT, st.integers(0, 3), st.integers(0, 3))
def test_crlf_and_trailing_space_noise_is_cosmetic(text, spaces, newlines):
    text = text.replace("\r", "")
    noisy = "\r\n".join(ln + " " * spaces for ln in text.split("\n")) + "\n" * newlines
    assert canon.canonical_sha256(noisy) == canon.canonical_sha256(text)


@SETTINGS
@given(ANY_TEXT)
def test_sanitize_output_is_terminal_safe_and_idempotent(text):
    out = safety.sanitize(text)
    assert "\r" not in out and "\x1b" not in out and "‮" not in out
    assert all(not safety.classify(c) or ord(c) in safety.PRESENTATION_SELECTORS for c in out)
    assert safety.sanitize(out) == out
    one = safety.one_line(text)
    assert "\n" not in one and "\r" not in one


@SETTINGS
@given(ANY_TEXT, ANY_TEXT)
def test_variable_values_cannot_forge_chat_roles(user_a, user_b):
    template = "system:\nBe kind.\n\nuser:\n{{a}} and {{b}}\n"
    base = client.render_messages(template, a="x", b="y")
    got = client.render_messages(template, a=user_a, b=user_b)
    assert [m["role"] for m in got] == [m["role"] for m in base] == ["system", "user"]


@SETTINGS
@given(
    st.dictionaries(st.text(max_size=8), st.one_of(st.integers(), st.text(max_size=10)), max_size=5)
)
def test_attestation_roundtrip(extra):
    statement = {
        "_type": attest.STATEMENT_TYPE,
        "predicateType": attest.PREDICATE_TYPE,
        "predicate": {"env": "production", "extra": extra},
    }
    env = attest.sign(statement, KEY)
    assert attest.open_envelope(env, KEY) == statement


@SETTINGS
@given(st.data())
def test_any_single_character_change_to_an_envelope_is_rejected(data):
    statement = {
        "_type": attest.STATEMENT_TYPE,
        "predicateType": attest.PREDICATE_TYPE,
        "predicate": {"env": "production", "prompts": {"a": {"sha": "1" * 40}}},
    }
    env = attest.sign(statement, KEY)
    field = data.draw(st.sampled_from(["payload", "sig"]))
    text = env["payload"] if field == "payload" else env["signatures"][0]["sig"]
    i = data.draw(st.integers(0, len(text) - 1))
    repl = data.draw(
        st.sampled_from([c for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef0123/+" if c != text[i]])
    )
    changed = text[:i] + repl + text[i + 1 :]
    bad = json.loads(json.dumps(env))
    if field == "payload":
        bad["payload"] = changed
    else:
        bad["signatures"][0]["sig"] = changed
    try:
        base64.b64decode(changed, validate=True)
    except ValueError:
        pass
    # Either the base64 is malformed or the MAC fails: both are AttestationError, never a pass.
    with pytest.raises(attest.AttestationError):
        attest.open_envelope(bad, KEY)


JSON_SCALARS = st.one_of(st.none(), st.booleans(), st.integers(-10, 10), st.text(max_size=8))


@SETTINGS
@given(
    st.dictionaries(st.text(min_size=1, max_size=6), JSON_SCALARS, min_size=1, max_size=5),
    st.data(),
)
def test_evidence_seal_detects_any_edited_value(body, data):
    body = {k: v for k, v in body.items() if k not in ("schema", "report_sha256", "hmac_sha256")}
    body["schema"] = evidence.SCHEMA
    report = evidence.seal(body, KEY)
    assert evidence.check(report, KEY) == []
    key = data.draw(st.sampled_from(sorted(k for k in body if k != "schema")), label="field")
    edited = dict(report)
    old = edited[key]
    edited[key] = "changed" if old != "changed" else "changed2"
    assert evidence.check(edited), "an edited value must be detected"
    assert evidence.check(edited, KEY)
