"""Validation: schema checks, variable consistency, best-effort secret scan.

The secret scan is a heuristic tripwire, not a guarantee — it is labeled as
such everywhere it appears. Real secret hygiene is pre-commit hooks and
restricted keys.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from promptpin import registry
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


def validate_prompt(prompt: registry.Prompt) -> list[Finding]:
    findings: list[Finding] = []
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
    for name in registry.list_prompts(top, cfg.prompts_dir):
        try:
            prompt = registry.load_prompt(top, cfg.prompts_dir, name)
        except InvalidPromptError as exc:
            findings.append(Finding("error", name, str(exc)))
            continue
        findings.extend(validate_prompt(prompt))
    return findings


def is_clean(findings: list[Finding]) -> bool:
    return not any(f.level == "error" for f in findings)
