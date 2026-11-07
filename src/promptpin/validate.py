"""Validation: schema checks, variable consistency, best-effort secret scan.

The secret scan is a heuristic tripwire, not a guarantee — it is labeled as
such everywhere it appears. Real secret hygiene is pre-commit hooks and
restricted keys.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

from promptpin import registry, safety
from promptpin.config import Config
from promptpin.errors import InvalidPromptError

#: (label, regex) — best-effort patterns for obvious secret material.
SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("openai-key", re.compile(r"\bsk-(proj-)?[A-Za-z0-9_-]{20,}")),
    ("aws-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "generic-api-key",
        re.compile(r"(?i)\b(api[_-]?key|secret)\b\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{16,}"),
    ),
    ("private-key-block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
]


@dataclass
class Finding:
    level: str  # "error" | "warning"
    prompt: str
    message: str

    def __str__(self) -> str:
        return f"[{self.level}] {self.prompt}: {self.message}"


def _hidden_findings(prompt: registry.Prompt) -> list[Finding]:
    fields = {
        "template": prompt.template,
        "description": prompt.description,
        "model": prompt.model,
    }

    def walk(value: object, path: str, depth: int = 0) -> None:
        if depth > 50:
            return
        if isinstance(value, str):
            fields[path] = value
        elif isinstance(value, dict):
            for k, v in value.items():
                walk(str(k), f"{path}.{k} (key)", depth + 1)
                walk(v, f"{path}.{k}", depth + 1)
        elif isinstance(value, (list, tuple)):
            for idx, v in enumerate(value):
                walk(v, f"{path}[{idx}]", depth + 1)

    walk(prompt.params or {}, "params")
    for idx, var in enumerate(prompt.variables):
        fields[f"variables[{idx}]"] = var
    findings = []
    for field_name, text in fields.items():
        hits = safety.find_hidden(text or "")
        if not hits:
            continue
        for severity in ("error", "warning"):
            group = [h for h in hits if h.severity == severity]
            if not group:
                continue
            line = (text[: group[0].index].count("\n")) + 1
            shown = ", ".join(sorted({h.label for h in group})[:3])
            more = f" (+{len(group) - 3} more)" if len(group) > 3 else ""
            kinds = "/".join(sorted({h.kind for h in group}))
            findings.append(
                Finding(
                    severity,
                    prompt.name,
                    f"{len(group)} invisible {kinds} character(s) in {field_name}"
                    f" (first on line {line}): {shown}{more} — reviewers cannot see these,"
                    " models can",
                )
            )
    return findings


_PLACEHOLDER_LIKE = re.compile(r"\{\{[^{}\n]*\}\}")


def _nonfinite(value: object, depth: int = 0) -> bool:
    if depth > 50:
        return False
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, dict):
        return any(_nonfinite(v, depth + 1) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_nonfinite(v, depth + 1) for v in value)
    return False


def _extra_findings(prompt: registry.Prompt) -> list[Finding]:
    findings: list[Finding] = []
    for m in _PLACEHOLDER_LIKE.finditer(prompt.template):
        if not registry.VARIABLE_RE.fullmatch(m.group()):
            line = prompt.template[: m.start()].count("\n") + 1
            shown = safety.one_line(m.group())[:40]
            findings.append(
                Finding(
                    "warning",
                    prompt.name,
                    f"{shown} on line {line} is not a valid placeholder (use {{{{name}}}} with"
                    " letters, digits and underscores) and will be sent to the model as written",
                )
            )
    if _nonfinite(prompt.params):
        findings.append(
            Finding(
                "error",
                prompt.name,
                "params contain NaN or infinity, which JSON (and other-language verifiers) cannot"
                " represent",
            )
        )
    for field_name, text in (("template", prompt.template), ("description", prompt.description)):
        mixed = safety.find_mixed_script(text or "")
        if mixed:
            index, word = mixed[0]
            line = text[:index].count("\n") + 1
            shown = word.encode("ascii", "backslashreplace").decode("ascii")[:40]
            more = f" (+{len(mixed) - 1} more)" if len(mixed) > 1 else ""
            findings.append(
                Finding(
                    "warning",
                    prompt.name,
                    f"{field_name} line {line}: word '{shown}' mixes Latin with Cyrillic/Greek"
                    f" letters{more} - a possible look-alike (homoglyph) swap that reviewers"
                    " cannot see",
                )
            )
    return findings


def validate_prompt(prompt: registry.Prompt) -> list[Finding]:
    findings: list[Finding] = _hidden_findings(prompt) + _extra_findings(prompt)
    declared = set(prompt.variables)
    used = set(prompt.template_variables())
    for var in sorted(used - declared):
        findings.append(
            Finding(
                "warning",
                prompt.name,
                f"template uses {{{{{var}}}}} but it is not declared in 'variables'",
            )
        )
    for var in sorted(declared - used):
        findings.append(
            Finding(
                "warning",
                prompt.name,
                f"'variables' declares {var!r} but the template never uses it",
            )
        )
    for label, pattern in SECRET_PATTERNS:
        if pattern.search(prompt.template):
            findings.append(
                Finding(
                    "warning",
                    prompt.name,
                    f"possible secret material ({label}) in template — "
                    "best-effort heuristic, verify manually; do not commit secrets",
                )
            )
    return findings


def validate_all(repo_root: Path) -> list[Finding]:
    """Validate every prompt in the working tree. Parse errors become findings."""
    from promptpin import gitutil

    top = gitutil.ensure_repo(repo_root)
    cfg = Config.load(top)
    findings: list[Finding] = []
    names = registry.list_prompts(top, cfg.prompts_dir)
    if not names:
        # Fail closed: a CI gate that checks zero prompts must not pass.
        return [
            Finding(
                "error",
                "-",
                f"no prompts found in {cfg.prompts_dir}/ — nothing to validate"
                " (wrong prompts_dir in promptpin.yaml?)",
            )
        ]
    for name in names:
        try:
            prompt = registry.load_prompt(top, cfg.prompts_dir, name)
        except (InvalidPromptError, UnicodeDecodeError) as exc:
            findings.append(Finding("error", name, str(exc)))
            continue
        findings.extend(validate_prompt(prompt))
    return findings


def is_clean(findings: list[Finding], strict: bool = False) -> bool:
    """True when no errors (with ``strict``, no warnings either)."""
    if strict:
        return not findings
    return not any(f.level == "error" for f in findings)
