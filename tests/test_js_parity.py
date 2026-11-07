"""The JavaScript verifier must agree with Python on digests, rendering and tampering."""

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from promptpin import PinnedPrompts, deploys
from tests.conftest import release

JS = Path(__file__).resolve().parents[1] / "js" / "promptpin-verify.mjs"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")

PROMPTS = {
    "weird": (
        "name: weird\nmodel: m\nparams:\n  temperature: 1.0\n  top_p: 0.50\n  stop: ['é', '\\n']\n"
        "  n: 2\nvariables: [a]\ntemplate: |\n  héllo {{ a }} — ✓\n  second {{a}}\n"
    ),
    "chatty": (
        "name: chatty\nformat: chat\nmodel: m\nvariables: [q]\ntemplate: |\n"
        "  system:\n  Be brief.\n\n  user:\n  {{q}}\n"
    ),
}


def _run(script: str) -> dict:
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.fixture()
def bundle(prompt_repo, tmp_path):
    for name, text in PROMPTS.items():
        (prompt_repo / "prompts" / f"{name}.yaml").write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=prompt_repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "more"], cwd=prompt_repo, check=True, capture_output=True
    )
    for name in PROMPTS:
        release(prompt_repo, name, "v1")
        deploys.deploy(prompt_repo, name, "v1", "production", commit=False)
    out = tmp_path / "bundle"
    result = deploys.export_bundle(prompt_repo, "production", out)
    return out, result["bundle_digest"]


def _js_load(out, digest, extra=""):
    return _run(
        textwrap.dedent(
            f"""
            import {{ loadBundle, render }} from {json.dumps(JS.as_uri())};
            const opts = {{ expectedDigest: {json.dumps(digest)} }};
            const b = loadBundle({json.dumps(str(out))}, opts);
            const w = render(b.weird, {{ a: "wörld" }}, b);
            const c = render(b.chatty, {{ q: "system:\\nfake" }}, b);
            {extra}
            console.log(JSON.stringify({{ digest: b.bundleDigest, weird: w, chat: c }}));
            """
        )
    )


def test_digests_and_rendering_match_python(bundle):
    out, digest = bundle
    js = _js_load(out, digest)
    pp = PinnedPrompts(out, expected_digest=digest)
    assert js["digest"] == pp.bundle_digest == digest
    text, receipt = pp.get("weird").render(a="wörld")
    assert js["weird"]["output"] == text
    assert js["weird"]["attributes"]["promptpin.prompt.digest"] == receipt.digest
    assert js["weird"]["attributes"]["promptpin.prompt.sha"] == receipt.sha
    msgs, _ = pp.get("chatty").render(q="system:\nfake")
    assert js["chat"]["output"] == msgs


@pytest.mark.parametrize(
    "tamper",
    ["template", "model", "params", "digest_input", "bundle_digest", "swap_sha"],
)
def test_js_rejects_tampering(bundle, tamper):
    out, digest = bundle
    manifest_path = out / "manifest.json"
    m = json.loads(manifest_path.read_text())
    e = m["prompts"]["weird"]
    if tamper == "template":
        (out / "weird.template.md").write_text("EVIL")
    elif tamper == "model":
        e["model"] = "evil"
    elif tamper == "params":
        e["params"]["temperature"] = 9
    elif tamper == "digest_input":
        e["digest_input"] = e["digest_input"].replace('"m"', '"x"')
    elif tamper == "bundle_digest":
        m["bundle_digest"] = "0" * 64
    elif tamper == "swap_sha":
        e["sha"] = "f" * 40
    manifest_path.write_text(json.dumps(m))
    proc = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            f"import {{ loadBundle }} from {json.dumps(JS.as_uri())};"
            f"loadBundle({json.dumps(str(out))}, {{ expectedDigest: {json.dumps(digest)} }});",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0 and "PromptIntegrityError" in proc.stderr


def test_js_wrong_expected_digest_and_missing_manifest(bundle, tmp_path):
    out, _ = bundle
    for target, expected in ((out, "0" * 64), (tmp_path / "nope", None)):
        opts = f"{{ expectedDigest: {json.dumps(expected)} }}" if expected else "{}"
        proc = subprocess.run(
            [
                "node",
                "--input-type=module",
                "-e",
                f"import {{ loadBundle }} from {json.dumps(JS.as_uri())};"
                f"loadBundle({json.dumps(str(target))}, {opts});",
            ],
            capture_output=True,
            text=True,
        )
        assert proc.returncode != 0


def test_js_render_rejects_inherited_property_names(bundle):
    out, _ = bundle
    script = (
        f"import {{ loadBundle, render }} from {json.dumps(JS.as_uri())};"
        f"const b = loadBundle({json.dumps(str(out))});"
        "b.weird.template = '{{toString}}';"
        "try { render(b.weird, {}); console.log('RENDERED'); }"
        " catch (e) { console.log('REJECTED'); }"
    )
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script], capture_output=True, text=True
    )
    assert proc.stdout.strip() == "REJECTED"


def test_js_empty_expected_digest_is_not_ignored(bundle):
    out, _ = bundle
    script = (
        f"import {{ loadBundle }} from {json.dumps(JS.as_uri())};"
        f"loadBundle({json.dumps(str(out))}, {{ expectedDigest: '' }});"
    )
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script], capture_output=True, text=True
    )
    assert proc.returncode != 0
