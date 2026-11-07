"""Deployments: pinning prompt versions to environments.

The deployment record is a **committed** ``promptpin.lock`` file::

    envs:
      production:
        classifier:
          ref: v3
          sha: a1b2c3d4...
          ...

``deploy`` resolves ``<name>@<ref>`` to a SHA at deploy time, updates the lock
file, appends to the local deploy ledger, and (by default) commits the lock
file so ``git log -p promptpin.lock`` is the deployment history.

Concurrent deploys to the same repo serialize on ``.promptpin/deploy.lock``
(an OS file lock: fcntl on POSIX, msvcrt on Windows). The lock file itself
is per-env YAML, so git merges of concurrent deploys to *different* envs
resolve cleanly.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import yaml

from promptpin import fsutil, gitutil, registry, safety, statedb, yamlsafe
from promptpin.client import load_bundle_with_manifest
from promptpin.config import LOCK_FILENAME, Config, deploy_lock_path
from promptpin.errors import DeployError, InvalidPromptError, LockError, VersionNotFoundError


@dataclass
class Pin:
    ref: str
    sha: str
    tag: str
    model: str
    params: dict
    template_sha256: str
    deployed_at: str
    deployed_by: str
    note: str = ""
    mutable_ref: bool = False
    format: str = ""  # "" = written by promptpin < 0.2 (treated as "text")
    digest: str = ""  # registry.config_digest; "" = written by promptpin < 0.2

    def to_mapping(self) -> dict:
        return {
            "ref": self.ref,
            "sha": self.sha,
            "tag": self.tag,
            "model": self.model,
            "params": self.params,
            "template_sha256": self.template_sha256,
            "deployed_at": self.deployed_at,
            "deployed_by": self.deployed_by,
            "note": self.note,
            "mutable_ref": self.mutable_ref,
            "format": self.format or "text",
            "digest": self.digest,
        }

    @classmethod
    def from_mapping(cls, m: dict) -> Pin:
        def text(key: str) -> str:
            value = m.get(key)
            return "" if value is None else str(value)

        params = m.get("params")
        return cls(
            ref=text("ref"),
            sha=text("sha"),
            tag=text("tag"),
            model=text("model"),
            params=params if isinstance(params, dict) else {},
            template_sha256=text("template_sha256"),
            deployed_at=text("deployed_at"),
            deployed_by=text("deployed_by"),
            note=text("note"),
            mutable_ref=bool(m.get("mutable_ref", False)),
            format=text("format"),
            digest=text("digest"),
        )


def read_lock(repo_root: Path) -> dict:
    """The ``promptpin.lock`` contents as ``{env: {prompt: Pin}}``.

    The lock file arrives through git merges and PRs, so it is treated as
    untrusted input: a malformed structure or an invalid prompt name raises
    DeployError instead of flowing into file paths or git arguments.
    """
    path = repo_root / LOCK_FILENAME
    if not path.exists():
        return {}
    try:
        text = yamlsafe.decode(path.read_bytes(), LOCK_FILENAME)
    except (OSError, ValueError) as exc:
        raise DeployError(f"cannot read {LOCK_FILENAME}: {exc}") from None
    return parse_lock(text)


def parse_lock(text: str) -> dict:
    """Parse lock-file text (see ``read_lock``)."""
    try:
        data = yamlsafe.load(text) or {}
    except yaml.YAMLError as exc:
        raise DeployError(f"{LOCK_FILENAME} is not valid YAML (merge conflict?): {exc}") from exc
    if not isinstance(data, dict):
        raise DeployError(f"{LOCK_FILENAME}: top-level document must be a mapping")
    envs = data.get("envs") or {}
    if not isinstance(envs, dict):
        raise DeployError(f"{LOCK_FILENAME}: 'envs' must be a mapping")
    result: dict = {}
    for env, prompts in envs.items():
        if not isinstance(env, str) or not isinstance(prompts or {}, dict):
            raise DeployError(f"{LOCK_FILENAME}: env {env!r} must map prompt names to pins")
        result[env] = {}
        for name, m in (prompts or {}).items():
            if not registry.is_valid_name(name):
                raise DeployError(f"{LOCK_FILENAME}: invalid prompt name {name!r} in env {env!r}")
            if not isinstance(m, dict):
                raise DeployError(f"{LOCK_FILENAME}: pin {env}/{name} must be a mapping")
            result[env][name] = Pin.from_mapping(m)
    return result


def write_lock(repo_root: Path, pins: dict) -> None:
    data = {
        "version": 1,
        "envs": {
            env: {name: pin.to_mapping() for name, pin in prompts.items()}
            for env, prompts in pins.items()
        },
    }
    fsutil.atomic_write_text(repo_root / LOCK_FILENAME, yamlsafe.dump(data))


@contextmanager
def deploy_mutex(repo_root: Path, timeout: float = 30.0) -> Iterator[None]:
    """Serialize deploys with an OS file lock. Raises LockError on timeout.

    Uses fcntl on POSIX, msvcrt on Windows. The lock is per-machine: deploys
    from two machines racing on the same repo serialize via git (the loser
    of a push resolves the lock-file conflict with a normal merge).
    """
    from promptpin.config import ensure_state_dir

    ensure_state_dir(repo_root)
    lock_path = deploy_lock_path(repo_root)
    deadline = time.time() + timeout
    with open(lock_path, "w") as fh:
        _acquire(fh, deadline, timeout)
        try:
            yield
        finally:
            _release(fh)


def _acquire(fh, deadline: float, timeout: float) -> None:
    try:
        import fcntl as _fcntl
    except ImportError:  # Windows
        import msvcrt as _msvcrt

        while True:
            try:
                _msvcrt.locking(fh.fileno(), _msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.time() >= deadline:
                    raise LockError(_lock_timeout_message(timeout)) from None
                time.sleep(0.05)
    else:
        while True:
            try:
                _fcntl.flock(fh, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.time() >= deadline:
                    raise LockError(_lock_timeout_message(timeout)) from None
                time.sleep(0.05)


def _release(fh) -> None:
    try:
        import fcntl as _fcntl
    except ImportError:
        import msvcrt as _msvcrt

        _msvcrt.locking(fh.fileno(), _msvcrt.LK_UNLCK, 1)
    else:
        _fcntl.flock(fh, _fcntl.LOCK_UN)


def _lock_timeout_message(timeout: float) -> str:
    return (
        f"could not acquire deploy lock within {timeout}s — "
        "another deploy may be stuck; check .promptpin/deploy.lock"
    )


def _utc_iso() -> str:
    import datetime

    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def prompt_at(top: Path, cfg: Config, name: str, sha: str) -> registry.Prompt:
    """The parsed prompt ``name`` at commit ``sha``."""
    for ext in (".yaml", ".yml", ".md"):
        candidate = f"{cfg.prompts_dir}/{name}{ext}"
        try:
            text = gitutil.show_file(top, sha, candidate)
        except VersionNotFoundError:
            continue
        return registry.parse_text(text, candidate)
    raise DeployError(f"prompt {name!r} has no file in {cfg.prompts_dir}/ at {sha[:7]}")


def template_at(top: Path, cfg: Config, name: str, sha: str) -> str:
    """The template text of ``name`` at commit ``sha``."""
    return prompt_at(top, cfg, name, sha).template


def pin_mismatches(pin: Pin, prompt: registry.Prompt) -> list[str]:
    """Every way ``pin`` disagrees with the prompt file at its SHA.

    Compares template hash, model, params and format, and the combined digest
    when the pin has one. Empty list = the pin is exactly what git holds.
    """
    problems = []
    if prompt.template_sha256 != pin.template_sha256:
        problems.append(
            f"template_sha256 mismatch (lock says {pin.template_sha256[:12]}…,"
            f" git has {prompt.template_sha256[:12]}…)"
        )
    if pin.model != prompt.model:
        problems.append(f"model mismatch (lock says {pin.model!r}, git has {prompt.model!r})")
    if pin.params != (prompt.params or {}):
        problems.append(f"params mismatch (lock says {pin.params!r}, git has {prompt.params!r})")
    if (pin.format or "text") != prompt.format:
        problems.append(f"format mismatch (lock says {pin.format!r}, git has {prompt.format!r})")
    if pin.digest and pin.digest != prompt.digest:
        problems.append("digest mismatch (lock entry was edited after deploy)")
    return problems


def bundle_digest(entries: dict) -> str:
    """SHA-256 over every entry's name, commit and config digest, in name order."""
    import hashlib

    lines = "".join(
        f"{n}\t{entries[n].get('sha')}\t{entries[n].get('digest')}\n" for n in sorted(entries)
    )
    return hashlib.sha256(lines.encode("ascii", errors="replace")).hexdigest()


def export_bundle(repo_root: Path, env: str, out_dir: Path) -> dict:
    """Export pinned templates + manifest for runtime use.

    Writes ``<out_dir>/manifest.yaml`` and ``<out_dir>/<name>.template.md``.
    Bundle this directory with your app at build time: the app then needs
    no git access and no network to serve the exact pinned prompts.
    """
    top = gitutil.ensure_repo(repo_root)
    cfg = Config.load(top)
    pins = read_lock(top)
    prompts = pins.get(env)
    if not prompts:
        raise DeployError(f"no pins for env {env!r} — deploy something first")
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_prompts = {}
    for name, pin in prompts.items():
        if not gitutil.is_full_sha(pin.sha):
            raise DeployError(f"{env}/{name}: pinned sha {pin.sha!r} is not a full commit SHA")
        prompt = prompt_at(top, cfg, name, pin.sha)
        problems = pin_mismatches(pin, prompt)
        if problems:
            # Fail closed: never ship a template, model or params that differ
            # from what git holds at the pinned commit.
            raise DeployError(
                f"{env}/{name}: lock does not match git at {pin.sha[:7]}: {problems[0]}"
                " — run `promptpin verify`"
            )
        filename = f"{name}.template.md"
        # Bytes, not text mode: text mode would translate newlines on Windows
        # (and CRLF templates anywhere), breaking the hash check at load time.
        fsutil.atomic_write_bytes(out_dir / filename, prompt.template.encode("utf-8"))
        manifest_prompts[name] = {
            "sha": pin.sha,
            "ref": pin.ref,
            "model": prompt.model,
            "params": prompt.params or {},
            "format": prompt.format,
            "template_sha256": prompt.template_sha256,
            "digest": prompt.digest,
            "file": filename,
        }
    manifest = {
        "env": env,
        "bundle_digest": bundle_digest(manifest_prompts),
        "prompts": manifest_prompts,
    }
    fsutil.atomic_write_text(out_dir / "manifest.yaml", yamlsafe.dump(manifest))
    # JSON twin for runtimes without a YAML parser (see js/promptpin-verify.mjs).
    # `digest_input` is the exact string `digest` hashes, so no verifier has to
    # re-serialise params identically to Python.
    json_manifest = {
        "env": env,
        "bundle_digest": manifest["bundle_digest"],
        "prompts": {
            n: {
                **m,
                "digest_input": registry.config_digest_input(
                    m["template_sha256"], m["model"], m["params"], m["format"]
                ),
            }
            for n, m in manifest_prompts.items()
        },
    }
    import json as _json

    fsutil.atomic_write_text(
        out_dir / "manifest.json",
        _json.dumps(json_manifest, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
    )
    return {
        "env": env,
        "out": str(out_dir),
        "prompts": sorted(manifest_prompts),
        "bundle_digest": manifest["bundle_digest"],
    }


def deploy(
    repo_root: Path,
    name: str,
    ref: str,
    env: str,
    note: str = "",
    commit: bool | None = None,
    actor: str | None = None,
) -> dict:
    """Pin ``name@ref`` to ``env``. Returns a summary dict.

    Resolution, lock update, and ledger append happen under the deploy mutex,
    so concurrent deploys cannot interleave a read-modify-write.
    """
    top = gitutil.ensure_repo(repo_root)
    cfg = Config.load(top)
    if not registry.is_valid_name(name):
        raise DeployError(f"invalid prompt name {name!r}")
    if env not in cfg.envs:
        raise DeployError(
            f"unknown env {env!r} (configured envs: {', '.join(cfg.envs)})."
            f" Add it to {top / 'promptpin.yaml'} first."
        )
    actor = actor or gitutil.current_actor(top)
    do_commit = cfg.auto_commit_deploys if commit is None else commit

    sha, display_ref, mutable = gitutil.resolve_prompt_ref(top, name, ref)
    try:
        prompt = prompt_at(top, cfg, name, sha)
    except DeployError:
        available = gitutil.list_version_tags(top, name)
        hint = (
            f" Known versions: {', '.join(t.rsplit('@', 1)[-1] for t in available)}"
            if available
            else ""
        )
        raise DeployError(
            f"prompt {name!r} has no file in {cfg.prompts_dir}/ at {display_ref} ({sha[:7]}).{hint}"
        ) from None
    # Record the canonical version tag when the ref is one — whether the
    # user wrote `v3`, `3`, or the full `promptpin/<name>@v3`. The recorded
    # tag powers `status`'s tag-moved/tag-deleted drift detection.
    if gitutil.is_version_label(ref):
        tag = gitutil.tag_name(name, ref)
    elif ref.startswith(gitutil.TAG_PREFIX + "/"):
        tag = ref
    else:
        tag = ""

    pin = Pin(
        ref=ref,
        sha=sha,
        tag=tag,
        model=prompt.model,
        params=prompt.params,
        template_sha256=prompt.template_sha256,
        format=prompt.format,
        digest=prompt.digest,
        deployed_at=_utc_iso(),
        deployed_by=actor,
        note=note,
        mutable_ref=mutable,
    )

    with deploy_mutex(top):
        pins = read_lock(top)
        previous = pins.get(env, {}).get(name)
        pins.setdefault(env, {})[name] = pin
        write_lock(top, pins)
        conn = statedb.connect(top)
        try:
            statedb.record_deploy(
                conn,
                env=env,
                prompt=name,
                ref=display_ref,
                resolved_sha=sha,
                previous_sha=previous.sha if previous else None,
                actor=actor,
                note=note,
                mutable_ref=mutable,
            )
        finally:
            conn.close()

    commit_sha = None
    if do_commit:
        commit_sha = gitutil.commit_file(
            top, LOCK_FILENAME, f"promptpin: deploy {name}@{ref} → {env}"
        )

    return {
        "prompt": name,
        "ref": ref,
        "display_ref": display_ref,
        "sha": sha,
        "env": env,
        "previous_sha": previous.sha if previous else None,
        "mutable_ref": mutable,
        "commit_sha": commit_sha,
        "model": prompt.model,
    }


def rollback(
    repo_root: Path,
    name: str,
    env: str,
    note: str = "",
    commit: bool | None = None,
    actor: str | None = None,
) -> dict:
    """Redeploy the previous pin for (env, name)."""
    top = gitutil.ensure_repo(repo_root)
    conn = statedb.connect(top)
    try:
        prev = statedb.previous_pin(conn, env, name)
    finally:
        conn.close()
    if prev is None:
        raise DeployError(
            f"no previous deployment of {name!r} to {env!r} in the local ledger — "
            "nothing to roll back to. (The ledger lives in .promptpin/state.db;"
            " deploys made on another machine are not visible here.)"
        )
    result = deploy(
        top,
        name,
        prev["resolved_sha"],
        env,
        note=note or f"rollback from {prev['ref']}",
        commit=commit,
        actor=actor,
    )
    result["rolled_back_from"] = prev["resolved_sha"]
    return result


def status_issues(repo_root: Path, pins: dict) -> list[str]:
    """Detect pins whose ref no longer resolves to the pinned SHA.

    Catches tag deletion / tag moves after deploy: the pin stays frozen at
    the deployed SHA, but we warn loudly.
    """
    top = gitutil.ensure_repo(repo_root)
    return [f"{env}/{name}: {msg}" for env, name, msg in _tag_issues(top, pins)]


def _tag_issues(top: Path, pins: dict) -> list[tuple[str, str, str]]:
    issues = []
    for env, prompts in pins.items():
        for name, pin in prompts.items():
            if not pin.tag:
                continue
            try:
                current_sha, _ = gitutil.resolve_ref(top, pin.tag)
            except VersionNotFoundError:
                issues.append(
                    (env, name, f"tag {pin.tag} no longer exists — pin is frozen at {pin.sha[:7]}")
                )
                continue
            if current_sha != pin.sha:
                issues.append(
                    (
                        env,
                        name,
                        f"tag {pin.tag} moved ({pin.sha[:7]} → {current_sha[:7]}) — "
                        "pin is frozen at the deployed SHA",
                    )
                )
    return issues


# ---------------------------------------------------------------- verify ---


@dataclass
class Check:
    level: str  # "error" | "warning"
    env: str
    prompt: str
    message: str

    def to_mapping(self) -> dict:
        return {
            "level": self.level,
            "env": self.env,
            "prompt": self.prompt,
            "message": self.message,
        }


def verify_lock(
    repo_root: Path,
    env: str | None = None,
    bundle_dir: Path | None = None,
    reachable_from: str | None = None,
    require_signed: bool = False,
) -> list[Check]:
    """Re-derive every pin from git and compare. Fails closed.

    Errors: missing/empty/malformed lock, env not configured, pinned SHA not
    a full SHA or not in the repo, prompt file missing at the SHA, prompt
    name mismatch, template hash mismatch, and (with ``bundle_dir``) a bundle
    whose manifest or templates differ from the pins. Warnings: mutable refs
    and tags that moved or were deleted after deploy.

    Provenance (opt-in, still offline): ``reachable_from="origin/main"`` makes
    a pin whose commit is not an ancestor of that ref an error (it never went
    through the reviewed branch); ``require_signed=True`` requires a good git
    signature on every pinned commit and on the newest commit touching the
    lock file. An unresolvable ``reachable_from`` is an error, not a skip.
    """
    top = gitutil.ensure_repo(repo_root)
    cfg = Config.load(top)
    checks: list[Check] = []
    if not (top / LOCK_FILENAME).exists():
        return [Check("error", "-", "-", f"{LOCK_FILENAME} not found — nothing is pinned")]
    try:
        pins = read_lock(top)
    except DeployError as exc:
        return [Check("error", "-", "-", str(exc))]
    if env is not None:
        pins = {env: pins[env]} if env in pins else {}
    if not any(pins.values()):
        where = f" for env {env!r}" if env else ""
        return [Check("error", env or "-", "-", f"no pins{where} in {LOCK_FILENAME}")]

    for env_name, prompts in sorted(pins.items()):
        if env_name not in cfg.envs:
            checks.append(
                Check(
                    "error", env_name, "-", f"env {env_name!r} is not configured in promptpin.yaml"
                )
            )
        for name, pin in sorted(prompts.items()):

            def add(level: str, msg: str, _env: str = env_name, _name: str = name) -> None:
                checks.append(Check(level, _env, _name, msg))

            if not gitutil.is_full_sha(pin.sha):
                add("error", f"pinned sha {pin.sha!r} is not a full 40-char commit SHA")
                continue
            try:
                resolved, _ = gitutil.resolve_ref(top, pin.sha)
            except VersionNotFoundError:
                add("error", f"pinned commit {pin.sha[:7]} is not in this repo (fetch it?)")
                continue
            if resolved != pin.sha:
                add("error", f"pinned sha {pin.sha[:7]} resolves to a different commit")
                continue
            try:
                prompt = prompt_at(top, cfg, name, pin.sha)
            except (DeployError, InvalidPromptError) as exc:
                add("error", str(exc))
                continue
            if prompt.name != name:
                add("error", f"file at {pin.sha[:7]} declares name {prompt.name!r}")
            for problem in pin_mismatches(pin, prompt):
                add("error", f"{problem} at {pin.sha[:7]}")
            if not pin.digest:
                add("warning", "pin has no digest (deployed by promptpin < 0.2) — redeploy it")
            if pin.mutable_ref:
                add("warning", f"deployed from mutable ref {pin.ref!r} (frozen at {pin.sha[:7]})")
            if reachable_from is not None:
                try:
                    if not gitutil.is_ancestor(top, pin.sha, reachable_from):
                        add(
                            "error",
                            f"pinned commit {pin.sha[:7]} is not reachable from"
                            f" {reachable_from!r} — never merged through the reviewed branch",
                        )
                except VersionNotFoundError as exc:
                    add("error", f"cannot check reachability: {exc}")
            if require_signed:
                code, signer = gitutil.signature_status(top, pin.sha)
                _signature_check(add, f"pinned commit {pin.sha[:7]}", code, signer)
    if require_signed:
        code, signer = gitutil.signature_status(top, "HEAD", LOCK_FILENAME)
        _signature_check(
            lambda level, msg: checks.append(Check(level, env or "-", "-", msg)),
            f"newest commit touching {LOCK_FILENAME}",
            code,
            signer,
        )
    for env_name, name, msg in _tag_issues(top, pins):
        checks.append(Check("warning", env_name, name, msg))

    if bundle_dir is not None:
        checks.extend(_verify_bundle(pins, bundle_dir, env))
    return checks


def _signature_check(add, what: str, code: str, signer: str) -> None:
    if code == "G":
        return
    if code == "U":
        add("warning", f"{what} has a valid signature from an untrusted key")
        return
    reasons = {
        "N": "is not signed",
        "B": "has a BAD signature",
        "X": "has an expired signature",
        "Y": "was signed with an expired key",
        "R": "was signed with a revoked key",
        "E": "has a signature that cannot be checked (missing key/allowed-signers?)",
    }
    add("error", f"{what} {reasons.get(code, 'has an unrecognised signature status')}")


def _verify_bundle(pins: dict, bundle_dir: Path, env: str | None) -> list[Check]:
    checks: list[Check] = []
    try:
        bundle, manifest = load_bundle_with_manifest(bundle_dir)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        return [Check("error", env or "-", "-", f"bundle: {exc}")]
    bundle_env = manifest.get("env") if isinstance(manifest, dict) else None
    if not isinstance(bundle_env, str) or not bundle_env:
        return [Check("error", env or "-", "-", f"bundle: manifest env {bundle_env!r} is invalid")]
    if env is not None and bundle_env != env:
        checks.append(Check("error", env, "-", f"bundle is for env {bundle_env!r}, not {env!r}"))
        return checks
    expected = pins.get(bundle_env) or {}
    if not expected:
        return [Check("error", str(bundle_env), "-", "bundle env has no pins in the lock file")]
    json_path = Path(bundle_dir) / "manifest.json"
    if json_path.exists():
        import json as _json

        try:
            twin = _json.loads(json_path.read_text(encoding="utf-8"))
            twin_ok = (
                twin.get("bundle_digest") == manifest.get("bundle_digest")
                and twin.get("env") == bundle_env
                and set(twin.get("prompts", {})) == set(bundle)
                and all(
                    twin["prompts"][n].get(k) == bundle[n].get(k)
                    for n in bundle
                    for k in ("sha", "digest", "template_sha256")
                )
            )
        except (OSError, ValueError, AttributeError, TypeError):
            twin_ok = False
        if not twin_ok:
            checks.append(
                Check("error", bundle_env, "-", "manifest.json differs from manifest.yaml")
            )
    try:
        listed = {f"{n}.template.md" for n in bundle}
        strays = sorted(
            f.name
            for f in Path(bundle_dir).iterdir()
            if f.name.endswith(".template.md") and f.name not in listed
        )
    except OSError:
        strays = []
    if strays:
        shown = ", ".join(safety.one_line(s) for s in strays[:3])
        checks.append(
            Check(
                "warning",
                bundle_env,
                "-",
                f"bundle holds template file(s) the manifest does not list ({shown}):"
                " delete them, nothing verifies their contents",
            )
        )
    for name in sorted(set(expected) | set(bundle)):
        if name not in bundle:
            checks.append(Check("error", bundle_env, name, "pinned but missing from bundle"))
        elif name not in expected:
            checks.append(Check("error", bundle_env, name, "in bundle but not pinned"))
        else:
            b, p = bundle[name], expected[name]
            if b.get("sha") != p.sha:
                checks.append(
                    Check(
                        "error",
                        bundle_env,
                        name,
                        f"bundle is stale: has {str(b.get('sha'))[:7]}, lock pins {p.sha[:7]}"
                        " — re-run `promptpin export`",
                    )
                )
                continue
            for field_name, want in (
                ("template_sha256", p.template_sha256),
                ("model", p.model),
                ("params", p.params),
                ("format", p.format or "text"),
            ):
                if b.get(field_name) != want:
                    checks.append(
                        Check(
                            "error",
                            bundle_env,
                            name,
                            f"bundle {field_name} {b.get(field_name)!r} differs from the lock"
                            f" ({want!r})",
                        )
                    )
            if p.digest and b.get("digest") != p.digest:
                checks.append(Check("error", bundle_env, name, "bundle digest differs from lock"))
    return checks
