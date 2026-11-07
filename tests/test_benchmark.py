"""Keep the committed benchmark honest: it must run and meet its own claims."""

import importlib.util
from pathlib import Path

import pytest

BENCH = Path(__file__).resolve().parents[1] / "benchmarks" / "run.py"


@pytest.fixture(scope="module")
def bench():
    spec = importlib.util.spec_from_file_location("promptpin_bench", BENCH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_tamper_matrix_catches_every_attack(bench):
    _, stats = bench.tamper_matrix()
    assert stats["caught_now"] == stats["attacks"]
    assert stats["caught_0_2_0"] < stats["attacks"]  # the new defences must add something


def test_canonical_digest_has_no_false_drift_and_misses_nothing(bench):
    _, stats = bench.hash_granularity()
    canon = stats["canonical digest (0.3.0 review)"]
    assert canon == {"false_drift": 0.0, "missed": 0.0}
    assert stats["config digest (0.2.0 pin)"]["missed"] == 0.0
