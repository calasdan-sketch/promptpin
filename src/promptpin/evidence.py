"""``promptpin evidence``: one self-describing record of an environment, for an auditor.

Everything in it is re-derived from git at the moment it is made (never read
from the local ledger): the current pins and whether each still matches its
source, the signature status of the pinned commits and of the newest lock
commit, the deploy history from ``git log`` of ``promptpin.lock`` (who, when,
signed or not), and, if a serving log exists, what ``log verify`` found.

The report carries its own SHA-256 so a later edit is detectable, and can be
HMAC-signed with a shared key. Neither is public-key non-repudiation: the
SHA-256 proves "unchanged since written" only if you keep the hash somewhere
else (a ticket, a signed commit); the HMAC proves "written by someone with the
key". The report says so inside itself.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path

from promptpin import __version__, deploys, gitutil, history, servecheck
from promptpin.config import LOCK_FILENAME
from promptpin.errors import PromptpinError

SCHEMA = "promptpin-evidence/1"


class EvidenceError(PromptpinError):
    pass


LIMITS = [
    "deployed_by and deployed_at in the lock are typed by whoever ran deploy; only the commit"
    " author, date and signature come from git.",
    "A good commit signature shows who committed, not that the change was reviewed;"
    " use --reachable-from for the reviewed branch and CODEOWNERS for approval.",
    "The serving log is written by the application; it shows what the app recorded.",
    "report_sha256 detects later edits only if the hash is stored somewhere the editor cannot"
    " reach. hmac_sha256 is a shared-secret MAC: anyone with the key can also forge it.",
]


def canonical(body: dict) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )


def build(
    repo_root: Path,
    env: str,
    *,
    reachable_from: str | None = None,
    require_signed: bool = False,
    db_file: Path | None = None,
    limit: int = 100,
    key: bytes | None = None,
) -> dict:
    top = gitutil.ensure_repo(repo_root)
    pins = deploys.read_lock(top).get(env)
    if not pins:
        raise EvidenceError(f"no pins for env {env!r} in {LOCK_FILENAME} - nothing to report")
    checks = deploys.verify_lock(
        top, env=env, reachable_from=reachable_from, require_signed=require_signed
    )
    pin_rows = []
    for name, pin in sorted(pins.items()):
        code, signer = (
            gitutil.signature_status(top, pin.sha) if gitutil.is_full_sha(pin.sha) else ("N", "")
        )
        reachable = None
        if reachable_from is not None and gitutil.is_full_sha(pin.sha):
            try:
                reachable = gitutil.is_ancestor(top, pin.sha, reachable_from)
            except PromptpinError:
                reachable = None
        pin_rows.append(
            {
                "prompt": name,
                "ref": pin.ref,
                "sha": pin.sha,
                "model": pin.model,
                "config_digest": pin.digest,
                "template_sha256": pin.template_sha256,
                "deployed_by": pin.deployed_by,
                "deployed_at": pin.deployed_at,
                "note": pin.note,
                "commit_signature": code,
                "commit_signer": signer,
                "reachable_from_reviewed_ref": reachable,
            }
        )
    lock_code, lock_signer = gitutil.signature_status(top, "HEAD", LOCK_FILENAME)
    events, problems = history.lock_history(top, env=env, limit=limit)
    serving: dict = {"checked": False}
    db = Path(db_file) if db_file else None
    from promptpin.config import db_path

    if (db or db_path(top)).exists():
        s_checks = servecheck.verify_serving(top, env, db)
        serving = {"checked": True, "checks": [c.to_mapping() for c in s_checks]}
    errors = [c for c in checks if c.level == "error"]
    body = {
        "schema": SCHEMA,
        "generated_at": deploys._utc_iso(),
        "promptpin_version": __version__,
        "env": env,
        "repo_head": gitutil.head_sha(top),
        "reviewed_ref": reachable_from,
        "verify": {
            "ok": not errors,
            "errors": [c.to_mapping() for c in checks if c.level == "error"],
            "warnings": [c.to_mapping() for c in checks if c.level == "warning"],
        },
        "pins": pin_rows,
        "lock_commit_signature": {"code": lock_code, "signer": lock_signer},
        "history": [ev.to_mapping() for ev in events],
        "history_problems": problems,
        "claim_mismatches": sum(1 for ev in events if ev.claim_mismatch),
        "serving": serving,
        "limits": LIMITS,
    }
    return seal(body, key)


def seal(body: dict, key: bytes | None = None) -> dict:
    body = {k: v for k, v in body.items() if k not in ("report_sha256", "hmac_sha256", "keyid")}
    data = canonical(body)
    body["report_sha256"] = hashlib.sha256(data).hexdigest()
    if key is not None:
        from promptpin import attest

        key = attest._check_key(key)  # same rules as attestations: length and variety
        mac = hmac.new(key, b"promptpin-evidence/1\n" + data, hashlib.sha256).digest()
        body["hmac_sha256"] = base64.b64encode(mac).decode("ascii")
    return body


def check(report: object, key: bytes | None = None) -> list[str]:
    """Problems with a stored report: edited body, wrong or missing MAC."""
    if not isinstance(report, dict) or report.get("schema") != SCHEMA:
        return ["not a promptpin evidence report"]
    problems = []
    body = {k: v for k, v in report.items() if k not in ("report_sha256", "hmac_sha256", "keyid")}
    try:
        data = canonical(body)
    except (TypeError, ValueError):
        return ["report contains values that cannot be hashed"]
    if report.get("report_sha256") != hashlib.sha256(data).hexdigest():
        problems.append("report_sha256 does not match the contents - the report was edited")
    if key is not None:
        mac = report.get("hmac_sha256")
        if not isinstance(mac, str):
            problems.append("report has no hmac_sha256 but a key was given")
        else:
            want = hmac.new(key, b"promptpin-evidence/1\n" + data, hashlib.sha256).digest()
            try:
                got = base64.b64decode(mac, validate=True)
            except ValueError:
                got = b""
            if not hmac.compare_digest(want, got):
                problems.append("hmac_sha256 does not match the key and contents")
    return problems


def _md(value: object) -> str:
    """One line of untrusted text that cannot break out of a Markdown table or code span."""
    from promptpin import safety

    text = safety.one_line(value)
    for ch in "|`<>[]\\*_":
        text = text.replace(ch, "\\" + ch)
    return text


def to_markdown(report: dict) -> str:
    """Plain-language rendering. Untrusted text is neutralised by the caller (safety.sanitize)."""
    v = report["verify"]
    lines = [
        f"# promptpin evidence: {report['env']}",
        "",
        f"- Made: {report['generated_at']} by promptpin {report['promptpin_version']}",
        f"- Repository head: `{report['repo_head']}`",
        f"- Verification against git: **{'PASSED' if v['ok'] else 'FAILED'}**"
        f" ({len(v['errors'])} error(s), {len(v['warnings'])} warning(s))",
        f"- Newest lock commit signature: {report['lock_commit_signature']['code']}",
        f"- Deploys whose recorded person differs from the git author:"
        f" {report['claim_mismatches']}",
        "",
        "## Pinned now",
        "",
        "| Prompt | Version | Commit | Signature | Deployed by (claimed) |",
        "|---|---|---|---|---|",
    ]
    for p in report["pins"]:
        lines.append(
            f"| {_md(p['prompt'])} | {_md(p['ref'])} | `{p['sha'][:7]}`"
            f" | {_md(p['commit_signature'])}"
            f" | {_md(p['deployed_by'])} |"
        )
    lines += ["", "## Deploy history (from git)", ""]
    if not report["history"]:
        lines.append("No committed deploys found.")
    for ev in report["history"]:
        flag = " (author differs from deployed_by)" if ev["claim_mismatch"] else ""
        lines.append(
            f"- {_md(ev['commit_date'])} {ev['action']} {_md(ev['prompt'])} -> `{ev['sha'][:7]}`"
            f" commit `{ev['commit'][:7]}` by {_md(ev['commit_author'])},"
            f" signature {_md(ev['signature'])}{flag}"
        )
    for c in v["errors"] + v["warnings"]:
        lines.append(f"- {c['level']}: {_md(c['prompt'])}: {_md(c['message'])}")
    lines += ["", "## What this does not prove", ""] + [f"- {t}" for t in report["limits"]]
    lines += ["", f"report_sha256: `{report['report_sha256']}`", ""]
    return "\n".join(lines)
