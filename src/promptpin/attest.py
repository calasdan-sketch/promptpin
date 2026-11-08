"""Offline release attestations: an in-toto Statement in a DSSE envelope.

``promptpin attest`` writes a standard in-toto Statement v1 (subjects = each
pinned template's SHA-256) whose predicate records, per prompt, the commit,
model, params, format and config digest, plus the SHA-256 of ``promptpin.lock``.
It is wrapped in a DSSE envelope. Signing here is **HMAC-SHA256 with a shared
secret** (stdlib only, offline): anyone who can verify can also forge, so it
proves "built by someone holding the key", not public non-repudiation.

The statement is plain in-toto JSON, so you can sign the same bytes with a
public-key tool of your choice; promptpin does not implement or test Sigstore.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path

from promptpin import deploys, gitutil
from promptpin.config import LOCK_FILENAME
from promptpin.errors import PromptpinError

PAYLOAD_TYPE = "application/vnd.in-toto+json"
STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
PREDICATE_TYPE = "urn:promptpin:attestation:v1"
MIN_KEY_BYTES = 32
MIN_DISTINCT_BYTES = 8
MAX_CLOCK_SKEW_SECONDS = 300.0


class AttestationError(PromptpinError):
    pass


def _pae(payload_type: str, payload: bytes) -> bytes:
    """DSSE v1 pre-authentication encoding."""
    t = payload_type.encode("utf-8")
    return b"DSSEv1 %d %s %d %s" % (len(t), t, len(payload), payload)


def keyid(key: bytes) -> str:
    return "hmac-sha256:" + hashlib.sha256(key).hexdigest()[:16]


def _check_key(key: bytes) -> bytes:
    if not isinstance(key, (bytes, bytearray)) or len(key) < MIN_KEY_BYTES:
        raise AttestationError(
            f"signing key must be at least {MIN_KEY_BYTES} bytes - make one with:"
            ' python -c "import secrets; print(secrets.token_hex(32))"'
        )
    if len(set(key)) < MIN_DISTINCT_BYTES:
        raise AttestationError("signing key is too repetitive to be a real secret")
    return bytes(key)


def build_statement(repo_root: Path, env: str) -> dict:
    top = gitutil.ensure_repo(repo_root)
    pins = deploys.read_lock(top).get(env)
    if not pins:
        raise AttestationError(f"no pins for env {env!r} — nothing to attest")
    checks = [c for c in deploys.verify_lock(top, env=env) if c.level == "error"]
    if checks:
        raise AttestationError(
            f"refusing to attest: verify failed for {env!r}: {checks[0].message}"
            " — run `promptpin verify`"
        )
    try:
        lock_bytes = (top / LOCK_FILENAME).read_bytes()
    except OSError as exc:
        raise AttestationError(f"cannot read {LOCK_FILENAME}: {exc}") from None
    entries = {
        n: {
            "sha": p.sha,
            "ref": p.ref,
            "model": p.model,
            "params": p.params,
            "format": p.format or "text",
            "digest": p.digest,
        }
        for n, p in sorted(pins.items())
    }
    bundle = deploys.bundle_digest(
        {n: {"sha": e["sha"], "digest": e["digest"]} for n, e in entries.items()}
    )
    return {
        "_type": STATEMENT_TYPE,
        "subject": [
            {"name": f"prompt/{n}", "digest": {"sha256": p.template_sha256}}
            for n, p in sorted(pins.items())
        ],
        "predicateType": PREDICATE_TYPE,
        "predicate": {
            "env": env,
            "bundle_digest": bundle,
            "created_at": deploys._utc_iso(),
            "lock_sha256": hashlib.sha256(lock_bytes).hexdigest(),
            "repo_head": gitutil.head_sha(top),
            "prompts": entries,
        },
    }


def sign(statement: dict, key: bytes) -> dict:
    """DSSE envelope with an HMAC-SHA256 signature over the statement."""
    key = _check_key(key)
    payload = json.dumps(statement, sort_keys=True, separators=(",", ":")).encode("utf-8")
    sig = hmac.new(key, _pae(PAYLOAD_TYPE, payload), hashlib.sha256).digest()
    return {
        "payloadType": PAYLOAD_TYPE,
        "payload": base64.b64encode(payload).decode("ascii"),
        "signatures": [{"keyid": keyid(key), "sig": base64.b64encode(sig).decode("ascii")}],
    }


def open_envelope(envelope: object, key: bytes) -> dict:
    """Verify the HMAC and return the statement. Raises AttestationError."""
    key = _check_key(key)
    if not isinstance(envelope, dict) or envelope.get("payloadType") != PAYLOAD_TYPE:
        raise AttestationError("not a promptpin attestation (wrong payloadType)")
    try:
        payload = base64.b64decode(envelope["payload"], validate=True)
        sigs = [base64.b64decode(s["sig"], validate=True) for s in envelope["signatures"]]
    except (KeyError, TypeError, ValueError):
        raise AttestationError("malformed attestation envelope") from None
    # Base64 can encode the same bytes in more than one way (stray bits in the last
    # character). Accept only the canonical text, so one signed payload has one file form.
    if base64.b64encode(payload).decode("ascii") != envelope["payload"] or any(
        base64.b64encode(raw).decode("ascii") != s["sig"]
        for raw, s in zip(sigs, envelope["signatures"])
    ):
        raise AttestationError("malformed attestation envelope (non-canonical base64)")
    want = hmac.new(key, _pae(PAYLOAD_TYPE, payload), hashlib.sha256).digest()
    if not any(hmac.compare_digest(want, s) for s in sigs):
        raise AttestationError("attestation signature does not match the key")
    try:
        statement = json.loads(payload.decode("utf-8"))
    except ValueError:
        raise AttestationError("attestation payload is not JSON") from None
    if (
        not isinstance(statement, dict)
        or statement.get("_type") != STATEMENT_TYPE
        or statement.get("predicateType") != PREDICATE_TYPE
        or not isinstance(statement.get("predicate"), dict)
    ):
        raise AttestationError("attestation is not a promptpin in-toto statement")
    return statement


def read_verified(path: str | Path, key: bytes) -> dict:
    try:
        envelope = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AttestationError(f"cannot read attestation {path}: {exc}") from None
    return open_envelope(envelope, key)


def check_against_repo(repo_root: Path, statement: dict, env: str | None = None) -> list[str]:
    """Problems between the attested predicate and the repo's current lock and git."""
    pred = statement["predicate"]
    problems = []
    if env is not None and pred.get("env") != env:
        return [f"attestation is for env {pred.get('env')!r}, not {env!r}"]
    top = gitutil.ensure_repo(repo_root)
    try:
        lock_hash = hashlib.sha256((top / LOCK_FILENAME).read_bytes()).hexdigest()
    except OSError as exc:
        return [f"cannot read {LOCK_FILENAME}: {exc}"]
    if pred.get("lock_sha256") != lock_hash:
        # The lock may legitimately change for *other* envs; compare this env's pins instead.
        try:
            current = build_statement(top, str(pred.get("env")))["predicate"]
        except PromptpinError as exc:
            return [str(exc)]
        if current["prompts"] != pred.get("prompts"):
            problems.append("the lock's pins for this env differ from the attested pins")
        elif current["bundle_digest"] != pred.get("bundle_digest"):
            problems.append("bundle digest differs from the attested one")
    return problems


def check_freshness(statement: dict, max_age_seconds: float, now: float | None = None) -> list[str]:
    """Problems with the attestation's age. An attestation with no valid timestamp fails closed."""
    import datetime
    import time

    created = statement["predicate"].get("created_at")
    try:
        when = datetime.datetime.fromisoformat(str(created).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return ["attestation has no valid created_at, so its age cannot be checked"]
    age = (time.time() if now is None else now) - when
    if age < -MAX_CLOCK_SKEW_SECONDS:
        return [f"attestation is dated in the future ({created})"]
    if age > max_age_seconds:
        return [f"attestation is {int(age // 3600)}h old, older than the allowed maximum"]
    return []
