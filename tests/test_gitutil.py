"""gitutil: tag names, ref resolution, mutability detection."""

import pytest

from promptpin import gitutil
from promptpin.errors import NotAGitRepoError, VersionNotFoundError
from tests.conftest import CLASSIFIER_V2, commit_prompt, release


def test_tag_name_format():
    assert gitutil.tag_name("classifier", "v3") == "promptpin/classifier@v3"
    assert gitutil.tag_name("classifier", "3") == "promptpin/classifier@v3"


def test_ensure_repo_rejects_non_repo(tmp_path):
    with pytest.raises(NotAGitRepoError):
        gitutil.ensure_repo(tmp_path / "nope")


def test_resolve_tag_is_immutable(prompt_repo):
    release(prompt_repo, "classifier", "v1")
    sha, display, mutable = gitutil.resolve_prompt_ref(prompt_repo, "classifier", "v1")
    assert not mutable
    assert display == "promptpin/classifier@v1"
    assert len(sha) == 40


def test_resolve_numeric_version_maps_to_tag(prompt_repo):
    release(prompt_repo, "classifier", "v2")
    sha1, _, _ = gitutil.resolve_prompt_ref(prompt_repo, "classifier", "v2")
    sha2, _, _ = gitutil.resolve_prompt_ref(prompt_repo, "classifier", "2")
    assert sha1 == sha2


def test_resolve_sha_is_immutable(prompt_repo):
    sha = gitutil.head_sha(prompt_repo)
    got, _, mutable = gitutil.resolve_prompt_ref(prompt_repo, "classifier", sha)
    assert got == sha and not mutable
    short = sha[:7]
    got2, _, mutable2 = gitutil.resolve_prompt_ref(prompt_repo, "classifier", short)
    assert got2 == sha and not mutable2


def test_resolve_branch_is_mutable(prompt_repo):
    import subprocess

    subprocess.run(
        ["git", "checkout", "-b", "feat", "HEAD"], cwd=prompt_repo, check=True, capture_output=True
    )
    sha, display, mutable = gitutil.resolve_prompt_ref(prompt_repo, "classifier", "feat")
    assert mutable
    assert display == "feat"


def test_resolve_latest_prefers_highest_tag(prompt_repo):
    release(prompt_repo, "classifier", "v1")
    commit_prompt(prompt_repo, "classifier", CLASSIFIER_V2, "v2 text")
    release(prompt_repo, "classifier", "v2")
    sha, display, mutable = gitutil.resolve_prompt_ref(prompt_repo, "classifier", "latest")
    assert mutable  # latest can move
    assert "promptpin/classifier@v2" in display
    tag_sha, _ = gitutil.resolve_ref(prompt_repo, "promptpin/classifier@v2")
    assert sha == tag_sha


def test_resolve_latest_falls_back_to_head(prompt_repo):
    sha, display, mutable = gitutil.resolve_prompt_ref(prompt_repo, "classifier", "latest")
    assert mutable and display == "latest→HEAD"
    assert sha == gitutil.head_sha(prompt_repo)


def test_resolve_unknown_ref_raises(prompt_repo):
    with pytest.raises(VersionNotFoundError):
        gitutil.resolve_prompt_ref(prompt_repo, "classifier", "v99")


def test_next_version_increments(prompt_repo):
    assert gitutil.next_version(prompt_repo, "classifier") == "v1"
    release(prompt_repo, "classifier", "v1")
    assert gitutil.next_version(prompt_repo, "classifier") == "v2"
    release(prompt_repo, "classifier", "v9")
    assert gitutil.next_version(prompt_repo, "classifier") == "v10"


def test_list_version_tags_sorted(prompt_repo):
    for v in ["v2", "v10", "v1"]:
        release(prompt_repo, "classifier", v)
    tags = gitutil.list_version_tags(prompt_repo, "classifier")
    assert [t.rsplit("@", 1)[-1] for t in tags] == ["v1", "v2", "v10"]


def test_show_file_at_old_commit(prompt_repo):
    sha_v1 = gitutil.head_sha(prompt_repo)
    commit_prompt(prompt_repo, "classifier", CLASSIFIER_V2, "change text")
    text = gitutil.show_file(prompt_repo, sha_v1, "prompts/classifier.yaml")
    assert "spam" not in text
    assert "bug, feature, question." in text


def test_show_file_missing_at_commit_raises(prompt_repo):
    sha = gitutil.head_sha(prompt_repo)
    with pytest.raises(VersionNotFoundError, match="does not exist"):
        gitutil.show_file(prompt_repo, sha, "prompts/never-existed.yaml")
