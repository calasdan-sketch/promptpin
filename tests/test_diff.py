"""Diff: template text panel, config panel, recorded-eval panel."""

from promptpin import diff as diffmod
from promptpin import gitutil, statedb
from tests.conftest import CLASSIFIER_V2, commit_prompt, release


def _two_versions(prompt_repo):
    release(prompt_repo, "classifier", "v1")
    sha1 = gitutil.head_sha(prompt_repo)
    commit_prompt(prompt_repo, "classifier", CLASSIFIER_V2, "v2 text")
    release(prompt_repo, "classifier", "v2")
    sha2 = gitutil.head_sha(prompt_repo)
    return sha1, sha2


def test_text_diff_shows_change(prompt_repo):
    sha1, sha2 = _two_versions(prompt_repo)
    a, _, _ = diffmod.load_at(prompt_repo, "classifier", "v1")
    b, _, _ = diffmod.load_at(prompt_repo, "classifier", "v2")
    td = diffmod.text_diff(a, b, "v1", "v2")
    assert "+Reply with one of: bug, feature, question, spam." in td
    assert "-Reply with one of: bug, feature, question." in td


def test_text_diff_identical_templates(prompt_repo):
    release(prompt_repo, "classifier", "v1")
    a, _, _ = diffmod.load_at(prompt_repo, "classifier", "v1")
    assert diffmod.text_diff(a, a, "v1", "v1") == ""


def test_config_diff_catches_temperature(prompt_repo):
    _two_versions(prompt_repo)
    a, _, _ = diffmod.load_at(prompt_repo, "classifier", "v1")
    b, _, _ = diffmod.load_at(prompt_repo, "classifier", "v2")
    changes = diffmod.config_diff(a, b)
    temp = [c for c in changes if c[0] == "params.temperature"]
    assert temp and temp[0][1] == "0.2" and temp[0][2] == "0.5"


def test_config_diff_empty_when_identical(prompt_repo):
    release(prompt_repo, "classifier", "v1")
    a, _, _ = diffmod.load_at(prompt_repo, "classifier", "v1")
    assert diffmod.config_diff(a, a) == []


def _record(prompt_repo, sha, metric, value, suite="nightly", ds="ds-sha-1"):
    conn = statedb.connect(prompt_repo)
    try:
        statedb.record_eval(
            conn,
            prompt="classifier",
            ref="vX",
            resolved_sha=sha,
            suite=suite,
            dataset_sha=ds,
            metric=metric,
            value=value,
            recorded_by="tester",
        )
    finally:
        conn.close()


def test_eval_delta_shows_recorded_change(prompt_repo):
    sha1, sha2 = _two_versions(prompt_repo)
    _record(prompt_repo, sha1, "accuracy", 0.94)
    _record(prompt_repo, sha2, "accuracy", 0.91)
    rows = diffmod.eval_delta(prompt_repo, "classifier", sha1, sha2)
    acc = [r for r in rows if r["metric"] == "accuracy"][0]
    assert acc["a"]["value"] == 0.94
    assert acc["b"]["value"] == 0.91
    assert abs(acc["delta"] - (-0.03)) < 1e-9
    assert acc["provenance_match"]


def test_eval_delta_flags_provenance_mismatch(prompt_repo):
    sha1, sha2 = _two_versions(prompt_repo)
    _record(prompt_repo, sha1, "accuracy", 0.94, suite="nightly", ds="ds-1")
    _record(prompt_repo, sha2, "accuracy", 0.97, suite="manual", ds="ds-2")
    rows = diffmod.eval_delta(prompt_repo, "classifier", sha1, sha2)
    acc = [r for r in rows if r["metric"] == "accuracy"][0]
    assert not acc["provenance_match"]


def test_eval_delta_metric_missing_on_one_side(prompt_repo):
    sha1, sha2 = _two_versions(prompt_repo)
    _record(prompt_repo, sha2, "latency_p50_ms", 120.0)
    rows = diffmod.eval_delta(prompt_repo, "classifier", sha1, sha2)
    lat = [r for r in rows if r["metric"] == "latency_p50_ms"][0]
    assert lat["a"] is None and lat["b"]["value"] == 120.0
    assert lat["delta"] is None


def test_render_diff_honest_eval_disclaimer(prompt_repo):
    sha1, sha2 = _two_versions(prompt_repo)
    _record(prompt_repo, sha1, "accuracy", 0.94)
    out = diffmod.render_diff(prompt_repo, "classifier", "v1", "v2")
    assert "=== template ===" in out
    assert "=== config ===" in out
    assert "=== recorded evals ===" in out
    assert "not proof" in out  # causal honesty disclaimer
    assert "accuracy" in out


def test_render_diff_no_evals_message(prompt_repo):
    _two_versions(prompt_repo)
    out = diffmod.render_diff(prompt_repo, "classifier", "v1", "v2")
    assert "no evals recorded" in out
