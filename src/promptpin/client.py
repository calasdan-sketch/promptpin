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

import yaml

from promptpin import statedb
from promptpin.registry import VARIABLE_RE


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
    data = yaml.safe_load(lock_path.read_text(encoding="utf-8")) or {}
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


def load_bundle(bundle_dir: str | Path) -> dict:
    """Load a bundle produced by ``promptpin export``.

    Returns ``{name: {sha, ref, model, params, template_sha256, file,
    template}}``. Every template is hash-checked against the pinned
    ``template_sha256`` — a bundle that drifted from its pin fails loudly
    instead of serving the wrong prompt.
    """
    import hashlib

    bundle_dir = Path(bundle_dir)
    manifest_path = bundle_dir / "manifest.yaml"
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"{manifest_path} not found — is this a `promptpin export` directory?"
        )
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    result = {}
    for name, meta in (manifest.get("prompts") or {}).items():
        template = (bundle_dir / meta["file"]).read_text(encoding="utf-8")
        digest = hashlib.sha256(template.encode("utf-8")).hexdigest()
        if digest != meta["template_sha256"]:
            raise ValueError(
                f"bundle template for {name!r} does not match the pinned sha256 "
                f"(expected {meta['template_sha256'][:12]}…, got {digest[:12]}…) — "
                "re-export the bundle"
            )
        result[name] = {**meta, "template": template}
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
