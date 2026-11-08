"""Canonical form of a template, for *classifying* changes (never for pinning).

Pins and bundles always use the exact template bytes (``template_sha256``): the
model receives those bytes, so a pin must cover them. The canonical form only
answers a reviewer's question, "did this edit change anything but whitespace
noise?", so a CRLF conversion or an editor's trailing-space cleanup does not
look like a prompt change.

Canonicalisation is deliberately narrow and never touches visible text,
indentation, blank lines inside the template or any invisible/bidi character
(those must stay visible to review):

- line endings: CRLF -> LF (a lone CR is left alone; it is a validate error);
- trailing spaces and tabs at the end of each line are removed;
- trailing blank lines are collapsed to exactly one final newline (a text that ends in a
  lone CR is left ending in it; ``validate`` rejects such text anyway).
"""

from __future__ import annotations

import hashlib
import re

_TRAILING_WS = re.compile(r"[ \t]+$", re.MULTILINE)


def _normalise(text: str) -> str:
    return _TRAILING_WS.sub("", text.replace("\r\n", "\n"))


def canonical_template(text: str) -> str:
    out = _normalise(text)
    # Stripping spaces between a lone CR and a newline can create a new CRLF
    # (e.g. "\r \n"); repeat until stable so the result is a fixed point.
    # Every extra pass removes at least one character, so this terminates.
    while True:
        again = _normalise(out)
        if again == out:
            break
        out = again
    if not out.strip("\n"):
        return ""
    out = out.rstrip("\n")
    # A lone CR at the very end must not be glued to the final newline: that would
    # create a CRLF, which a second pass would then rewrite (not idempotent).
    return out if out.endswith("\r") else out + "\n"


def canonical_sha256(text: str) -> str:
    return hashlib.sha256(canonical_template(text).encode("utf-8")).hexdigest()


def classify_change(a, b) -> str:
    """How ``b`` differs from ``a`` (two ``Prompt`` objects).

    One of ``identical`` | ``cosmetic`` | ``config`` | ``wording`` |
    ``wording+config``. ``cosmetic`` means only line-end whitespace, line
    endings or trailing blank lines changed (config included: model, params,
    format identical).
    """
    config_same = (
        a.model == b.model and (a.params or {}) == (b.params or {}) and a.format == b.format
    )
    if a.template == b.template:
        return "identical" if config_same else "config"
    if canonical_template(a.template) == canonical_template(b.template):
        return "cosmetic" if config_same else "config"
    return "wording" if config_same else "wording+config"
