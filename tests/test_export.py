"""Export bundle: pinned templates for runtime use, with integrity checks."""

import pytest
import yaml

from promptpin import deploys
from promptpin.client import load_bundle
from promptpin.errors import DeployError
from tests.conftest import release


def _setup(prompt_repo, tmp_path):
    release(prompt_repo, "classifier", "v1")
    deploys.deploy(prompt_repo, "classifier", "v1", "production", commit=False)
    out = tmp_path / "bundle"
    result = deploys.export_bundle(prompt_repo, "production", out)
    return out, result


def test_export_writes_manifest_and_templates(prompt_repo, tmp_path):
    out, result = _setup(prompt_repo, tmp_path)
    assert result["prompts"] == ["classifier"]
    manifest = yaml.safe_load((out / "manifest.yaml").read_text())
    assert manifest["env"] == "production"
    assert manifest["prompts"]["classifier"]["sha"]
    template = (out / "classifier.template.md").read_text()
    assert "Classify this ticket" in template


def test_load_bundle_roundtrip(prompt_repo, tmp_path):
    out, _ = _setup(prompt_repo, tmp_path)
    bundle = load_bundle(out)
    assert "Classify this ticket" in bundle["classifier"]["template"]
    assert bundle["classifier"]["model"] == "gpt-4o-mini"
    assert len(bundle["classifier"]["template_sha256"]) == 64


def test_load_bundle_detects_tampering(prompt_repo, tmp_path):
    out, _ = _setup(prompt_repo, tmp_path)
    (out / "classifier.template.md").write_text("HACKED template", encoding="utf-8")
    with pytest.raises(ValueError, match="does not match the pinned sha256"):
        load_bundle(out)


def test_load_bundle_missing_manifest(tmp_path):
    with pytest.raises(FileNotFoundError, match="manifest.yaml"):
        load_bundle(tmp_path / "empty")


def test_export_empty_env_raises(prompt_repo, tmp_path):
    with pytest.raises(DeployError, match="no pins"):
        deploys.export_bundle(prompt_repo, "production", tmp_path / "b")


def test_export_after_new_version_keeps_old_bundle_valid(prompt_repo, tmp_path):
    """Exporting v2 does not invalidate a previously exported v1 bundle."""
    from tests.conftest import CLASSIFIER_V2, commit_prompt

    out_v1, _ = _setup(prompt_repo, tmp_path)
    bundle_v1_path = tmp_path / "bundle-v1"
    (tmp_path / "bundle").rename(bundle_v1_path)

    commit_prompt(prompt_repo, "classifier", CLASSIFIER_V2, "v2")
    release(prompt_repo, "classifier", "v2")
    deploys.deploy(prompt_repo, "classifier", "v2", "production", commit=False)
    out_v2 = tmp_path / "bundle-v2"
    deploys.export_bundle(prompt_repo, "production", out_v2)

    b1 = load_bundle(bundle_v1_path)
    b2 = load_bundle(out_v2)
    assert "spam" not in b1["classifier"]["template"]
    assert "spam" in b2["classifier"]["template"]
    assert b1["classifier"]["sha"] != b2["classifier"]["sha"]


def test_cli_export(prompt_repo, tmp_path, capsys):
    from promptpin.cli import main

    release(prompt_repo, "classifier", "v1")
    main(
        [
            "--repo",
            str(prompt_repo),
            "deploy",
            "classifier@v1",
            "--env",
            "production",
            "--no-commit",
        ]
    )
    out = tmp_path / "b"
    rc = main(["--repo", str(prompt_repo), "export", "--env", "production", "--out", str(out)])
    assert rc == 0
    printed = capsys.readouterr().out
    assert "exported 1 prompts" in printed
    assert (out / "manifest.yaml").exists()


def test_release_warns_on_dirty_prompt_file(prompt_repo, capsys):
    from promptpin.cli import main

    (prompt_repo / "prompts" / "classifier.yaml").write_text(
        "name: classifier\ntemplate: dirty edit\n", encoding="utf-8"
    )
    rc = main(["--repo", str(prompt_repo), "release", "classifier"])
    assert rc == 0  # warns, does not block
    assert "uncommitted changes" in capsys.readouterr().out
