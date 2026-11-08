"""Deploy history derived from git itself (``git log`` of ``promptpin.lock``).

The local ledger in ``.promptpin/state.db`` is per-machine and not committed:
on a fresh clone or a CI runner it is empty. The committed lock file is the
record every machine shares, so this module rebuilds the history from it:
one event per pin that a commit added, moved, re-deployed or removed, with
the commit's author, date and signature status.

Two things are deliberately kept apart in each event: what the *commit* says
(author, date, signature - set by git, signed if you sign commits) and what
the *lock entry* claims (``deployed_by`` / ``deployed_at`` - typed by whoever
ran ``deploy``, never signed on their own). ``claim_mismatch`` flags events
where those disagree.
"""

from __future__ import annotations

import dataclasses
import secrets
import subprocess
from pathlib import Path

from promptpin import deploys, gitutil
from promptpin.config import LOCK_FILENAME
from promptpin.errors import PromptpinError

MAX_COMMITS = 2000


@dataclasses.dataclass
class DeployEvent:
    commit: str
    commit_date: str
    commit_author: str
    signature: str  # git %G? code: G good, U untrusted, N none, B bad, ...
    signer: str
    action: str  # pin | repin | redeploy | unpin
    env: str
    prompt: str
    sha: str
    previous_sha: str
    ref: str
    deployed_at: str
    deployed_by: str
    note: str
    claim_mismatch: bool

    def to_mapping(self) -> dict:
        return dataclasses.asdict(self)


def _cat_file_batch(top: Path, specs: list[str]) -> dict[str, bytes | None]:
    """Contents of each ``<rev>:<path>`` (None when missing), via one ``git cat-file --batch``."""
    if not specs:
        return {}
    try:
        proc = subprocess.run(
            ["git", "cat-file", "--batch"],
            cwd=top,
            input=("\n".join(specs) + "\n").encode("utf-8"),
            capture_output=True,
        )
    except OSError as exc:
        raise PromptpinError(f"cannot run git: {exc}") from None
    out = proc.stdout
    result: dict[str, bytes | None] = {}
    pos = 0
    for spec in specs:
        eol = out.find(b"\n", pos)
        if eol < 0:
            raise PromptpinError("git cat-file returned truncated output")
        header = out[pos:eol].split()
        pos = eol + 1
        if len(header) == 2 and header[1] == b"missing":
            result[spec] = None
            continue
        if len(header) != 3 or not header[2].isdigit():
            result[spec] = None
            continue
        size = int(header[2])
        result[spec] = out[pos : pos + size]
        pos += size + 1  # the blob is followed by a newline
    return result


def _pins_of(blob: bytes | None) -> dict | None:
    """``{env: {prompt: Pin}}`` of one lock blob; None when absent or unreadable."""
    if blob is None:
        return {}
    from promptpin import yamlsafe

    try:
        return deploys.parse_lock(yamlsafe.decode(blob, LOCK_FILENAME))
    except Exception:  # noqa: BLE001 - a bad historic lock must not hide the others
        return None


def lock_history(
    repo_root: Path,
    env: str | None = None,
    prompt: str | None = None,
    limit: int = 50,
    signatures: bool = True,
) -> tuple[list[DeployEvent], list[str]]:
    """Newest-first deploy events from git, plus notes (``problems``) about unreadable history.

    ``signatures=False`` skips git's per-commit signature check (``%G?``), which can
    take tens of milliseconds a commit when gpg or ssh signing is configured.
    """
    top = gitutil.ensure_repo(repo_root)
    # A random field separator: author names are chosen by whoever made the commit and
    # could contain any fixed separator, which would make their commit vanish from the list.
    sep = "%x1f" + secrets.token_hex(8) + "%x1f"
    fmt = f"%H{sep}%an{sep}%aI{sep}" + (f"%G?{sep}%GS" if signatures else f"{sep}")
    proc = gitutil._run(
        top, "log", f"-n{MAX_COMMITS}", "--topo-order", f"--format={fmt}", "--", LOCK_FILENAME
    )
    if proc.returncode != 0:
        if "does not have any commits" in proc.stderr:
            return [], []
        return [], [f"git log failed: {proc.stderr.strip()[:200]}"]
    commits = []
    skipped = 0
    marker = "\x1f" + sep[4:20] + "\x1f"
    for line in proc.stdout.split("\n"):  # not splitlines(): it also splits on U+2028, U+0085
        if not line:
            continue
        parts = line.split(marker)
        if len(parts) == 5 and gitutil.is_full_sha(parts[0]):
            commits.append(parts)
        else:
            skipped += 1
    if not commits:
        return [], []
    specs = []
    for c in commits:
        specs += [f"{c[0]}:{LOCK_FILENAME}", f"{c[0]}^:{LOCK_FILENAME}"]
    blobs = _cat_file_batch(top, specs)
    events: list[DeployEvent] = []
    problems: list[str] = []
    if gitutil._run(top, "rev-parse", "--is-shallow-repository").stdout.strip() == "true":
        problems.append(
            "this is a shallow clone: older lock commits are missing, so the oldest event shown"
            " may really be a later change (fetch the full history)"
        )
    if skipped:
        problems.append(f"{skipped} line(s) of git log output could not be read")
    for sha, author, date, code, signer in commits:
        new = _pins_of(blobs.get(f"{sha}:{LOCK_FILENAME}"))
        old = _pins_of(blobs.get(f"{sha}^:{LOCK_FILENAME}"))
        if new is None or old is None:
            problems.append(f"{sha[:7]}: {LOCK_FILENAME} could not be read at this commit")
            continue
        keys = {(e, n) for e, ps in new.items() for n in ps} | {
            (e, n) for e, ps in old.items() for n in ps
        }
        for e, n in sorted(keys):
            if env is not None and e != env:
                continue
            if prompt is not None and n != prompt:
                continue
            a = old.get(e, {}).get(n)
            b = new.get(e, {}).get(n)
            if a is None and b is None:
                continue
            if b is None:
                action, pin = "unpin", a
            elif a is None:
                action, pin = "pin", b
            elif a.sha != b.sha:
                action, pin = "repin", b
            elif a.to_mapping() != b.to_mapping():
                action, pin = "redeploy", b
            else:
                continue
            assert pin is not None
            events.append(
                DeployEvent(
                    commit=sha,
                    commit_date=date,
                    commit_author=author,
                    signature=(code or "N") if signatures else "",
                    signer=signer,
                    action=action,
                    env=e,
                    prompt=n,
                    sha=pin.sha,
                    previous_sha=a.sha if a else "",
                    ref=pin.ref,
                    deployed_at=pin.deployed_at,
                    deployed_by=pin.deployed_by,
                    note=pin.note,
                    claim_mismatch=action != "unpin" and pin.deployed_by != author,
                )
            )
    return events[:limit], problems


def known_shas(repo_root: Path, env: str, prompt: str) -> tuple[set[str] | None, list[str]]:
    """Every commit ever pinned for ``env/prompt`` in the lock history, or None if unknowable."""
    events, problems = lock_history(
        repo_root, env=env, prompt=prompt, limit=MAX_COMMITS, signatures=False
    )
    if not events:
        return None, problems
    return {ev.sha for ev in events if ev.action != "unpin"}, problems


def previous_pin_sha(repo_root: Path, env: str, prompt: str) -> tuple[str, str] | None:
    """``(sha, ref)`` of the newest pin for ``env/prompt`` that differs from the current one."""
    top = gitutil.ensure_repo(repo_root)
    current = deploys.read_lock(top).get(env, {}).get(prompt)
    if current is None:
        return None
    events, _ = lock_history(top, env=env, prompt=prompt, limit=MAX_COMMITS, signatures=False)
    for ev in events:  # newest first
        if ev.action != "unpin" and ev.sha != current.sha:
            return ev.sha, ev.ref
    return None
