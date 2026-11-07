"""``promptpin review``: a plain-language summary of what a PR changes in production.

Compares ``promptpin.lock`` at a base ref with the head (default: the working
tree) and renders Markdown a non-engineer can approve: which pin moves from
which version to which, what kind of change it is (wording / settings /
whitespace-only), invisible characters, the exact wording diff, and the
command that undoes it. All prompt text is escaped and fenced, so it cannot
inject Markdown, mentions or terminal sequences into the review.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path

from promptpin import deploys, gitutil, safety
from promptpin.canon import classify_change
from promptpin.config import LOCK_FILENAME, Config
from promptpin.diff import config_diff
from promptpin.errors import PromptpinError

MAX_DIFF_LINES = 200


@dataclass
class Change:
    env: str
    prompt: str
    kind: str  # added | removed | moved
    old: deploys.Pin | None
    new: deploys.Pin | None
    summary: list[str] = field(default_factory=list)
    diff_lines: list[str] = field(default_factory=list)
    config_changes: list[tuple[str, str, str]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    blocking: bool = False


def _lock_at(top: Path, ref: str) -> dict:
    sha, _ = gitutil.resolve_ref(top, ref)
    raw = gitutil.show_bytes(top, sha, LOCK_FILENAME)
    if raw is None:
        return {}
    return deploys.parse_lock(raw.decode("utf-8", errors="strict"))


def _fence(text: str, lang: str = "") -> str:
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}{lang}\n{text}\n{ticks}"


def _inline(text: object) -> str:
    return "`" + safety.one_line(text).replace("`", "'") + "`"


def _count_hidden(text: str) -> int:
    return sum(1 for h in safety.find_hidden(text) if h.severity == "error")


def _describe(top: Path, cfg: Config, ch: Change) -> None:
    old, new = ch.old, ch.new
    if new is None:
        ch.summary.append("This prompt is no longer pinned to this environment.")
        return
    try:
        pn = deploys.prompt_at(top, cfg, ch.prompt, new.sha)
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        ch.problems.append(f"cannot load the new version at {new.sha[:7]}: {exc}")
        ch.blocking = True
        return
    if new.mutable_ref:
        ch.summary.append(
            f"Deployed from a moving ref ({_inline(new.ref)});"
            f" it is frozen at {_inline(new.sha[:7])}."
        )
    hidden = _count_hidden(pn.template)
    if old is None:
        ch.summary.append("New in this environment: nothing was pinned before.")
        if hidden:
            ch.problems.append(f"the template contains {hidden} invisible character(s)")
            ch.blocking = True
        return
    try:
        po = deploys.prompt_at(top, cfg, ch.prompt, old.sha)
    except Exception as exc:  # noqa: BLE001
        ch.problems.append(f"cannot load the previous version at {old.sha[:7]}: {exc}")
        ch.blocking = True
        return
    kind = classify_change(po, pn)
    ch.config_changes = config_diff(po, pn)
    if kind == "identical":
        ch.summary.append("Same wording and settings (only the pinned commit moved).")
    elif kind == "cosmetic":
        ch.summary.append(
            "Whitespace-only change (line endings, trailing spaces or blank lines at the end)."
            " The words are identical."
        )
    elif kind in ("wording", "wording+config"):
        added = removed = 0
        for line in difflib.unified_diff(po.template.split("\n"), pn.template.split("\n"), n=0):
            if line.startswith("+") and not line.startswith("+++"):
                added += 1
            elif line.startswith("-") and not line.startswith("---"):
                removed += 1
        ch.summary.append(f"Wording changed: {added} line(s) added, {removed} removed.")
    if ch.config_changes:
        names = ", ".join(c[0] for c in ch.config_changes)
        ch.summary.append(f"Settings changed: {names}.")
    if (
        po.model != pn.model or (po.params or {}) != (pn.params or {}) or po.format != pn.format
    ) and kind in ("config", "wording+config"):
        ch.summary.append("The model will behave differently even if the words are the same.")
    if hidden > _count_hidden(po.template):
        ch.problems.append(
            f"adds {hidden - _count_hidden(po.template)} invisible character(s) that a reviewer"
            " cannot see in an editor"
        )
        ch.blocking = True
    lines = list(
        difflib.unified_diff(
            po.template.split("\n"),
            pn.template.split("\n"),
            fromfile=f"{ch.prompt}@{old.sha[:7]}",
            tofile=f"{ch.prompt}@{new.sha[:7]}",
            lineterm="",
        )
    )
    ch.diff_lines = lines


def build_changes(top: Path, base: str, head: str | None = None) -> list[Change]:
    cfg = Config.load(top)
    old_lock = _lock_at(top, base)
    new_lock = _lock_at(top, head) if head else deploys.read_lock(top)
    changes = []
    for env in sorted(set(old_lock) | set(new_lock)):
        a, b = old_lock.get(env, {}), new_lock.get(env, {})
        for name in sorted(set(a) | set(b)):
            old, new = a.get(name), b.get(name)
            if (
                old
                and new
                and (old.sha, old.digest, old.template_sha256)
                == (
                    new.sha,
                    new.digest,
                    new.template_sha256,
                )
            ):
                continue
            kind = "added" if old is None else "removed" if new is None else "moved"
            ch = Change(env=env, prompt=name, kind=kind, old=old, new=new)
            _describe(top, cfg, ch)
            changes.append(ch)
    return changes


def _label(pin: deploys.Pin | None) -> str:
    if pin is None:
        return "(none)"
    return f"{_inline(pin.ref or pin.sha[:7])} ({_inline(pin.sha[:7])})"


def render_markdown(changes: list[Change], base: str) -> str:
    out = ["## promptpin review", ""]
    if not changes:
        out.append(
            f"No pin changes compared with {_inline(base)}: production prompts are unchanged."
        )
        return "\n".join(out) + "\n"
    blocked = [c for c in changes if c.blocking]
    out.append(
        f"{len(changes)} pin change(s) compared with {_inline(base)}"
        + (f", **{len(blocked)} need attention before approval**." if blocked else ".")
    )
    for c in changes:
        out += [
            "",
            f"### {_inline(c.env)} / {_inline(c.prompt)}: {_label(c.old)} to {_label(c.new)}",
        ]
        for s in c.summary:
            out.append(f"- {s}")
        for p in c.problems:
            out.append(f"- **Warning:** {safety.one_line(p).replace('`', chr(39))}")
        if c.config_changes:
            out += ["", "| Setting | Before | After |", "|---|---|---|"]
            for field_name, old, new in c.config_changes:
                cells = [
                    safety.one_line(x).replace("|", "\\|").replace("`", "'")
                    for x in (field_name, old, new)
                ]
                out.append("| " + " | ".join(f"`{x}`" for x in cells) + " |")
        if c.diff_lines:
            shown = c.diff_lines[:MAX_DIFF_LINES]
            body = safety.sanitize("\n".join(shown))
            if len(c.diff_lines) > MAX_DIFF_LINES:
                body += f"\n... ({len(c.diff_lines) - MAX_DIFF_LINES} more line(s) not shown)"
            out += ["", _fence(body, "diff")]
        if c.new is not None and c.new.note:
            out.append(f"\nDeploy note: {_inline(c.new.note)}")
        if c.old is not None and c.new is not None:
            out.append(
                "\nTo undo: "
                + _inline(f"promptpin deploy {c.prompt}@{c.old.sha[:40]} --env {c.env}")
                + " (pins the previous commit again)."
            )
    return "\n".join(out) + "\n"


def review(top: Path, base: str, head: str | None = None) -> tuple[str, bool]:
    """Markdown report and ``ok`` (False when something needs attention)."""
    try:
        changes = build_changes(top, base, head)
    except PromptpinError:
        raise
    return render_markdown(changes, base), not any(c.blocking for c in changes)
