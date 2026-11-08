#!/usr/bin/env python
"""Mutation-style sanity check: break one security condition at a time, expect a test to fail.

Not a full mutation-testing tool. Each mutant below flips ONE condition that a tamper
check depends on; the named test files are then run against a scratch copy of the tree.
A mutant that the tests do not catch ("SURVIVED") means a check is not really tested.

    python benchmarks/mutation_check.py          # prints a table, exit 1 if any mutant survives
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (label, file, old text, new text, tests that must fail)
MUTANTS = [
    (
        "attest: accept any MAC",
        "attest.py",
        "if not any(hmac.compare_digest(want, s) for s in sigs):",
        "if False:",
        ["tests/test_attest.py"],
    ),
    (
        "attest: allow tiny keys",
        "attest.py",
        "MIN_KEY_BYTES = 32",
        "MIN_KEY_BYTES = 1",
        ["tests/test_attest.py", "tests/test_round5.py"],
    ),
    (
        "attest: accept non-canonical base64",
        "attest.py",
        'if base64.b64encode(payload).decode("ascii") != envelope["payload"] or any(',
        "if False and any(",
        ["tests/test_properties.py"],
    ),
    (
        "attest: ignore age",
        "attest.py",
        "if age > max_age_seconds:",
        "if False:",
        ["tests/test_round5.py"],
    ),
    (
        "verify: ignore template hash",
        "deploys.py",
        "if prompt.template_sha256 != pin.template_sha256:",
        "if False:",
        ["tests/test_deploy.py", "tests/test_adversarial.py", "tests/test_round5.py"],
    ),
    (
        "verify: ignore model edits",
        "deploys.py",
        "if pin.model != prompt.model:",
        "if False:",
        ["tests/test_deploy.py", "tests/test_adversarial.py", "tests/test_round5.py"],
    ),
    (
        "verify: ignore config digest",
        "deploys.py",
        "if pin.digest and pin.digest != prompt.digest:",
        "if False:",
        ["tests/test_deploy.py", "tests/test_adversarial.py", "tests/test_round5.py"],
    ),
    (
        "log verify: wrong commit allowed",
        "servecheck.py",
        'if r["sha"] != pin.sha:',
        "if False:",
        ["tests/test_servecheck.py"],
    ),
    (
        "log verify: grace window unchecked",
        "servecheck.py",
        'if r["sha"] not in known:',
        "if False:",
        ["tests/test_round5.py"],
    ),
    (
        "promote: allow a moved tag",
        "deploys.py",
        "if sha != pin.sha:",
        "if False:",
        ["tests/test_round5.py"],
    ),
    (
        "promote: skip source verification",
        "deploys.py",
        '    if bad:\n        raise DeployError(\n            f"refusing to promote: {from_env}',
        '    if False:\n        raise DeployError(\n            f"refusing to promote: {from_env}',
        ["tests/test_round5.py"],
    ),
    (
        "rollback: pick the current pin",
        "history.py",
        'if ev.action != "unpin" and ev.sha != current.sha:',
        'if ev.action != "unpin" and ev.sha == current.sha:',
        ["tests/test_round5.py"],
    ),
    (
        "history: never flag author mismatch",
        "history.py",
        'claim_mismatch=action != "unpin" and pin.deployed_by != author,',
        "claim_mismatch=False,",
        ["tests/test_round5.py"],
    ),
    (
        "evidence: ignore edits",
        "evidence.py",
        'if report.get("report_sha256") != hashlib.sha256(data).hexdigest():',
        "if False:",
        ["tests/test_round5.py", "tests/test_properties.py"],
    ),
    (
        "evidence: ignore MAC",
        "evidence.py",
        "if not hmac.compare_digest(want, got):",
        "if False:",
        ["tests/test_round5.py"],
    ),
    (
        "reachable: invert ancestor test",
        "gitutil.py",
        'return proc.returncode == 0\n    raise VersionNotFoundError(f"git merge-base',
        'return proc.returncode != 0\n    raise VersionNotFoundError(f"git merge-base',
        ["tests/test_provenance.py"],
    ),
    (
        "runtime: skip in-memory hash guard",
        "runtime.py",
        'if _sha256(entry["template"]) != entry["template_sha256"]:',
        "if False:",
        ["tests/test_runtime.py"],
    ),
    (
        "bundle: skip template hash",
        "client.py",
        "if digest != expected_hash:",
        "if False:",
        ["tests/test_export.py", "tests/test_runtime.py", "tests/test_adversarial.py"],
    ),
    (
        "safety: show bidi controls raw",
        "safety.py",
        "(classify(ch) and ord(ch) not in PRESENTATION_SELECTORS)",
        "False",
        ["tests/test_safety.py", "tests/test_properties.py"],
    ),
    (
        "canon: do not strip trailing spaces",
        "canon.py",
        'return _TRAILING_WS.sub("", text.replace("\\r\\n", "\\n"))',
        'return text.replace("\\r\\n", "\\n")',
        ["tests/test_canon.py", "tests/test_properties.py"],
    ),
]


def run_one(label, fname, old, new, tests) -> str:
    with tempfile.TemporaryDirectory(prefix="pp-mut-") as tmp:
        work = Path(tmp) / "t"
        shutil.copytree(
            ROOT,
            work,
            ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", ".*_cache", "*.egg-info"),
        )
        target = work / "src" / "promptpin" / fname
        text = target.read_text(encoding="utf-8")
        if old not in text:
            return "STALE (pattern not found: update this script)"
        target.write_text(text.replace(old, new, 1), encoding="utf-8")
        env = {**os.environ, "PYTHONPATH": str(work / "src") + os.pathsep + str(work)}
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *tests],
            cwd=work,
            env=env,
            capture_output=True,
            text=True,
        )
        if "promptpin/__init__" in proc.stdout:  # never expected
            return "ERROR"
        return "killed" if proc.returncode != 0 else "SURVIVED"


def main() -> int:
    results = [(m[0], run_one(*m)) for m in MUTANTS]
    width = max(len(r[0]) for r in results)
    for label, verdict in results:
        print(f"{label:<{width}}  {verdict}")
    bad = [r for r in results if r[1] != "killed"]
    print(f"\n{len(results) - len(bad)} of {len(results)} mutants killed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
