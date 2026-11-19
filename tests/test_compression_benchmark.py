# test_compression_benchmark.py

from blockchain_compression.benchmarks import compression_benchmark as comp_bench
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


def test_compression_benchmark_run_and_main_fast(monkeypatch, caplog):
    tiny_datasets = {
        "synthetic-blocks": lambda: synthetic_block_data(num_blocks=2, txs_per_block=3),
        "repetitive": lambda: repetitive_data(size=512),
        "random": lambda: random_data(size=512),
    }
    monkeypatch.setattr(comp_bench, "DATASETS", tiny_datasets)
    monkeypatch.setattr(
        comp_bench,
        "run_benchmarks",
        lambda presets=(1,), repeats=1: [
            benchmark_dataset(name, make_data(), presets[0], repeats)
            for name, make_data in tiny_datasets.items()
        ],
    )

    with caplog.at_level("INFO"):
        comp_bench.main()

    assert "synthetic-blocks" in caplog.text
    assert "repetitive" in caplog.text
    assert "random" in caplog.text
