"""Prompt file discovery and parsing.

Two formats, one data model::

    prompts/classifier.yaml     # pure YAML
    prompts/summarizer.md       # YAML frontmatter + Markdown body

Required fields: ``name``, ``template``.
Optional: ``description``, ``model``, ``params`` (mapping), ``variables`` (list).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from promptpin.errors import InvalidPromptError, PromptNotFoundError

PROMPTS_DIRNAME = "prompts"
FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n(.*)\Z", re.DOTALL)
VARIABLE_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


@dataclass
class Prompt:
    name: str
    template: str
    description: str = ""
    model: str = ""
    params: dict = field(default_factory=dict)
    variables: list[str] = field(default_factory=list)
    source_file: str = ""

    @property
    def template_sha256(self) -> str:
        return hashlib.sha256(self.template.encode("utf-8")).hexdigest()

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
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise InvalidPromptError(f"{source}: invalid YAML: {exc}") from exc
    return _build_prompt(_parse_mapping(data, source), source)


def parse_markdown_text(text: str, source: str) -> Prompt:
    match = FRONTMATTER_RE.match(text)
    if not match:
        raise InvalidPromptError(
            f"{source}: Markdown prompts need YAML frontmatter between --- lines"
        )
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
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
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
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
    return Prompt(
        name=name,
        template=template,
        description=description,
        model=model,
        params=params,
        variables=variables,
        source_file=source,
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
    text = (repo_root / rel).read_text(encoding="utf-8")
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
