# test_compression_benchmark.py

from blockchain_compression.benchmarks.compression_benchmark import (
    benchmark_dataset,
    random_data,
    repetitive_data,
    synthetic_block_data,
)


def test_benchmark_smoke():
    data = repetitive_data(size=10_000)
    result = benchmark_dataset("repetitive-small", data, preset=1, repeats=1)

    assert result.original_size == len(data)
    assert 0 < result.compressed_size < result.original_size
    assert result.ratio > 1
    assert result.compress_mb_per_s > 0
    assert result.decompress_mb_per_s > 0


def test_dataset_generators_are_deterministic():
    assert repetitive_data(1_000) == repetitive_data(1_000)
    assert random_data(1_000) == random_data(1_000)
    assert synthetic_block_data(2, 3) == synthetic_block_data(2, 3)
