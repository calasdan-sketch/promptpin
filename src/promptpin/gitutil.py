"""Thin, testable wrapper around the git CLI.

All git interaction goes through here so version resolution has exactly one
implementation. Functions take an explicit ``repo`` path (a directory inside
the working tree) rather than relying on process cwd.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from promptpin import yamlsafe
from promptpin.errors import InvalidPromptError, NotAGitRepoError, VersionNotFoundError

#: A version label: ``v3`` or ``3``. Anything else (``vnext``, ``v2-exp``) is
#: treated as an ordinary git ref (branch, tag or SHA).
VERSION_RE = re.compile(r"v?[0-9]+")
_SHA_RE = re.compile(r"[0-9a-f]{7,40}")


def is_version_label(ref: str) -> bool:
    return VERSION_RE.fullmatch(ref) is not None


def is_full_sha(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None


def check_ref(ref: str) -> str:
    """Reject refs that git could read as an option or that cannot be a ref."""
    if (
        not ref
        or ref.startswith("-")
        or any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F for c in ref)
    ):
        raise VersionNotFoundError(f"invalid ref {ref!r}")
    return ref


#: Namespace prefix for promptpin's version tags.
#: ``promptpin release classifier`` for the 3rd time creates ``promptpin/classifier@v3``.
TAG_PREFIX = "promptpin"


def tag_name(prompt: str, version: str) -> str:
    """Return the canonical git tag for ``prompt`` at ``version`` (e.g. ``v3``)."""
    if not is_version_label(version):
        raise VersionNotFoundError(f"invalid version label {version!r} — use vN, e.g. v4")
    version = version if version.startswith("v") else f"v{version}"
    return f"{TAG_PREFIX}/{prompt}@{version}"


def _run_bytes(repo: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(["git", *args], cwd=repo, capture_output=True)
    except OSError:
        # Nonexistent cwd (or git missing): behave like a failed git call so
        # callers raise NotAGitRepoError instead of leaking FileNotFoundError.
        return subprocess.CompletedProcess(
            args=["git", *args], returncode=128, stdout=b"", stderr=b"not a git repository"
        )


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run git; decode output as UTF-8 regardless of the OS locale.

    (``text=True`` would use the locale codec — ASCII under ``LC_ALL=C``,
    cp1252 on many Windows machines — and crash or garble non-ASCII text.)
    """
    proc = _run_bytes(repo, *args)
    return subprocess.CompletedProcess(
        args=proc.args,
        returncode=proc.returncode,
        stdout=proc.stdout.decode("utf-8", errors="replace"),
        stderr=proc.stderr.decode("utf-8", errors="replace"),
    )


def ensure_repo(repo: Path) -> Path:
    """Return the repo top-level directory, or raise NotAGitRepoError."""
    proc = _run(repo, "rev-parse", "--show-toplevel")
    if proc.returncode != 0:
        raise NotAGitRepoError(
            f"{repo} is not inside a git repository. "
            "promptpin versions prompts with git — run `git init` first."
        )
    return Path(proc.stdout.strip())


def head_sha(repo: Path) -> str:
    proc = _run(repo, "rev-parse", "HEAD")
    if proc.returncode != 0:
        raise NotAGitRepoError("no commits yet — commit your prompts first")
    return proc.stdout.strip()


def resolve_ref(repo: Path, ref: str) -> tuple[str, bool]:
    """Resolve any git ref to a full 40-char SHA.

    Returns ``(sha, is_mutable)`` where ``is_mutable`` is True for refs that
    can move (branches, ``HEAD``, ``latest``). Tags and SHAs are immutable.
    Raises VersionNotFoundError when the ref does not exist.
    """
    check_ref(ref)
    proc = _run(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
    if proc.returncode != 0:
        raise VersionNotFoundError(f"ref {ref!r} does not resolve to a commit")
    sha = proc.stdout.strip()

    # A ref is mutable if it is not a tag and not a raw SHA.
    is_sha = _SHA_RE.fullmatch(ref.lower()) is not None and sha.startswith(ref.lower())
    tag_proc = _run(repo, "tag", "--points-at", sha)
    is_tag = ref in tag_proc.stdout.split()
    is_mutable = not (is_sha or is_tag)
    return sha, is_mutable


def resolve_prompt_ref(repo: Path, prompt: str, ref: str) -> tuple[str, str, bool]:
    """Resolve ``<prompt>@<ref>`` to ``(sha, display_ref, is_mutable)``.

    ``ref`` may be:
    - ``v3`` / ``3``      → tag ``promptpin/<prompt>@v3`` (immutable)
    - ``latest``          → highest-numbered version tag, else HEAD (mutable)
    - a full/short SHA   → that commit (immutable)
    - a branch name       → branch tip (mutable, warns)
    """
    check_ref(ref)
    display = ref
    if ref == "latest":
        tag = latest_tag(repo, prompt)
        if tag is None:
            sha, _ = resolve_ref(repo, "HEAD")
            return sha, "latest→HEAD", True
        # `latest` always resolves through the current highest tag: mutable by
        # definition, even though the underlying tag is immutable.
        sha, _ = resolve_ref(repo, tag)
        return sha, f"latest→{tag}", True
    immutable_tag = False
    if is_version_label(ref):
        ref = tag_name(prompt, ref)
        display = ref
        immutable_tag = True

    sha, mutable = resolve_ref(repo, ref)
    if immutable_tag:
        mutable = False
    return sha, display, mutable


def latest_tag(repo: Path, prompt: str) -> str | None:
    """Return the highest-numbered version tag for ``prompt``, or None."""
    tags = list_version_tags(repo, prompt)
    return tags[-1] if tags else None


def list_version_tags(repo: Path, prompt: str) -> list[str]:
    """All version tags for ``prompt``, sorted by version number ascending."""
    proc = _run(repo, "tag", "--list", f"{TAG_PREFIX}/{prompt}@v*")
    tags = [t for t in proc.stdout.splitlines() if t]

    def version_key(t: str) -> tuple[int, str]:
        v = t.rsplit("@v", 1)[-1]
        return (int(v) if v.isdigit() else 10**9, t)

    return sorted(tags, key=version_key)


def next_version(repo: Path, prompt: str) -> str:
    """Next version label (``vN``) for ``prompt``."""
    tags = list_version_tags(repo, prompt)
    if not tags:
        return "v1"
    last = tags[-1].rsplit("@v", 1)[-1]
    return f"v{int(last) + 1}" if last.isdigit() else "v1"


def create_tag(repo: Path, prompt: str, version: str, sha: str, message: str) -> str:
    """Create an annotated version tag; returns the tag name."""
    tag = tag_name(prompt, version)
    proc = _run(repo, "tag", "-a", tag, sha, "-m", message)
    if proc.returncode != 0:
        raise VersionNotFoundError(f"could not create tag {tag}: {proc.stderr.strip()}")
    return tag


def show_file(repo: Path, sha: str, relpath: str) -> str:
    """Return the contents of ``relpath`` at commit ``sha``."""
    proc = _run_bytes(repo, "show", f"{sha}:{relpath}")
    if proc.returncode != 0:
        raise VersionNotFoundError(
            f"{relpath} does not exist at {sha[:7]} — "
            "the prompt may have been added later, or renamed"
        )
    try:
        return yamlsafe.decode(proc.stdout, f"{relpath} at {sha[:7]}")
    except ValueError as exc:
        raise InvalidPromptError(str(exc)) from None


def file_history(repo: Path, relpath: str, limit: int = 20) -> list[dict]:
    """Recent commits touching ``relpath``: [{sha, author, date, message}]."""
    proc = _run(
        repo, "log", f"-n{limit}", "--format=%H%x1f%an%x1f%ad%x1f%s", "--date=iso", "--", relpath
    )
    entries = []
    for line in proc.stdout.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 4:
            entries.append(
                {"sha": parts[0], "author": parts[1], "date": parts[2], "message": parts[3]}
            )
    return entries


def commit_file(repo: Path, relpath: str, message: str) -> str:
    """Stage ``relpath`` and commit; returns the new commit SHA."""
    add = _run(repo, "add", relpath)
    if add.returncode != 0:
        raise VersionNotFoundError(f"git add failed: {add.stderr.strip()}")
    commit = _run(repo, "commit", "-m", message, "--", relpath)
    if commit.returncode != 0:
        raise VersionNotFoundError(f"git commit failed: {commit.stderr.strip()}")
    return head_sha(repo)


def status_porcelain(repo: Path) -> str:
    proc = _run(repo, "status", "--porcelain")
    return proc.stdout


def current_actor(repo: Path) -> str:
    proc = _run(repo, "config", "user.name")
    name = proc.stdout.strip()
    if name:
        return name
    import getpass

    return getpass.getuser()
