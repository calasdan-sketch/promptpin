"""Hidden-character detection and terminal-safe output.

Two jobs:

1. **Find invisible characters in prompts.** Bidi overrides ("Trojan
   Source", CVE-2021-42574), zero-width characters and Unicode *tag*
   characters (U+E0000-E007F, used for "ASCII smuggling" prompt injection)
   render as nothing — or reorder text — in editors, diffs and code review,
   but a model still reads them. A reviewer approving a prompt PR cannot see
   them. ``find_hidden`` reports each one with its code point.

2. **Never let untrusted text drive the terminal.** Prompt templates, deploy
   notes, commit messages and author names are attacker-controllable. Before
   promptpin prints any of them, ``sanitize`` replaces control, bidi,
   zero-width and tag characters with a visible ``\\u....`` escape, so an
   escape sequence in a prompt cannot clear your screen, rewrite earlier
   lines or hide a diff hunk.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

#: Bidirectional formatting characters (Trojan Source).
BIDI_CHARS = frozenset(
    [0x061C, 0x200E, 0x200F]
    + list(range(0x202A, 0x202F))  # LRE RLE PDF LRO RLO
    + list(range(0x2066, 0x206A))  # LRI RLI FSI PDI
)
#: Zero-width / invisible characters that are almost never intended in a prompt.
ZERO_WIDTH_CHARS = frozenset([0x200B, 0x2060, 0x2061, 0x2062, 0x2063, 0x2064, 0xFEFF, 0x00AD])
#: Joiners: legitimately used inside emoji sequences and some scripts, so
#: they are reported as warnings rather than errors.
JOINER_CHARS = frozenset([0x200C, 0x200D])
#: Unicode tag block — invisible ASCII look-alikes ("ASCII smuggling").
TAG_RANGE = range(0xE0000, 0xE0080)
#: Hangul/braille/combining "filler" characters that render as blank space
#: but are not category Cf.
FILLER_CHARS = frozenset([0x034F, 0x115F, 0x1160, 0x2800, 0x3164, 0xFFA0])
#: Variation selectors. Any run of them can encode hidden bytes ("emoji
#: smuggling"); a single U+FE0E/U+FE0F after a visible character is the
#: normal text/emoji presentation switch and is allowed.
VS_RANGES = (range(0xFE00, 0xFE10), range(0xE0100, 0xE01F0))
PRESENTATION_SELECTORS = frozenset([0xFE0E, 0xFE0F])
#: Whitespace control characters that are fine in a prompt.
ALLOWED_CONTROLS = frozenset("\t\n\r")


def is_variation_selector(cp: int) -> bool:
    return any(cp in r for r in VS_RANGES)


@dataclass(frozen=True)
class HiddenChar:
    index: int
    codepoint: int
    kind: str  # bidi | tag | control | zero-width | format | filler | variation-selector | joiner
    severity: str  # "error" | "warning"

    @property
    def label(self) -> str:
        name = unicodedata.name(chr(self.codepoint), "")
        return f"U+{self.codepoint:04X}" + (f" {name}" if name else "")


def classify(ch: str) -> tuple[str, str] | None:
    """Return ``(kind, severity)`` for a suspicious character, else None.

    Per-character view; ``find_hidden`` adds context rules (variation
    selector runs, lone carriage returns).
    """
    cp = ord(ch)
    if cp in BIDI_CHARS:
        return "bidi", "error"
    if cp in TAG_RANGE:
        return "tag", "error"
    if ch in ALLOWED_CONTROLS:
        return None
    if cp < 0x20 or 0x7F <= cp <= 0x9F or cp in (0x2028, 0x2029):
        return "control", "error"
    if cp in JOINER_CHARS:
        return "joiner", "warning"
    if cp in ZERO_WIDTH_CHARS:
        return "zero-width", "error"
    if is_variation_selector(cp):
        return "variation-selector", "warning"
    if cp in FILLER_CHARS:
        return "filler", "error"
    if unicodedata.category(ch) == "Cf":
        # Every other Unicode "format" character: invisible by definition
        # (e.g. U+180E, U+206A-206F, U+FFF9-FFFB, U+0600-0605, U+1D173-1D17A).
        return "format", "error"
    return None


def find_hidden(text: str) -> list[HiddenChar]:
    """Every suspicious invisible/control character in ``text``, in order."""
    found = []
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        cp = ord(ch)
        if is_variation_selector(cp):
            j = i
            while j < n and is_variation_selector(ord(text[j])):
                j += 1
            run = j - i
            if run == 1 and cp in PRESENTATION_SELECTORS and i > 0:
                i = j  # ordinary emoji/text presentation selector
                continue
            severity = "error" if run > 1 else "warning"
            for k in range(i, j):
                found.append(HiddenChar(k, ord(text[k]), "variation-selector", severity))
            i = j
            continue
        if ch == "\r" and not (i + 1 < n and text[i + 1] == "\n"):
            found.append(HiddenChar(i, cp, "control", "error"))  # lone CR rewrites lines
        else:
            hit = classify(ch)
            if hit:
                found.append(HiddenChar(i, cp, hit[0], hit[1]))
        i += 1
    return found


def _escape(ch: str) -> str:
    cp = ord(ch)
    return f"\\u{cp:04x}" if cp <= 0xFFFF else f"\\U{cp:08x}"


def sanitize(text: object, keep_newlines: bool = True) -> str:
    """Make ``text`` safe to print to a terminal.

    Control, bidi, zero-width, joiner and tag characters become visible
    ``\\uXXXX`` escapes. Tabs are kept; newlines are kept unless
    ``keep_newlines`` is False (then ``\\n`` / ``\\r`` are escaped too, for
    single-line fields such as notes and author names). Carriage returns are
    always escaped because they can overwrite the current line.
    """
    s = str(text)
    out = []
    for ch in s:
        if ch == "\n" and keep_newlines:
            out.append(ch)
        elif ch == "\t":
            out.append(ch)
        elif ch in "\n\r" or (classify(ch) and ord(ch) not in PRESENTATION_SELECTORS):
            out.append(_escape(ch))
        else:
            out.append(ch)
    return "".join(out)


def one_line(text: object) -> str:
    """``sanitize`` for single-line fields (notes, actors, commit subjects)."""
    return sanitize(text, keep_newlines=False)
