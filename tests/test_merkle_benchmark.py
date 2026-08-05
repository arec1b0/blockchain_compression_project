# test_merkle_benchmark.py

from blockchain_compression.benchmarks.merkle_benchmark import benchmark_size


def test_proof_length_within_log2_bound():
    # Structural, deterministic assertion - no timing involved, so no CI flakiness.
    for size in (1, 2, 3, 5, 10, 50, 200):
        result = benchmark_size(size, repeats=1)
        assert result.proof_length <= result.log2_bound


def test_benchmark_result_fields_are_positive():
    result = benchmark_size(64, repeats=1)
    assert result.num_transactions == 64
    assert result.proof_length > 0
    assert result.get_proof_seconds > 0
    assert result.verify_seconds > 0


def test_single_transaction_has_empty_proof():
    result = benchmark_size(1, repeats=1)
    assert result.proof_length == 0
    assert result.log2_bound == 0
