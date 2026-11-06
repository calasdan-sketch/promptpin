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

import random
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
    import hashlib

    bundle_dir = Path(bundle_dir)
    manifest_path = bundle_dir / "manifest.yaml"
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"{manifest_path} not found — is this a `promptpin export` directory?"
        )
    manifest = yamlsafe.load(manifest_path.read_text(encoding="utf-8")) or {}
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
    return result


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
) -> bool:
    """Record that ``sha`` of ``prompt`` served a request.

    Returns True when the event was recorded, False when dropped by sampling.
    Synchronous local SQLite append — safe to call inline per request.
    """
    if not 0.0 <= sample_rate <= 1.0:
        raise ValueError("sample_rate must be between 0 and 1")
    if sample_rate < 1.0 and random.random() >= sample_rate:
        return False
    import sqlite3

    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(statedb.SCHEMA)
        conn.execute(
            "INSERT INTO serving (ts, env, prompt, sha, model, request_id,"
            " latency_ms, tokens_in, tokens_out, error)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
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
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return True
