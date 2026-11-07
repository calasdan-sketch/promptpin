"""Three-panel diff: template text, config, and recorded evals.

The eval panel shows *recorded measurements with provenance* — it never
claims a prompt change caused a score change.
"""

from __future__ import annotations

import difflib
from pathlib import Path

from promptpin import deploys, gitutil, safety, statedb
from promptpin.config import Config
from promptpin.registry import Prompt


def _find_prompt_file_at(top: Path, cfg: Config, name: str, sha: str) -> tuple[str, str]:
    prompt = deploys.prompt_at(top, cfg, name, sha)
    return prompt.source_file, prompt.template


def load_at(top: Path, name: str, ref: str) -> tuple[Prompt, str, str]:
    """Load prompt ``name`` at ``ref`` → (prompt, sha, display_ref)."""
    cfg = Config.load(top)
    sha, display_ref, _ = gitutil.resolve_prompt_ref(top, name, ref)
    return deploys.prompt_at(top, cfg, name, sha), sha, display_ref


def text_diff(a: Prompt, b: Prompt, a_label: str, b_label: str) -> str:
    lines = difflib.unified_diff(
        # split("\n"), not splitlines(): splitlines() also breaks on U+2028,
        # \x1c etc., which would make those characters vanish from the diff.
        a.template.split("\n"),
        b.template.split("\n"),
        fromfile=f"{a.name}@{a_label}",
        tofile=f"{b.name}@{b_label}",
        lineterm="",
    )
    return "\n".join(lines)


def config_diff(a: Prompt, b: Prompt) -> list[tuple[str, str, str]]:
    """Changed config keys as (field, old, new)."""
    changes = []
    if a.model != b.model:
        changes.append(("model", a.model or "(unset)", b.model or "(unset)"))
    if a.format != b.format:
        changes.append(("format", a.format, b.format))
    if a.description != b.description:
        changes.append(("description", a.description or "(unset)", b.description or "(unset)"))
    params_a = a.params or {}
    params_b = b.params or {}
    for key in sorted(set(params_a) | set(params_b)):
        va, vb = params_a.get(key), params_b.get(key)
        if va != vb:
            changes.append((f"params.{key}", repr(va), repr(vb)))
    vars_a, vars_b = sorted(a.variables), sorted(b.variables)
    if vars_a != vars_b:
        changes.append(("variables", ",".join(vars_a) or "(none)", ",".join(vars_b) or "(none)"))
    return changes


def eval_delta(top: Path, name: str, sha_a: str, sha_b: str) -> list[dict]:
    """Per-metric comparison of latest recorded evals between two versions."""
    conn = statedb.connect(top)
    try:
        va = statedb.latest_eval_values(conn, name, sha_a)
        vb = statedb.latest_eval_values(conn, name, sha_b)
    finally:
        conn.close()
    rows = []
    for metric in sorted(set(va) | set(vb)):
        a_info, b_info = va.get(metric), vb.get(metric)
        delta = None
        if a_info and b_info:
            delta = b_info["value"] - a_info["value"]
        rows.append(
            {
                "metric": metric,
                "a": a_info,
                "b": b_info,
                "delta": delta,
                "provenance_match": bool(
                    a_info
                    and b_info
                    and a_info["suite"] == b_info["suite"]
                    and a_info["dataset_sha"] == b_info["dataset_sha"]
                ),
            }
        )
    return rows


def render_diff(
    top: Path,
    name: str,
    ref_a: str,
    ref_b: str,
    color: bool = False,
) -> str:
    """Full three-panel diff as plain text (pipeable, PR-pasteable)."""
    a, sha_a, disp_a = load_at(top, name, ref_a)
    b, sha_b, disp_b = load_at(top, name, ref_b)

    def paint(code: str, s: str) -> str:
        return f"\033[{code}m{s}\033[0m" if color else s

    one = safety.one_line
    out: list[str] = []
    out.append(f"promptpin diff {name}: {one(disp_a)} ({sha_a[:7]}) → {one(disp_b)} ({sha_b[:7]})")
    hidden_a, hidden_b = safety.find_hidden(a.template), safety.find_hidden(b.template)
    # Compare *which* characters, not just how many: swapping a harmless
    # joiner for a tag character keeps the count but is a new risk.
    new_kinds = {h.codepoint for h in hidden_b} - {h.codepoint for h in hidden_a}
    added = len(hidden_b) - len(hidden_a)
    if new_kinds or added > 0:
        labels = ", ".join(f"U+{cp:04X}" for cp in sorted(new_kinds)[:5])
        detail = f" (new: {labels})" if labels else ""
        count = (
            f"adds {added} invisible character(s)" if added > 0 else "changes invisible characters"
        )
        out.append(
            paint(
                "33",
                f"⚠ {one(disp_b)} {count}{detail}; they are shown below as \\uXXXX escapes",
            )
        )
    from promptpin.canon import classify_change

    kind = classify_change(a, b)
    if kind == "cosmetic":
        out.append(
            "ℹ whitespace-only change: the words are identical (line endings, trailing"
            " spaces or end-of-file blank lines)"
        )
    out.append("")

    out.append("=== template ===")
    td = text_diff(a, b, disp_a, disp_b)
    if td:
        for line in td.split("\n"):
            line = one(line)
            if line.startswith("+") and not line.startswith("+++"):
                out.append(paint("32", line))
            elif line.startswith("-") and not line.startswith("---"):
                out.append(paint("31", line))
            else:
                out.append(line)
    else:
        out.append("(template identical)")
    out.append("")

    out.append("=== config ===")
    changes = config_diff(a, b)
    if changes:
        for field_name, old, new in changes:
            out.append(f"  {field_name}: {paint('31', one(old))} → {paint('32', one(new))}")
    else:
        out.append("(config identical)")
    out.append("")

    out.append("=== recorded evals ===")
    out.append("(measurements on record — not proof that the prompt change caused them)")
    rows = eval_delta(top, name, sha_a, sha_b)
    if not rows:
        out.append("(no evals recorded for either version)")
    else:
        for r in rows:
            a_v = f"{r['a']['value']}" if r["a"] else "—"
            b_v = f"{r['b']['value']}" if r["b"] else "—"
            d = f"{r['delta']:+.4g}" if r["delta"] is not None else "—"
            prov = ""
            if r["a"] and r["b"] and not r["provenance_match"]:
                prov = "  ⚠ different suite/dataset — not comparable"
            out.append(f"  {one(r['metric'])}: {a_v} → {b_v} (Δ {d}){prov}")
    return "\n".join(out)
