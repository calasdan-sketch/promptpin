"""Thin client library for applications.

Design: synchronous, no threads, no network (stdlib + PyYAML only).
``resolve`` reads the committed ``promptpin.lock`` (versions pinned at
deploy time — nothing on the request hot path). ``log_serve`` appends one
row to the local SQLite database; a local append is microseconds, so it
runs inline rather than in a background thread (threads in user processes
break under fork/gevent/serverless freeze — async batching is intentionally
not the default).

Sampling: pass ``sample_rate`` (0..1); calls are dropped probabilistically
*before* touching the database.
"""

from __future__ import annotations

import os
import random
import threading
from pathlib import Path
from re import Match

from promptpin import statedb, yamlsafe
from promptpin.registry import (
    FORMATS,
    VARIABLE_RE,
    config_digest,
    is_valid_name,
    split_messages,
)


def resolve(lock_path: str | Path, env: str, prompt: str) -> dict:
    """Return the pin for ``(env, prompt)`` from a lock file.

    Accepts either the committed ``promptpin.lock`` (``{"envs": {env: ...}}``)
    or the minimal manifest from ``promptpin lock --env E --out``
    (``{"env": E, "prompts": ...}``) — the exported manifest only carries
    ``sha``, ``model``, ``params``, ``template_sha256``. Raises KeyError
    when not pinned. For the template text too, use ``load_bundle`` on a
    directory produced by ``promptpin export``.
    """
    lock_path = Path(lock_path)
    data = yamlsafe.load(lock_path.read_text(encoding="utf-8")) or {}
    pin = None
    if isinstance(data.get("envs"), dict):
        try:
            pin = data["envs"][env][prompt]
        except KeyError:
            pin = None
    elif data.get("env") == env and isinstance(data.get("prompts"), dict):
        pin = data["prompts"].get(prompt)
    if pin is None:
        raise KeyError(f"no pin for prompt {prompt!r} in env {env!r} ({lock_path})")
    return dict(pin)


def load_bundle(bundle_dir: str | Path, expected_digest: str | None = None) -> dict:
    """Load a bundle produced by ``promptpin export``.

    Returns ``{name: {sha, ref, model, params, format, template_sha256,
    digest, file, template}}``. Fails loudly instead of serving the wrong
    prompt when:

    - a template file's bytes don't match its ``template_sha256``;
    - an entry's ``digest`` doesn't cover its template hash, model, params
      and format (so editing ``model`` or ``params`` in the manifest fails);
    - the manifest's ``bundle_digest`` doesn't match its entries.

    These digests are unkeyed: they catch drift and casual edits, not an
    attacker who can rewrite the whole bundle. For that, pass
    ``expected_digest`` — the ``bundle_digest`` printed by ``promptpin
    export`` — from somewhere the attacker can't write (your build config or
    deploy environment), and any change to the bundle is rejected.
    """
    return load_bundle_with_manifest(bundle_dir, expected_digest)[0]


def load_bundle_with_manifest(
    bundle_dir: str | Path, expected_digest: str | None = None
) -> tuple[dict, dict]:
    """``load_bundle`` plus the parsed manifest, from a single read of it.

    Every failure is a ``ValueError`` (or ``OSError`` for a missing file), so a
    caller can treat "anything went wrong" as "verification failed".
    """
    import hashlib

    bundle_dir = Path(bundle_dir)
    manifest_path = bundle_dir / "manifest.yaml"
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"{manifest_path} not found — is this a `promptpin export` directory?"
        )
    try:
        manifest = yamlsafe.load(yamlsafe.decode(manifest_path.read_bytes(), "manifest.yaml"))
    except yamlsafe.yaml.YAMLError as exc:
        raise ValueError(f"{manifest_path}: not valid YAML: {exc}") from None
    manifest = manifest or {}
    prompts = manifest.get("prompts") if isinstance(manifest, dict) else None
    if not isinstance(prompts, dict) or not prompts:
        # Fail closed: an empty or malformed bundle must not "load" as zero prompts.
        raise ValueError(f"{manifest_path}: no prompts in bundle manifest — re-export the bundle")
    result = {}
    for name, meta in prompts.items():
        if not is_valid_name(name) or not isinstance(meta, dict):
            raise ValueError(f"{manifest_path}: invalid bundle entry {name!r}")
        expected_file = f"{name}.template.md"
        if meta.get("file") != expected_file:
            # The manifest is data; never let it point outside the bundle.
            raise ValueError(
                f"{manifest_path}: entry {name!r} must reference {expected_file!r},"
                f" got {meta.get('file')!r}"
            )
        expected_hash = meta.get("template_sha256")
        if not isinstance(expected_hash, str) or len(expected_hash) != 64:
            raise ValueError(f"{manifest_path}: entry {name!r} has no valid template_sha256")
        # Symlinks are fine (Kubernetes ConfigMap volumes use them): the digest
        # check below covers whatever the link resolves to.
        raw = (bundle_dir / expected_file).read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest != expected_hash:
            raise ValueError(
                f"bundle template for {name!r} does not match the pinned sha256 "
                f"(expected {expected_hash[:12]}…, got {digest[:12]}…) — "
                "re-export the bundle"
            )
        fmt = meta.get("format", "text")
        params = meta.get("params") if meta.get("params") is not None else {}
        model = meta.get("model") or ""
        if not isinstance(params, dict) or not isinstance(model, str) or fmt not in FORMATS:
            raise ValueError(f"{manifest_path}: entry {name!r} has invalid model/params/format")
        want = config_digest(digest, model, params, fmt)
        if meta.get("digest") != want:
            raise ValueError(
                f"bundle entry {name!r}: model/params/format do not match its digest"
                " (manifest edited, or exported by promptpin < 0.2) — re-export the bundle"
            )
        try:
            template = yamlsafe.decode(raw, expected_file)
        except ValueError as exc:
            raise ValueError(f"bundle template for {name!r}: {exc}") from None
        result[name] = {
            **meta,
            "format": fmt,
            "params": params,
            "model": model,
            "template": template,
        }
    from promptpin.deploys import bundle_digest

    actual = bundle_digest(result)
    if manifest.get("bundle_digest") != actual:
        raise ValueError(f"{manifest_path}: bundle_digest does not match its entries — re-export")
    if expected_digest is not None and expected_digest != actual:
        raise ValueError(
            f"bundle digest {actual[:12]}… is not the expected {str(expected_digest)[:12]}…"
            " — this is not the bundle your build produced"
        )
    return result, manifest


def render_template(template: str, variables: dict | None = None, **kwargs) -> str:
    """Substitute ``{{var}}`` placeholders. Missing variables raise KeyError."""
    values = dict(variables or {})
    values.update(kwargs)

    def replace(match: Match) -> str:
        name = match.group(1)
        if name not in values:
            raise KeyError(f"template variable {name!r} was not provided")
        return str(values[name])

    return VARIABLE_RE.sub(replace, template)


def render_messages(template: str, variables: dict | None = None, **kwargs) -> list[dict]:
    """Render a ``format: chat`` template into ``[{"role", "content"}]``.

    The template is split into messages *before* variables are substituted,
    so a variable value containing a line like ``system:`` stays inside its
    message — user input cannot forge a new role. Missing variables raise
    KeyError; a malformed chat template raises ValueError.
    """
    return [
        {"role": m["role"], "content": render_template(m["content"], variables, **kwargs)}
        for m in split_messages(template)
    ]


def render(entry: dict, variables: dict | None = None, **kwargs) -> str | list[dict]:
    """Render a ``load_bundle`` entry: a string for text prompts, messages for chat."""
    if entry.get("format") == "chat":
        return render_messages(entry["template"], variables, **kwargs)
    return render_template(entry["template"], variables, **kwargs)


#: One connection per (process, thread, database). Opening and closing a SQLite
#: connection per request costs a WAL checkpoint and fsync each time (about
#: 25 ms on the test machine); a cached connection makes an append microseconds.
_LOCAL = threading.local()
#: Seconds a writer waits for another writer before giving up.
LOG_BUSY_TIMEOUT = 5.0


def _cache_key(db_path: Path) -> tuple[int, str]:
    return (os.getpid(), str(db_path.resolve()))


def _connection(db_path: Path):
    import sqlite3

    conns = getattr(_LOCAL, "conns", None)
    if conns is None:
        conns = _LOCAL.conns = {}
    key = _cache_key(db_path)
    cached = conns.get(key)
    conn = None
    if cached is not None:
        conn, ident = cached
        try:
            st = os.stat(db_path)
            same = (st.st_ino, st.st_dev) == ident
        except OSError:
            same = False
        if not same:
            # The file was deleted or replaced: appending to the old inode would lose rows.
            conns.pop(key, None)
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            conn = None
    if conn is None:
        # Drop connections inherited from a parent process (fork) or left over.
        for old in [k for k in conns if k[0] != key[0]]:
            conns.pop(old, None)
        if not db_path.exists():
            # Leftovers of a deleted database would be replayed into the new one.
            for suffix in ("-wal", "-shm"):
                try:
                    os.unlink(str(db_path) + suffix)
                except OSError:
                    pass
        conn = sqlite3.connect(db_path, timeout=LOG_BUSY_TIMEOUT)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(statedb.SCHEMA)
        statedb.migrate(conn)
        if len(conns) >= 8:
            conns.pop(next(iter(conns)))[0].close()
        st = os.stat(db_path)
        conns[key] = (conn, (st.st_ino, st.st_dev))
    return conn


def _drop_connection(db_path: Path) -> None:
    conns = getattr(_LOCAL, "conns", None) or {}
    cached = conns.pop(_cache_key(db_path), None)
    if cached is not None:
        try:
            cached[0].close()
        except Exception:  # noqa: BLE001
            pass


def log_serve(
    db_path: str | Path,
    *,
    prompt: str,
    sha: str,
    env: str = "production",
    model: str = "",
    request_id: str = "",
    latency_ms: float | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    error: str = "",
    sample_rate: float = 1.0,
    digest: str = "",
    rendered_sha256: str = "",
    on_error: str = "raise",
) -> bool:
    """Record that ``sha`` of ``prompt`` served a request.

    Returns True when the event was recorded, False when dropped by sampling
    (or, with ``on_error="ignore"``, when the write failed). Synchronous local
    SQLite append, safe to call inline per request: WAL mode, a busy timeout
    (concurrent writers wait instead of failing) and no per-call schema work
    after the first call for a database.

    ``on_error="raise"`` (default) lets a failed write propagate, so a request
    path never silently loses audit rows. ``on_error="ignore"`` prefers
    availability: a failed write returns False; ``promptpin log verify`` then
    reports the gap as "no requests logged".
    """
    if not 0.0 <= sample_rate <= 1.0:
        raise ValueError("sample_rate must be between 0 and 1")
    if on_error not in ("raise", "ignore"):
        raise ValueError('on_error must be "raise" or "ignore"')
    if sample_rate < 1.0 and random.random() >= sample_rate:
        return False
    import sqlite3

    db_path = Path(db_path)
    row = (
        statedb.utcnow(),
        env,
        prompt,
        sha,
        model,
        request_id,
        latency_ms,
        tokens_in,
        tokens_out,
        error,
        digest,
        rendered_sha256,
    )
    insert = (
        "INSERT INTO serving (ts, env, prompt, sha, model, request_id,"
        " latency_ms, tokens_in, tokens_out, error, digest, rendered_sha256)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in (0, 1):
            conn = _connection(db_path)
            try:
                conn.execute(insert, row)
                conn.commit()
                return True
            except sqlite3.OperationalError as exc:
                _drop_connection(db_path)
                # The file was deleted or replaced under us: rebuild the schema once.
                if attempt == 0 and "no such table" in str(exc):
                    continue
                raise
            except sqlite3.Error:
                _drop_connection(db_path)
                raise
    except (sqlite3.Error, OSError):
        if on_error == "raise":
            raise
        return False
    return False
    return False
