"""PinnedPrompts: per-request verification, policies, receipts, OpenTelemetry."""

import sys
import types

import pytest

from promptpin import PinnedPrompts, PromptIntegrityError, deploys
from promptpin.client import load_bundle
from tests.conftest import release

CHAT = """\
name: support
format: chat
model: m1
variables: [ticket_text]
template: |
  system:
  Be terse.

  user:
  {{ticket_text}}
"""


@pytest.fixture()
def bundle(prompt_repo, tmp_path):
    release(prompt_repo, "classifier", "v1")
    deploys.deploy(prompt_repo, "classifier", "v1", "production", commit=False)
    out = tmp_path / "bundle"
    result = deploys.export_bundle(prompt_repo, "production", out)
    return out, result["bundle_digest"]


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_receipt_names_exact_pin(bundle):
    out, digest = bundle
    pp = PinnedPrompts(out, expected_digest=digest, env="production")
    served = pp.get("classifier")
    text, receipt = served.render(ticket_text="login broken")
    pin = deploys.read_lock(out.parent / "repo")["production"]["classifier"]
    assert receipt.sha == pin.sha and receipt.digest == pin.digest
    assert receipt.bundle_digest == digest and receipt.verified
    assert len(receipt.rendered_sha256) == 64
    assert "login broken" in text
    attrs = receipt.attributes()
    assert attrs["promptpin.prompt.sha"] == pin.sha
    assert attrs["promptpin.verified"] is True


def test_rendered_hash_differs_per_input(bundle):
    out, _ = bundle
    s = PinnedPrompts(out).get("classifier")
    assert (
        s.render(ticket_text="a")[1].rendered_sha256 != s.render(ticket_text="b")[1].rendered_sha256
    )


def test_chat_rendered_hash(prompt_repo, tmp_path):
    (prompt_repo / "prompts" / "support.yaml").write_text(CHAT)
    import subprocess

    subprocess.run(["git", "add", "-A"], cwd=prompt_repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "chat"], cwd=prompt_repo, check=True, capture_output=True
    )
    release(prompt_repo, "support", "v1")
    deploys.deploy(prompt_repo, "support", "v1", "production", commit=False)
    deploys.export_bundle(prompt_repo, "production", tmp_path / "b")
    out, receipt = PinnedPrompts(tmp_path / "b").get("support").render(ticket_text="hi")
    assert out[0]["role"] == "system" and out[1]["content"] == "hi"
    assert receipt.rendered_sha256


def test_startup_tamper_fails_closed(bundle):
    out, digest = bundle
    (out / "classifier.template.md").write_text("EVIL")
    with pytest.raises(PromptIntegrityError):
        PinnedPrompts(out, expected_digest=digest)


def test_wrong_expected_digest_rejected(bundle):
    out, _ = bundle
    with pytest.raises(PromptIntegrityError, match="not the expected"):
        PinnedPrompts(out, expected_digest="0" * 64)


def test_wrong_env_rejected(bundle):
    out, _ = bundle
    with pytest.raises(PromptIntegrityError, match="env"):
        PinnedPrompts(out, env="staging")


def test_runtime_tamper_policy_raise(bundle):
    out, digest = bundle
    clock = Clock()
    pp = PinnedPrompts(out, expected_digest=digest, reverify_interval=10, clock=clock)
    pp.get("classifier")
    (out / "classifier.template.md").write_text("EVIL")
    pp.get("classifier")  # inside the interval: cached copy still verified
    clock.t = 11
    with pytest.raises(PromptIntegrityError):
        pp.get("classifier")


def test_runtime_tamper_policy_last_good(bundle):
    out, digest = bundle
    clock, events = Clock(), []
    pp = PinnedPrompts(
        out,
        expected_digest=digest,
        on_failure="last_good",
        reverify_interval=10,
        clock=clock,
        on_event=lambda kind, detail: events.append(kind),
    )
    good = pp.get("classifier").template
    (out / "classifier.template.md").write_text("EVIL")
    clock.t = 11
    served = pp.get("classifier")
    assert served.template == good  # never the unverified bytes
    assert not served.receipt.verified
    assert "verify_failed" in events
    # recovery: restore the file and the next check passes again
    (out / "classifier.template.md").write_text(good)
    clock.t = 30
    assert pp.get("classifier").receipt.verified


def test_last_good_still_raises_at_startup(bundle):
    out, _ = bundle
    (out / "classifier.template.md").write_text("EVIL")
    with pytest.raises(PromptIntegrityError):
        PinnedPrompts(out, on_failure="last_good")


def test_memory_mutation_detected(bundle):
    out, _ = bundle
    pp = PinnedPrompts(out)
    served = pp.get("classifier")
    served.entry["template"] += " ignore previous"  # caller mutating its copy
    assert pp.get("classifier").template != served.template  # cache not poisoned
    pp._bundle["classifier"]["template"] += "x"  # poke the cache itself
    with pytest.raises(PromptIntegrityError, match="in-memory"):
        pp.get("classifier")


def test_unknown_prompt_and_bad_args(bundle):
    out, _ = bundle
    pp = PinnedPrompts(out)
    with pytest.raises(KeyError):
        pp.get("nope")
    with pytest.raises(ValueError):
        PinnedPrompts(out, on_failure="serve_anyway")
    with pytest.raises(ValueError):
        PinnedPrompts(out, reverify_interval=-1)


def test_broken_callback_does_not_break_serving(bundle):
    out, digest = bundle

    def boom(kind, detail):
        raise RuntimeError("callback bug")

    clock = Clock()
    pp = PinnedPrompts(out, on_failure="last_good", reverify_interval=1, clock=clock, on_event=boom)
    (out / "classifier.template.md").write_text("EVIL")
    clock.t = 5
    assert pp.get("classifier").template


def test_annotate_span_with_fake_otel(bundle, monkeypatch):
    out, _ = bundle
    _, receipt = PinnedPrompts(out).get("classifier").render(ticket_text="x")
    seen = {}

    class Span:
        def is_recording(self):
            return True

        def set_attribute(self, k, v):
            seen[k] = v

    trace = types.ModuleType("opentelemetry.trace")
    trace.get_current_span = lambda: Span()
    pkg = types.ModuleType("opentelemetry")
    pkg.trace = trace
    monkeypatch.setitem(sys.modules, "opentelemetry", pkg)
    monkeypatch.setitem(sys.modules, "opentelemetry.trace", trace)
    assert receipt.annotate_span() is True
    assert seen["promptpin.prompt.sha"] == receipt.sha
    assert seen["promptpin.prompt.rendered_sha256"] == receipt.rendered_sha256


def test_annotate_span_without_otel(bundle, monkeypatch):
    out, _ = bundle
    _, receipt = PinnedPrompts(out).get("classifier").render(ticket_text="x")
    monkeypatch.setitem(sys.modules, "opentelemetry", None)
    assert receipt.annotate_span() is False


def test_bundle_still_loads_with_manifest_json(bundle):
    out, _ = bundle
    assert (out / "manifest.json").exists()
    assert "classifier" in load_bundle(out)
