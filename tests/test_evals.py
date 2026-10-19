"""Evals: provenance requirements, latest-value semantics, cherry-pick visibility."""

from promptpin import gitutil, statedb
from tests.conftest import release


def _record(conn, **kw):
    kw.setdefault("prompt", "classifier")
    kw.setdefault("ref", "v1")
    kw.setdefault("resolved_sha", "sha1")
    kw.setdefault("suite", "nightly")
    kw.setdefault("dataset_sha", "ds-abc")
    kw.setdefault("recorded_by", "tester")
    return statedb.record_eval(conn, **kw)


def test_record_and_query(prompt_repo):
    release(prompt_repo, "classifier", "v1")
    sha = gitutil.head_sha(prompt_repo)
    conn = statedb.connect(prompt_repo)
    try:
        _record(conn, metric="accuracy", value=0.94, resolved_sha=sha)
        rows = statedb.evals_for_sha(conn, "classifier", sha)
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["metric"] == "accuracy"
    assert rows[0]["value"] == 0.94
    assert rows[0]["suite"] == "nightly"
    assert rows[0]["dataset_sha"] == "ds-abc"


def test_latest_eval_values_picks_newest(prompt_repo):
    conn = statedb.connect(prompt_repo)
    try:
        _record(conn, metric="accuracy", value=0.90, resolved_sha="s1")
        _record(conn, metric="accuracy", value=0.95, resolved_sha="s1")
        latest = statedb.latest_eval_values(conn, "classifier", "s1")
    finally:
        conn.close()
    assert latest["accuracy"]["value"] == 0.95
    assert latest["accuracy"]["n_records"] == 2


def test_cherry_pick_visible_via_record_count(prompt_repo):
    """Five runs recorded, only the best shown as 'latest' — but the count
    reveals the history exists. Full provenance is one query away."""
    conn = statedb.connect(prompt_repo)
    try:
        for v in [0.80, 0.82, 0.79, 0.81, 0.99]:
            _record(conn, metric="accuracy", value=v, resolved_sha="s1")
        latest = statedb.latest_eval_values(conn, "classifier", "s1")
        all_rows = statedb.evals_for_sha(conn, "classifier", "s1")
    finally:
        conn.close()
    assert latest["accuracy"]["value"] == 0.99
    assert latest["accuracy"]["n_records"] == 5
    assert len(all_rows) == 5  # nothing hidden


def test_evals_isolated_per_sha(prompt_repo):
    conn = statedb.connect(prompt_repo)
    try:
        _record(conn, metric="accuracy", value=0.90, resolved_sha="s1")
        _record(conn, metric="accuracy", value=0.70, resolved_sha="s2")
        assert statedb.latest_eval_values(conn, "classifier", "s1")["accuracy"]["value"] == 0.90
        assert statedb.latest_eval_values(conn, "classifier", "s2")["accuracy"]["value"] == 0.70
    finally:
        conn.close()


def test_evals_filtered_by_ref(prompt_repo):
    conn = statedb.connect(prompt_repo)
    try:
        _record(conn, metric="accuracy", value=0.90, ref="v1")
        _record(conn, metric="accuracy", value=0.91, ref="v2")
        rows = statedb.evals_for_prompt(conn, "classifier", ref="v2")
    finally:
        conn.close()
    assert len(rows) == 1 and rows[0]["value"] == 0.91


def test_cli_eval_record_requires_provenance(prompt_repo):
    from promptpin.cli import main

    release(prompt_repo, "classifier", "v1")
    # Missing --suite → error, nothing recorded.
    rc = main(
        [
            "--repo",
            str(prompt_repo),
            "eval",
            "record",
            "--prompt",
            "classifier",
            "--ref",
            "v1",
            "--dataset-sha",
            "ds1",
            "--metric",
            "accuracy=0.9",
        ]
    )
    assert rc == 1
    conn = statedb.connect(prompt_repo)
    try:
        assert statedb.evals_for_prompt(conn, "classifier") == []
    finally:
        conn.close()


def test_cli_eval_record_requires_dataset_sha(prompt_repo):
    from promptpin.cli import main

    release(prompt_repo, "classifier", "v1")
    rc = main(
        [
            "--repo",
            str(prompt_repo),
            "eval",
            "record",
            "--prompt",
            "classifier",
            "--ref",
            "v1",
            "--suite",
            "nightly",
            "--metric",
            "accuracy=0.9",
        ]
    )
    assert rc == 1


def test_cli_eval_record_bad_metric_format(prompt_repo):
    from promptpin.cli import main

    release(prompt_repo, "classifier", "v1")
    rc = main(
        [
            "--repo",
            str(prompt_repo),
            "eval",
            "record",
            "--prompt",
            "classifier",
            "--ref",
            "v1",
            "--suite",
            "nightly",
            "--dataset-sha",
            "ds1",
            "--metric",
            "accuracy:high",
        ]
    )
    assert rc == 1


def test_cli_eval_record_happy_path(prompt_repo, capsys):
    from promptpin.cli import main

    release(prompt_repo, "classifier", "v1")
    rc = main(
        [
            "--repo",
            str(prompt_repo),
            "eval",
            "record",
            "--prompt",
            "classifier",
            "--ref",
            "v1",
            "--suite",
            "nightly",
            "--dataset-sha",
            "ds1",
            "--metric",
            "accuracy=0.94",
            "--metric",
            "latency_p50_ms=120",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "2 metric(s)" in out
