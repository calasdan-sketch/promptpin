"""Prompt file discovery and parsing.

Two formats, one data model::

    prompts/classifier.yaml     # pure YAML
    prompts/summarizer.md       # YAML frontmatter + Markdown body

Required fields: ``name``, ``template``.
Optional: ``description``, ``model``, ``params`` (mapping), ``variables`` (list),
``format`` (``text`` — the default — or ``chat``).

Chat prompts (``format: chat``) keep the template a single string — so
hashing, diffs, blame and bundles work exactly as for text prompts — and mark
messages with a role line, one of ``system:``, ``developer:``, ``user:`` or
``assistant:`` alone on its line::

    system:
    You are a terse support agent.

    user:
    {{ticket_text}}
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from promptpin import yamlsafe
from promptpin.errors import InvalidPromptError, PromptNotFoundError

PROMPTS_DIRNAME = "prompts"
FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n(.*)\Z", re.DOTALL)
VARIABLE_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
#: Valid prompt names. Names become file names and git tag components, so
#: no path separators, dots-only names, or leading dashes.
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
#: Chat role marker: a role name alone on its own line.
ROLE_RE = re.compile(r"^[ \t]*(system|developer|user|assistant)[ \t]*:[ \t]*\r?$", re.MULTILINE)
FORMATS = ("text", "chat")


def config_digest_input(template_sha256: str, model: str, params: dict, fmt: str) -> str:
    """The exact canonical JSON string that ``config_digest`` hashes.

    Sorted keys, ASCII-escaped, no whitespace. Other-language verifiers can
    hash this string as-is instead of re-serialising params (Python writes
    ``1.0`` where JavaScript writes ``1``).
    """
    return json.dumps(
        {
            "format": fmt or "text",
            "model": model or "",
            "params": params or {},
            "template_sha256": template_sha256,
        },
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
        default=str,
    )


def config_digest(template_sha256: str, model: str, params: dict, fmt: str) -> str:
    """One SHA-256 over everything that changes what the model receives.

    Covers the template hash, model, params and format, as canonical JSON
    (sorted keys, ASCII, no whitespace). A change to any of them changes the
    digest, so a bundle or lock entry cannot swap the model or params while
    keeping the template hash.
    """
    canonical = config_digest_input(template_sha256, model, params, fmt)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def is_valid_name(name: object) -> bool:
    return isinstance(name, str) and NAME_RE.fullmatch(name) is not None


def split_messages(template: str) -> list[dict]:
    """Split a chat template into ``[{"role", "content"}]`` (unrendered).

    Raises ValueError when there is no role marker, when non-blank text
    precedes the first marker, or when a message is empty.
    """
    markers = list(ROLE_RE.finditer(template))
    if not markers:
        raise ValueError(
            "chat prompt has no role markers — start messages with a line like 'system:'"
        )
    if template[: markers[0].start()].strip():
        raise ValueError("chat prompt has text before the first role marker")
    messages = []
    for i, m in enumerate(markers):
        end = markers[i + 1].start() if i + 1 < len(markers) else len(template)
        content = template[m.end() : end].strip("\r\n")
        if not content.strip():
            raise ValueError(f"chat message #{i + 1} ({m.group(1)}) is empty")
        messages.append({"role": m.group(1), "content": content})
    return messages


@dataclass
class Prompt:
    name: str
    template: str
    description: str = ""
    model: str = ""
    params: dict = field(default_factory=dict)
    variables: list[str] = field(default_factory=list)
    source_file: str = ""
    format: str = "text"

    @property
    def template_sha256(self) -> str:
        return hashlib.sha256(self.template.encode("utf-8")).hexdigest()

    @property
    def canonical_sha256(self) -> str:
        """Hash of the whitespace-canonical template (for classifying edits only)."""
        from promptpin.canon import canonical_sha256

        return canonical_sha256(self.template)

    @property
    def digest(self) -> str:
        """``config_digest`` of this prompt (template + model + params + format)."""
        return config_digest(self.template_sha256, self.model, self.params, self.format)

    def template_variables(self) -> list[str]:
        """Variables actually referenced as {{var}} in the template, in order."""
        seen: list[str] = []
        for m in VARIABLE_RE.finditer(self.template):
            if m.group(1) not in seen:
                seen.append(m.group(1))
        return seen


def _parse_mapping(data: object, source: str) -> dict:
    if not isinstance(data, dict):
        raise InvalidPromptError(f"{source}: top-level document must be a mapping")
    return data


def parse_yaml_text(text: str, source: str) -> Prompt:
    try:
        data = yamlsafe.load(text)
    except yamlsafe.yaml.YAMLError as exc:
        raise InvalidPromptError(f"{source}: invalid YAML: {exc}") from exc
    return _build_prompt(_parse_mapping(data, source), source)


def parse_markdown_text(text: str, source: str) -> Prompt:
    match = FRONTMATTER_RE.match(text)
    if not match:
        raise InvalidPromptError(
            f"{source}: Markdown prompts need YAML frontmatter between --- lines"
        )
    try:
        data = yamlsafe.load(match.group(1))
    except yamlsafe.yaml.YAMLError as exc:
        raise InvalidPromptError(f"{source}: invalid frontmatter YAML: {exc}") from exc
    mapping = _parse_mapping(data, source)
    mapping["template"] = match.group(2)
    return _build_prompt(mapping, source)


def _build_prompt(mapping: dict, source: str) -> Prompt:
    name = mapping.get("name")
    template = mapping.get("template")
    if not name or not isinstance(name, str):
        raise InvalidPromptError(f"{source}: missing required field 'name'")
    if not template or not isinstance(template, str):
        raise InvalidPromptError(f"{source}: missing required field 'template'")
    if not is_valid_name(name):
        raise InvalidPromptError(
            f"{source}: prompt name {name!r} must match [A-Za-z0-9][A-Za-z0-9_-]*"
        )
    params = mapping.get("params") or {}
    if not isinstance(params, dict):
        raise InvalidPromptError(f"{source}: 'params' must be a mapping")
    variables = mapping.get("variables") or []
    if not isinstance(variables, list) or any(not isinstance(v, str) for v in variables):
        raise InvalidPromptError(f"{source}: 'variables' must be a list of strings")
    model = mapping.get("model") or ""
    if not isinstance(model, str):
        raise InvalidPromptError(f"{source}: 'model' must be a string")
    description = mapping.get("description") or ""
    if not isinstance(description, str):
        raise InvalidPromptError(f"{source}: 'description' must be a string")
    fmt = mapping.get("format") or "text"
    if fmt not in FORMATS:
        raise InvalidPromptError(f"{source}: 'format' must be one of {', '.join(FORMATS)}")
    if fmt == "chat":
        try:
            split_messages(template)
        except ValueError as exc:
            raise InvalidPromptError(f"{source}: {exc}") from exc
    return Prompt(
        name=name,
        template=template,
        description=description,
        model=model,
        params=params,
        variables=variables,
        source_file=source,
        format=fmt,
    )


def parse_text(text: str, source: str) -> Prompt:
    """Parse prompt text based on file extension."""
    if source.endswith(".md"):
        return parse_markdown_text(text, source)
    return parse_yaml_text(text, source)


def prompt_relpath(repo_root: Path, prompts_dir: str, name: str) -> str | None:
    """Repo-relative path of a prompt file if it exists, else None."""
    for ext in (".yaml", ".yml", ".md"):
        candidate = Path(prompts_dir) / f"{name}{ext}"
        if (repo_root / candidate).exists():
            return candidate.as_posix()
    return None


def load_prompt(repo_root: Path, prompts_dir: str, name: str) -> Prompt:
    """Load a prompt from the working tree."""
    rel = prompt_relpath(repo_root, prompts_dir, name)
    if rel is None:
        raise PromptNotFoundError(
            f"prompt {name!r} not found in {prompts_dir}/ "
            "(expected <name>.yaml, <name>.yml or <name>.md)"
        )
    try:
        text = yamlsafe.decode((repo_root / rel).read_bytes(), rel)
    except ValueError as exc:
        raise InvalidPromptError(str(exc)) from None
    prompt = parse_text(text, rel)
    if prompt.name != name:
        raise InvalidPromptError(
            f"{rel}: file name {name!r} does not match prompt name {prompt.name!r}"
        )
    return prompt


def list_prompts(repo_root: Path, prompts_dir: str) -> list[str]:
    """Sorted prompt names found in the working tree."""
    base = repo_root / prompts_dir
    if not base.is_dir():
        return []
    names = set()
    for ext in (".yaml", ".yml", ".md"):
        for path in base.glob(f"*{ext}"):
            names.add(path.stem)
    return sorted(names)
