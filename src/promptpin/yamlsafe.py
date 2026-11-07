"""YAML loading for untrusted files: safe types only, no aliases.

``yaml.safe_load`` already refuses arbitrary Python objects, but it still
expands aliases. A few hundred bytes of nested ``&a [*a, *a, ...]`` anchors
("billion laughs") expand to gigabytes when printed or diffed. Prompt files,
lock files, configs and bundle manifests never need aliases, so they are
rejected outright.
"""

from __future__ import annotations

import yaml


class NoAliasSafeLoader(yaml.SafeLoader):
    """SafeLoader that raises on any ``*alias`` reference."""

    def compose_node(self, parent, index):  # type: ignore[override]
        if self.check_event(yaml.AliasEvent):
            event = self.peek_event()
            raise yaml.composer.ComposerError(
                None,
                None,
                "YAML aliases (*name) are not allowed in promptpin files",
                event.start_mark,
            )
        return super().compose_node(parent, index)


def load(text: str):
    """Parse one YAML document with safe types and no aliases.

    Absurdly deep nesting (``[[[[...``) would otherwise surface as a
    ``RecursionError`` traceback; it is reported as a normal YAML error.
    """
    try:
        return yaml.load(text, Loader=NoAliasSafeLoader)  # noqa: S506 - safe loader subclass
    except RecursionError:
        raise yaml.YAMLError("YAML is nested too deeply") from None


#: Upper bound for any single file promptpin parses or hashes (prompt, lock,
#: manifest, template). Real prompts are a few KB; this stops a hostile or
#: accidental 100 MB file from stalling a CI gate or a request path.
MAX_FILE_BYTES = 2 * 1024 * 1024


def decode(data: bytes, source: str = "") -> str:
    """Strict UTF-8 decode with CRLF normalized to LF.

    Independent of the OS locale (git output and files are read as bytes),
    so a template hashes the same on Linux, macOS and Windows. Only ``\\r\\n``
    is normalized; a lone ``\\r`` is kept so the hidden-character scan can
    report it instead of it silently becoming a line break.
    """
    if len(data) > MAX_FILE_BYTES:
        where = f"{source}: " if source else ""
        raise ValueError(f"{where}file is {len(data):,} bytes; the limit is {MAX_FILE_BYTES:,}")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        where = f"{source}: " if source else ""
        raise ValueError(f"{where}not valid UTF-8 ({exc.reason} at byte {exc.start})") from None
    if text.startswith("\ufeff"):
        text = text[1:]
    return text.replace("\r\n", "\n")


class _NoAliasDumper(yaml.SafeDumper):
    def ignore_aliases(self, data):  # type: ignore[override]
        return True


def dump(data) -> str:
    """``safe_dump`` that never emits anchors/aliases (our loader rejects them)."""
    return yaml.dump(data, Dumper=_NoAliasDumper, sort_keys=False, allow_unicode=False)
