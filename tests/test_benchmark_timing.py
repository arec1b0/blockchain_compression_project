# test_benchmark_timing.py

import pytest

from blockchain_compression.benchmarks import best_time


def test_best_time_runs_exact_repeats_and_returns_positive_float():
    calls = 0

    def _work():
        nonlocal calls
        calls += 1
        return sum(range(1000))

    elapsed = best_time(_work, repeats=3)
    assert calls == 3
    assert isinstance(elapsed, float)
    assert elapsed > 0


def test_best_time_input_validation():
    with pytest.raises(TypeError):
        best_time("not-callable", repeats=1)
    with pytest.raises(ValueError):
        best_time(lambda: None, repeats=0)
    with pytest.raises(ValueError):
        best_time(lambda: None, repeats=True)
