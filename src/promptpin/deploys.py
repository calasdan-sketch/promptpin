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

from promptpin import gitutil, registry, statedb
from promptpin.config import LOCK_FILENAME, Config, deploy_lock_path
from promptpin.errors import DeployError, LockError, VersionNotFoundError


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
        }

    @classmethod
    def from_mapping(cls, m: dict) -> Pin:
        return cls(
            ref=m.get("ref", ""),
            sha=m.get("sha", ""),
            tag=m.get("tag", ""),
            model=m.get("model", ""),
            params=m.get("params") or {},
            template_sha256=m.get("template_sha256", ""),
            deployed_at=m.get("deployed_at", ""),
            deployed_by=m.get("deployed_by", ""),
            note=m.get("note", ""),
            mutable_ref=bool(m.get("mutable_ref", False)),
        )


def read_lock(repo_root: Path) -> dict:
    """The ``promptpin.lock`` contents as ``{env: {prompt: Pin}}``."""
    path = repo_root / LOCK_FILENAME
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    envs = data.get("envs") or {}
    return {
        env: {name: Pin.from_mapping(m) for name, m in prompts.items()}
        for env, prompts in envs.items()
    }


def write_lock(repo_root: Path, pins: dict) -> None:
    data = {
        "version": 1,
        "envs": {
            env: {name: pin.to_mapping() for name, pin in prompts.items()}
            for env, prompts in pins.items()
        },
    }
    (repo_root / LOCK_FILENAME).write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


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
        template = template_at(top, cfg, name, pin.sha)
        filename = f"{name}.template.md"
        (out_dir / filename).write_text(template, encoding="utf-8")
        manifest_prompts[name] = {
            "sha": pin.sha,
            "ref": pin.ref,
            "model": pin.model,
            "params": pin.params,
            "template_sha256": pin.template_sha256,
            "file": filename,
        }
    manifest = {"env": env, "prompts": manifest_prompts}
    (out_dir / "manifest.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
    )
    return {"env": env, "out": str(out_dir), "prompts": sorted(manifest_prompts)}


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
    if ref.startswith("v") or ref.isdigit():
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
    issues = []
    for env, prompts in pins.items():
        for name, pin in prompts.items():
            if not pin.tag:
                continue
            try:
                current_sha, _ = gitutil.resolve_ref(top, pin.tag)
            except Exception:
                issues.append(
                    f"{env}/{name}: tag {pin.tag} no longer exists — pin is frozen at {pin.sha[:7]}"
                )
                continue
            if current_sha != pin.sha:
                issues.append(
                    f"{env}/{name}: tag {pin.tag} moved "
                    f"({pin.sha[:7]} → {current_sha[:7]}) — "
                    f"pin is frozen at the deployed SHA"
                )
    return issues
