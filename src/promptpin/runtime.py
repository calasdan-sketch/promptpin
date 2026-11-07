"""Runtime verifier: prove, per request, which prompt bytes were served.

``PinnedPrompts`` loads an exported bundle, checks every digest at startup,
re-checks the in-memory template on every ``get()`` (one SHA-256 of a few KB:
microseconds), re-reads the bundle from disk at most every
``reverify_interval`` seconds, and hands back a ``Receipt`` that names the
exact commit, digests and rendered-prompt hash. A receipt can be attached to
an OpenTelemetry span and written to the serving log, so ``promptpin log
verify`` can later prove production served only the pinned prompt.

Failure policy (``on_failure``):

``"raise"`` (default, fail closed)
    A failed check raises ``PromptIntegrityError``; nothing is served.
``"last_good"``
    A failed re-check keeps serving the last bundle that *passed* every check
    (never unverified bytes) and marks the receipt ``verified=False``; the
    ``on_event`` callback sees the failure. With no last-good bundle (a failure
    at startup) it still raises: there is nothing safe to serve.

Receipts prove what *this process* verified and rendered. They are not proof
against a compromised application process; see the README limits.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from promptpin.client import load_bundle, render_messages, render_template
from promptpin.errors import PromptpinError

ON_FAILURE = ("raise", "last_good")


class PromptIntegrityError(PromptpinError):
    """The bundle, a template or a digest failed verification."""


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Receipt:
    """Evidence of exactly which prompt a request used."""

    env: str
    prompt: str
    sha: str
    digest: str
    template_sha256: str
    bundle_digest: str
    model: str
    verified: bool
    rendered_sha256: str = ""

    def attributes(self) -> dict:
        """Flat attributes for a trace span (OpenTelemetry-compatible types).

        Keys use a ``promptpin.`` namespace; they are not part of any OpenTelemetry
        semantic convention.
        """
        attrs = {
            "promptpin.env": self.env,
            "promptpin.prompt.name": self.prompt,
            "promptpin.prompt.sha": self.sha,
            "promptpin.prompt.digest": self.digest,
            "promptpin.prompt.template_sha256": self.template_sha256,
            "promptpin.bundle.digest": self.bundle_digest,
            "promptpin.verified": self.verified,
        }
        if self.rendered_sha256:
            attrs["promptpin.prompt.rendered_sha256"] = self.rendered_sha256
        return attrs

    def annotate_span(self, span=None) -> bool:
        """Set the attributes on ``span`` (default: the current OpenTelemetry span).

        Returns False, without error, when OpenTelemetry is not installed or no
        recording span is active. promptpin does not depend on OpenTelemetry.
        """
        if span is None:
            try:
                from opentelemetry import trace  # type: ignore[import-not-found]
            except ImportError:
                return False
            span = trace.get_current_span()
        recording = getattr(span, "is_recording", None)
        if recording is not None and not recording():
            return False
        for key, value in self.attributes().items():
            span.set_attribute(key, value)
        return True

    def log(self, db_path: str | Path, **kwargs) -> bool:
        """Append this receipt to the serving log (see ``promptpin.log_serve``)."""
        from promptpin.client import log_serve

        return log_serve(
            db_path,
            prompt=self.prompt,
            sha=self.sha,
            env=self.env,
            model=self.model,
            digest=self.digest,
            rendered_sha256=self.rendered_sha256,
            **kwargs,
        )


@dataclass
class ServedPrompt:
    """A verified prompt entry plus the receipt for this request."""

    entry: dict
    receipt: Receipt

    @property
    def template(self) -> str:
        return self.entry["template"]

    @property
    def model(self) -> str:
        return self.entry["model"]

    @property
    def params(self) -> dict:
        return dict(self.entry["params"])

    def render(self, variables: dict | None = None, **kwargs):
        """Render the template; returns a new ``ServedPrompt`` whose receipt carries
        ``rendered_sha256`` (hash of exactly the text or messages produced)."""
        if self.entry["format"] == "chat":
            out = render_messages(self.template, variables, **kwargs)
            canonical = json.dumps(out, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        else:
            out = render_template(self.template, variables, **kwargs)
            canonical = out
        receipt = Receipt(**{**self.receipt.__dict__, "rendered_sha256": _sha256(canonical)})
        return out, receipt


class PinnedPrompts:
    """Verified, in-memory view of an exported bundle. Thread-safe."""

    def __init__(
        self,
        bundle_dir: str | Path,
        *,
        expected_digest: str | None = None,
        env: str | None = None,
        on_failure: str = "raise",
        reverify_interval: float | None = 30.0,
        on_event=None,
        clock=time.monotonic,
        attestation: str | Path | None = None,
        attestation_key: bytes | None = None,
    ):
        if on_failure not in ON_FAILURE:
            raise ValueError(f"on_failure must be one of {ON_FAILURE}")
        if reverify_interval is not None and reverify_interval < 0:
            raise ValueError("reverify_interval must be >= 0 or None")
        self._dir = Path(bundle_dir)
        if attestation is not None:
            # The signed statement supplies the digest the bundle must match.
            from promptpin import attest

            if attestation_key is None:
                raise ValueError("attestation needs attestation_key")
            try:
                statement = attest.read_verified(attestation, attestation_key)
            except PromptpinError as exc:
                raise PromptIntegrityError(str(exc)) from exc
            attested = statement["predicate"].get("bundle_digest")
            if expected_digest is not None and expected_digest != attested:
                raise PromptIntegrityError("expected_digest disagrees with the attestation")
            expected_digest = attested
            env = env or statement["predicate"].get("env")
        self._expected = expected_digest
        self._policy = on_failure
        self._interval = reverify_interval
        self._on_event = on_event
        self._clock = clock
        self._lock = threading.Lock()
        self._bundle: dict = {}
        self._bundle_digest = ""
        self._env = env or ""
        self._healthy = False
        self._checked_at = 0.0
        self._load(initial=True, env=env)

    # -- loading ---------------------------------------------------------

    def _read(self) -> tuple[dict, str, str]:
        from promptpin import yamlsafe
        from promptpin.deploys import bundle_digest

        bundle = load_bundle(self._dir, expected_digest=self._expected)
        manifest = yamlsafe.load((self._dir / "manifest.yaml").read_text(encoding="utf-8")) or {}
        env = manifest.get("env") if isinstance(manifest, dict) else ""
        return bundle, bundle_digest(bundle), env if isinstance(env, str) else ""

    def _emit(self, kind: str, detail: str) -> None:
        if self._on_event is not None:
            try:
                self._on_event(kind, detail)
            except Exception:  # noqa: BLE001 - a broken callback must not break serving
                pass

    def _load(self, *, initial: bool, env: str | None = None) -> None:
        try:
            bundle, digest, bundle_env = self._read()
            if env and bundle_env != env:
                raise ValueError(f"bundle is for env {bundle_env!r}, expected {env!r}")
        except (OSError, ValueError, KeyError) as exc:
            self._healthy = False
            self._emit("verify_failed", str(exc))
            if initial or self._policy == "raise" or not self._bundle:
                raise PromptIntegrityError(f"prompt bundle failed verification: {exc}") from exc
            self._checked_at = self._clock()
            return
        if not initial and digest != self._bundle_digest:
            self._emit("bundle_changed", f"{self._bundle_digest[:12]} -> {digest[:12]}")
        self._bundle, self._bundle_digest, self._env = bundle, digest, bundle_env or self._env
        self._healthy = True
        self._checked_at = self._clock()

    # -- per request -----------------------------------------------------

    def check(self) -> bool:
        """Re-read and fully re-verify the bundle now. True when it passed."""
        with self._lock:
            self._load(initial=False)
            return self._healthy

    @property
    def bundle_digest(self) -> str:
        return self._bundle_digest

    @property
    def env(self) -> str:
        return self._env

    def names(self) -> list[str]:
        return sorted(self._bundle)

    def get(self, name: str) -> ServedPrompt:
        """The verified prompt ``name`` and a receipt. Never returns unverified bytes."""
        with self._lock:
            if self._interval is not None and self._clock() - self._checked_at >= self._interval:
                self._load(initial=False)
            entry = self._bundle.get(name)
            if entry is None:
                raise KeyError(f"prompt {name!r} is not in the bundle")
            # Cheap per-request guard: the in-memory template must still hash to its pin.
            if _sha256(entry["template"]) != entry["template_sha256"]:
                self._healthy = False
                self._emit("memory_mismatch", name)
                raise PromptIntegrityError(f"in-memory template for {name!r} changed after load")
            receipt = Receipt(
                env=self._env,
                prompt=name,
                sha=entry["sha"],
                digest=entry["digest"],
                template_sha256=entry["template_sha256"],
                bundle_digest=self._bundle_digest,
                model=entry["model"],
                verified=self._healthy,
            )
            copy = {**entry, "params": dict(entry["params"])}
            return ServedPrompt(entry=copy, receipt=receipt)
